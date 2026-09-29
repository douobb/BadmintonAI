"""將已儲存的單一對話製成含靜態圖表的列印版 PDF。"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

PAGE_WIDTH = 595.28
PAGE_HEIGHT = 841.89
INK = colors.HexColor("#263238")
MUTED = colors.HexColor("#64748b")
TEAL = colors.HexColor("#189474")
FONT_NAME = "ChatCJK"


class ExportError(ValueError):
    """原生對話或圖表資料無法匯出。"""


class ChartParser(HTMLParser):
    """只擷取已保存 embed 的 Plotly JSON，不執行其 HTML。"""

    def __init__(self) -> None:
        super().__init__()
        self.active = False
        self.parts: list[str] = []
        self.payloads: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "script" and attributes.get("id") == "plotly-figure-data":
            self.active = True
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.active:
            self.payloads.append("".join(self.parts))
            self.active = False


def _messages(payload: object) -> list[dict]:
    if isinstance(payload, list):
        if len(payload) != 1:
            raise ExportError("PDF 只接受單一對話 JSON")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ExportError("對話 JSON 格式無效")
    chat = payload.get("chat", payload)
    if not isinstance(chat, dict):
        raise ExportError("對話內容無效")
    history = chat.get("history")
    if not isinstance(history, dict) or not isinstance(history.get("messages"), dict):
        raise ExportError("找不到對話 history")
    by_id = history["messages"]
    current = history.get("currentId")
    seen: set[str] = set()
    branch: list[dict] = []
    while current is not None:
        if not isinstance(current, str) or current in seen or current not in by_id:
            raise ExportError("對話分支無效或形成循環")
        seen.add(current)
        message = by_id[current]
        if not isinstance(message, dict):
            raise ExportError("訊息格式無效")
        branch.append(message)
        current = message.get("parentId")
    branch.reverse()
    return branch


def _charts(message: dict) -> list[dict]:
    result: list[dict] = []
    for embed in message.get("embeds") or []:
        if not isinstance(embed, str):
            continue
        parser = ChartParser()
        parser.feed(embed)
        for raw in parser.payloads:
            try:
                payload = json.loads(html.unescape(raw))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("charts"), list):
                result.extend(
                    chart for chart in payload["charts"] if isinstance(chart, dict)
                )
    return result


def _values(value: object) -> list:
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, dict) and set(value) >= {"dtype", "bdata"}:
        dtype = value["dtype"]
        if dtype not in {"i1", "i2", "i4", "i8", "u1", "u2", "u4", "u8", "f4", "f8"}:
            raise ExportError("不支援的 Plotly 數值型別")
        raw = base64.b64decode(value["bdata"], validate=True)
        return np.frombuffer(raw, dtype=dtype).tolist()
    raise ExportError("圖表數值陣列無效")


class StaticChart(Flowable):
    """從已保存的 Plotly trace 畫出列印用靜態圖，不重新分析資料。"""

    def __init__(self, chart: dict) -> None:
        super().__init__()
        self.chart = chart
        self.width = 480
        self.height = 332

    def draw(self) -> None:
        title = str(self.chart.get("title", "圖表"))
        self.canv.setFont(FONT_NAME, 11)
        self.canv.setFillColor(INK)
        self.canv.drawString(4, 310, title[:48])
        figure = self.chart.get("figure", {})
        traces = figure.get("data", []) if isinstance(figure, dict) else []
        if len(traces) != 1 or not isinstance(traces[0], dict):
            self._unavailable()
            return
        trace = traces[0]
        try:
            if trace.get("type") == "bar":
                self._bar(trace)
            elif trace.get("type") == "pie":
                self._pie(trace)
            elif trace.get("type") == "heatmap":
                self._heatmap(trace)
            else:
                self._unavailable()
        except (ExportError, KeyError, TypeError, ValueError):
            self._unavailable()

    def _unavailable(self) -> None:
        self.canv.setFillColor(MUTED)
        self.canv.drawString(8, 270, "此圖型無法靜態繪製；請使用互動 HTML 檢視。")

    def _bar(self, trace: dict) -> None:
        labels = _values(trace["x"])
        counts = [float(x) for x in _values(trace["y"])]
        if not counts or len(labels) != len(counts):
            raise ExportError("長條圖資料長度不一致")
        maximum = max(counts) or 1
        n = len(counts)
        step = min(25, 260 / n)
        self.canv.setFont(FONT_NAME, 8)
        for index, (label, count) in enumerate(zip(labels, counts)):
            y = 282 - index * step
            self.canv.setFillColor(INK)
            self.canv.drawRightString(100, y + 2, str(label)[:15])
            self.canv.setFillColor(TEAL)
            self.canv.roundRect(108, y, 300 * count / maximum, 13, 3, fill=1, stroke=0)
            self.canv.setFillColor(INK)
            self.canv.drawString(414, y + 2, f"{count:,.0f}")

    def _pie(self, trace: dict) -> None:
        labels = _values(trace["labels"])
        counts = [float(x) for x in _values(trace["values"])]
        if not counts or len(labels) != len(counts):
            raise ExportError("圓餅圖資料長度不一致")
        palette = [
            "#159575",
            "#527dd0",
            "#ef8f4a",
            "#8a6fc7",
            "#d15c7a",
            "#61a6b0",
            "#e6b555",
            "#729f68",
            "#9ca3af",
        ]
        total = sum(counts)
        angle = 90.0
        for index, (label, count) in enumerate(zip(labels, counts)):
            fraction = count / total
            self.canv.setFillColor(colors.HexColor(palette[index % len(palette)]))
            self.canv.wedge(30, 58, 248, 276, angle, 360 * fraction, fill=1, stroke=0)
            angle += 360 * fraction
            self.canv.rect(270, 266 - index * 27, 10, 10, fill=1, stroke=0)
            self.canv.setFillColor(INK)
            self.canv.setFont(FONT_NAME, 8)
            self.canv.drawString(
                286,
                268 - index * 27,
                f"{str(label)[:11]}  {fraction * 100:.1f}% ({count:,.0f})",
            )

    def _heatmap(self, trace: dict) -> None:
        xlabels = _values(trace["x"])
        ylabels = _values(trace["y"])
        matrix = [_values(row) for row in trace["z"]]
        if len(matrix) != len(ylabels) or any(
            len(row) != len(xlabels) for row in matrix
        ):
            raise ExportError("熱區圖資料形狀不一致")
        numbers = [float(value) for row in matrix for value in row]
        low, high = min(numbers), max(numbers)
        cell_w = 76
        cell_h = min(40, 245 / len(ylabels))
        self.canv.setFont(FONT_NAME, 8)
        for col, label in enumerate(xlabels):
            self.canv.setFillColor(INK)
            self.canv.drawCentredString(130 + col * cell_w, 285, str(label))
        for row, values in enumerate(matrix):
            y = 267 - (row + 1) * cell_h
            self.canv.setFillColor(INK)
            self.canv.drawRightString(87, y + cell_h / 2, str(ylabels[row])[:12])
            for col, value in enumerate(values):
                ratio = (float(value) - low) / (high - low or 1)
                self.canv.setFillColor(
                    colors.Color(
                        1 - 0.22 * ratio, 0.91 - 0.66 * ratio, 0.64 - 0.48 * ratio
                    )
                )
                self.canv.rect(
                    92 + col * cell_w, y, cell_w - 2, cell_h - 2, fill=1, stroke=0
                )
                self.canv.setFillColor(INK)
                self.canv.drawCentredString(
                    130 + col * cell_w, y + cell_h / 2, f"{float(value):,.0f}"
                )


def _plain_line(line: str) -> str:
    line = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", line)
    line = re.sub(r"\*\*|__|`", "", line)
    return escape(line.strip())


def _message_body(content: str, body_style: ParagraphStyle) -> list:
    flowables: list = []
    lines = content.splitlines()
    position = 0
    while position < len(lines):
        line = lines[position].strip()
        if not line:
            flowables.append(Spacer(1, 4))
            position += 1
            continue
        if line.startswith("|"):
            table_rows = []
            while position < len(lines) and lines[position].strip().startswith("|"):
                cells = [
                    cell.strip()
                    for cell in lines[position].strip().strip("|").split("|")
                ]
                if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                    table_rows.append(
                        [Paragraph(_plain_line(cell), body_style) for cell in cells]
                    )
                position += 1
            if table_rows:
                widths = [440 / len(table_rows[0])] * len(table_rows[0])
                table = Table(table_rows, colWidths=widths, repeatRows=1)
                table.setStyle(
                    TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6f3ef")),
                            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd5d1")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 5),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                        ]
                    )
                )
                flowables.extend([table, Spacer(1, 5)])
            continue
        prefix = "• " if line.startswith("- ") else ""
        if prefix:
            line = line[2:]
        flowables.append(Paragraph(prefix + _plain_line(line), body_style))
        position += 1
    return flowables


def export_pdf(source: Path, destination: Path) -> tuple[int, int]:
    if destination.exists():
        raise ExportError("輸出 PDF 已存在，請使用新的路徑")
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    messages = _messages(payload)
    font_path = next(
        (
            path
            for path in (
                Path("C:/Windows/Fonts/msjh.ttc"),
                Path("C:/Windows/Fonts/kaiu.ttf"),
            )
            if path.is_file()
        ),
        None,
    )
    if font_path is None:
        raise ExportError("找不到可嵌入的繁體中文字型")
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(font_path), subfontIndex=0))
    title_style = ParagraphStyle(
        "title",
        fontName=FONT_NAME,
        fontSize=18,
        leading=25,
        textColor=INK,
        spaceAfter=12,
    )
    heading_style = ParagraphStyle(
        "heading",
        fontName=FONT_NAME,
        fontSize=11,
        leading=16,
        textColor=TEAL,
        spaceBefore=10,
        spaceAfter=5,
    )
    body_style = ParagraphStyle(
        "body",
        fontName=FONT_NAME,
        fontSize=9,
        leading=15,
        textColor=INK,
        spaceAfter=3,
        wordWrap="CJK",
    )
    note_style = ParagraphStyle("note", parent=body_style, textColor=MUTED, fontSize=8)
    story: list = [
        Paragraph("BadmintonAI 對話匯出驗收", title_style),
        Paragraph(
            "此 PDF 由已儲存的對話與圖表資料製作；圖表是靜態列印版，互動請使用 HTML。保留原對話中的澄清、失敗與修正紀錄。",
            note_style,
        ),
        Spacer(1, 14),
    ]
    chart_count = 0
    for index, message in enumerate(messages, 1):
        role = "使用者" if message.get("role") == "user" else "BadmintonAI"
        story.append(Paragraph(f"{index:02d} · {role}", heading_style))
        content = message.get("content", "")
        if isinstance(content, str):
            story.extend(_message_body(content, body_style))
        if message.get("role") == "assistant":
            for chart in _charts(message):
                story.extend([Spacer(1, 8), StaticChart(chart), Spacer(1, 10)])
                chart_count += 1
        story.append(Spacer(1, 8))

    destination.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(destination),
        pagesize=(PAGE_WIDTH, PAGE_HEIGHT),
        rightMargin=55,
        leftMargin=55,
        topMargin=55,
        bottomMargin=55,
        title="BadmintonAI 對話匯出驗收",
    )

    def page_footer(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont(FONT_NAME, 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(55, 30, "BadmintonAI · 靜態 PDF 對話匯出")
        canvas.drawRightString(PAGE_WIDTH - 55, 30, f"{doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=page_footer, onLaterPages=page_footer)
    return len(messages), chart_count


def main() -> None:
    parser = argparse.ArgumentParser(description="從單一對話 JSON 匯出靜態圖表 PDF")
    parser.add_argument("input_json", type=Path)
    parser.add_argument("output_pdf", type=Path)
    args = parser.parse_args()
    count, charts = export_pdf(args.input_json, args.output_pdf)
    print(f"PDF 已匯出 {count} 則訊息、{charts} 張靜態圖表：{args.output_pdf}")


if __name__ == "__main__":
    main()
