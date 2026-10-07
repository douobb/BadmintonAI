"""將 Open WebUI 對話安全匯出為可離線開啟的單一 HTML。"""

from __future__ import annotations

import argparse
import html
import json
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Sequence

from markdown_it import MarkdownIt

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _PROJECT_ROOT / "src"
if _SOURCE_ROOT.is_dir() and str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

# 直接執行腳本時，需先加入專案 src 才能載入核心套件。
from badminton_ai.server.chart_display import MAX_EMBED_HTML_BYTES  # noqa: E402
from badminton_ai.server.plotly_rich import (  # noqa: E402
    MAX_PLOTLY_SPEC_BYTES,
    PLOTLY_SPEC_VERSION,
    PLOTLY_VERSION,
    PlotlySpecError,
    ValidatedPlotlyCharts,
    validate_plotly_charts,
)

MAX_CHAT_JSON_BYTES = 64 * 1024 * 1024
MAX_CHAT_MESSAGES = 20_000
_MARKDOWN = MarkdownIt("js-default", {"breaks": True})


class ChatExportError(ValueError):
    """對話 JSON 或 active branch 無法安全匯出。"""


class _ChartDataParser(HTMLParser):
    """只擷取指定 application/json script 的文字，不處理其他 HTML。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.payloads: list[str] = []
        self.in_script = False
        self.capture_script = False
        self.script_parts: list[str] = []
        self.malformed = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        if self.in_script:
            self.malformed = True
        self.in_script = True
        values: dict[str, list[str | None]] = {}
        for name, value in attrs:
            values.setdefault(name, []).append(value)
        if len(values.get("id", [])) > 1 or len(values.get("type", [])) > 1:
            self.malformed = True
            self.capture_script = False
            return
        script_id = values.get("id", [None])[0]
        media_type = values.get("type", [None])[0]
        self.capture_script = (
            script_id == "plotly-figure-data"
            and isinstance(media_type, str)
            and media_type.strip().casefold() == "application/json"
        )
        if self.capture_script:
            self.script_parts = []

    def handle_data(self, data: str) -> None:
        if self.in_script and self.capture_script:
            self.script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "script" or not self.in_script:
            return
        if self.capture_script:
            self.payloads.append("".join(self.script_parts))
        self.in_script = False
        self.capture_script = False
        self.script_parts = []

    def finish(self) -> None:
        self.close()
        if self.in_script:
            self.malformed = True


def _unique_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ChatExportError("JSON 內含重複欄位")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ChatExportError(f"JSON 不接受 {value}")


def _history_from_chat(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list):
        if len(payload) != 1:
            raise ChatExportError("只支援恰好一筆對話的原生 JSON 匯出檔")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ChatExportError("對話 JSON 根節點必須是物件")
    chat = payload
    wrapped_chat = payload.get("chat")
    if isinstance(wrapped_chat, dict) and "history" in wrapped_chat:
        chat = wrapped_chat
    history = chat.get("history")
    if not isinstance(history, dict):
        raise ChatExportError("找不到 Open WebUI history 物件")
    return history


def _active_branch(history: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    messages = history.get("messages")
    current_id = history.get("currentId")
    if not isinstance(messages, dict) or not messages:
        raise ChatExportError("history.messages 必須是非空物件")
    if len(messages) > MAX_CHAT_MESSAGES:
        raise ChatExportError("對話訊息數超出匯出上限")
    if not isinstance(current_id, str) or not current_id:
        raise ChatExportError("history.currentId 無效，無法判斷目前分支")

    branch: list[tuple[str, dict[str, Any]]] = []
    visited: set[str] = set()
    message_id: str | None = current_id
    while message_id is not None:
        if not isinstance(message_id, str) or not message_id:
            raise ChatExportError("active branch 含無效訊息 ID")
        if message_id in visited:
            raise ChatExportError("active branch 的 parentId 形成循環")
        visited.add(message_id)
        message = messages.get(message_id)
        if not isinstance(message, dict):
            raise ChatExportError(f"active branch 找不到訊息：{message_id}")
        stored_id = message.get("id")
        if stored_id is not None and stored_id != message_id:
            raise ChatExportError(f"訊息 ID 與 history key 不一致：{message_id}")
        branch.append((message_id, message))
        parent_id = message.get("parentId")
        if parent_id is not None and (not isinstance(parent_id, str) or not parent_id):
            raise ChatExportError(f"訊息 {message_id} 的 parentId 無效")
        message_id = parent_id

    branch.reverse()
    return branch


def _message_text(message: dict[str, Any], message_id: str) -> str:
    content = message.get("content", "")
    if content is None:
        return ""
    if not isinstance(content, str):
        raise ChatExportError(f"訊息 {message_id} 的 content 不是純文字")
    return content


def _message_embeds(message: dict[str, Any]) -> Sequence[Any] | None:
    if "embeds" in message:
        embeds = message["embeds"]
    else:
        metadata = message.get("metadata")
        embeds = metadata.get("embeds") if isinstance(metadata, dict) else None
    if embeds is None:
        return None
    if not isinstance(embeds, list):
        return (None,)
    return embeds


def parse_embedded_plotly_charts(
    embed: str, *, native_validation: bool = True
) -> ValidatedPlotlyCharts:
    """從 embed 僅擷取並驗證圖表 JSON，不執行或複製模型 HTML。"""

    if len(embed.encode("utf-8")) > MAX_EMBED_HTML_BYTES:
        raise ChatExportError("embed 超過大小上限")
    parser = _ChartDataParser()
    try:
        parser.feed(embed)
        parser.finish()
    except Exception as exc:
        raise ChatExportError("embed HTML 無法解析") from exc
    if parser.malformed:
        raise ChatExportError("embed HTML 的 script 結構無效")
    if not parser.payloads:
        raise ChatExportError("找不到 plotly-figure-data JSON")
    if len(parser.payloads) != 1:
        raise ChatExportError("embed 含多個 plotly-figure-data script")
    raw_json = parser.payloads[0]
    if len(raw_json.encode("utf-8")) > MAX_PLOTLY_SPEC_BYTES:
        raise ChatExportError("圖表 JSON 超過大小上限")
    try:
        embedded_payload = json.loads(
            raw_json,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_object_pairs,
        )
    except ChatExportError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ChatExportError("圖表 JSON 格式無效") from exc

    if isinstance(embedded_payload, dict) and set(embedded_payload) == {"charts"}:
        payload = {
            "schema_version": PLOTLY_SPEC_VERSION,
            "charts": embedded_payload["charts"],
        }
    else:
        payload = embedded_payload
    try:
        return validate_plotly_charts(payload, native_validation=native_validation)
    except PlotlySpecError as exc:
        raise ChatExportError(str(exc)) from exc


def _parse_embed_charts(
    embed: str, *, native_validation: bool = True
) -> ValidatedPlotlyCharts:
    return parse_embedded_plotly_charts(embed, native_validation=native_validation)


def script_safe_json(value: Any) -> str:
    """輸出可安全放入 script 文字節點的 JSON，避開 HTML script 結束序列。"""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _script_safe_json(value: Any) -> str:
    return script_safe_json(value)


def _plotly_javascript() -> str:
    try:
        import plotly
        from plotly.offline import get_plotlyjs
    except ImportError as exc:
        raise ChatExportError("找不到本機 Plotly 套件，無法建立離線互動圖表") from exc
    if plotly.__version__ != PLOTLY_VERSION:
        raise ChatExportError(
            f"本機 Plotly 版本必須是 {PLOTLY_VERSION}（目前為 {plotly.__version__}）"
        )
    javascript = get_plotlyjs()
    if not isinstance(javascript, str) or not javascript:
        raise ChatExportError("本機 Plotly.js 資產無效")
    if "</script" in javascript.casefold():
        raise ChatExportError("本機 Plotly.js 含無法安全內嵌的 script 終止標記")
    return javascript


def build_chat_export_html(
    payload: Any,
    *,
    theme: str = "light",
    native_validation: bool = True,
    plotly_javascript_provider: Callable[[], str] | None = None,
) -> tuple[str, int, int, int]:
    """建立離線 HTML；預設使用本機 Plotly，provider 僅供安全注入固定資產。

    native_validation=False 仍執行完整結構與安全檢查。自訂 provider 的輸出
    會限制大小並拒絕可提前終止 script 區塊的內容。
    """

    if theme not in {"light", "dark", "auto"}:
        raise ChatExportError("主題必須是 light、dark 或 auto")
    history = _history_from_chat(payload)
    branch = _active_branch(history)
    chart_payload: list[dict[str, Any]] = []
    message_markup: list[str] = []
    embed_warnings = 0

    for message_id, message in branch:
        role = message.get("role")
        if not isinstance(role, str) or not role.strip():
            raise ChatExportError(f"訊息 {message_id} 缺少有效 role")
        content = _message_text(message, message_id)
        parts = [
            '<article class="message" data-role="',
            html.escape(role, quote=True),
            '"><header>',
            html.escape(role),
            '</header><div class="message-content">',
            _MARKDOWN.render(content),
            "</div>",
        ]
        embeds = _message_embeds(message)
        if embeds is not None:
            for embed_index, embed in enumerate(embeds):
                try:
                    if not isinstance(embed, str):
                        raise ChatExportError("embed 不是 HTML 文字")
                    validated = _parse_embed_charts(
                        embed, native_validation=native_validation
                    )
                    for chart in validated.charts:
                        chart_index = len(chart_payload)
                        chart_payload.append(chart)
                        parts.extend(
                            [
                                '<section class="chart-card"><h2>',
                                html.escape(chart["title"], quote=True),
                                '</h2><div class="chart" id="chart-',
                                str(chart_index),
                                '" role="img" aria-label="',
                                html.escape(chart["title"], quote=True),
                                '"></div></section>',
                            ]
                        )
                except ChatExportError as exc:
                    embed_warnings += 1
                    parts.extend(
                        [
                            '<aside class="embed-warning">圖表 embed ',
                            str(embed_index + 1),
                            " 無法匯出：",
                            html.escape(str(exc), quote=True),
                            "。未執行或複製其 HTML/Script。</aside>",
                        ]
                    )
        parts.append("</article>")
        message_markup.append("".join(parts))

    library_script = ""
    if chart_payload:
        if plotly_javascript_provider is None:
            library_script = _plotly_javascript()
        else:
            try:
                library_script = plotly_javascript_provider()
            except Exception:
                raise ChatExportError("無法取得安全的固定版 Plotly 資產") from None
            if not isinstance(library_script, str) or not library_script:
                raise ChatExportError("固定版 Plotly 資產無效")
            try:
                script_size = len(library_script.encode("utf-8"))
            except UnicodeEncodeError:
                raise ChatExportError("固定版 Plotly 資產格式無效") from None
            if script_size > 8 * 1024 * 1024:
                raise ChatExportError("固定版 Plotly 資產超過大小限制")
            if "</script" in library_script.casefold():
                raise ChatExportError("固定版 Plotly 資產含不安全的 script 終止標記")
    chart_json = script_safe_json(chart_payload)
    csp = (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src data: blob:; font-src data:; connect-src 'none'; worker-src blob:"
    )
    library_block = f"<script>\n{library_script}\n</script>" if library_script else ""
    chart_block = (
        f'<script id="chat-chart-data" type="application/json">{chart_json}</script>'
        if chart_payload
        else ""
    )
    renderer_block = (
        """
<script>
(function () {
  "use strict";
  const charts = JSON.parse(document.getElementById("chat-chart-data").textContent);
  const pdfState = window.__BADMINTON_PDF_RENDER__;
  const colorScheme = window.matchMedia("(prefers-color-scheme: light)");
  const themeMode = document.documentElement.dataset.theme;
  const rendered = [];
  const cartesianTypes = new Set(["bar", "box", "funnel", "heatmap", "histogram", "histogram2d", "scatter", "scattergl", "violin", "waterfall"]);
  const palette = () => {
    const light = themeMode === "light" || (themeMode === "auto" && colorScheme.matches);
    return light
      ? {text: "#262626", muted: "#737373", grid: "rgba(0,0,0,.12)", axis: "rgba(0,0,0,.24)", hover: "#fff", hoverBorder: "#d4d4d4"}
      : {text: "#e5e5e5", muted: "#a3a3a3", grid: "rgba(255,255,255,.12)", axis: "rgba(255,255,255,.24)", hover: "#262626", hoverBorder: "#525252"};
  };
  const applyPalette = (layout, data, theme) => {
    layout.paper_bgcolor = "rgba(0,0,0,0)";
    layout.plot_bgcolor = "rgba(0,0,0,0)";
    layout.font = {...(layout.font || {}), color: theme.text};
    layout.legend = {...(layout.legend || {}), bgcolor: "rgba(0,0,0,0)", font: {...((layout.legend && layout.legend.font) || {}), color: theme.text}};
    layout.hoverlabel = {...(layout.hoverlabel || {}), bgcolor: theme.hover, bordercolor: theme.hoverBorder, font: {...((layout.hoverlabel && layout.hoverlabel.font) || {}), color: theme.text}};
    const axes = Object.keys(layout).filter(name => /^[xy]axis\\d*$/.test(name));
    if (data.some(trace => cartesianTypes.has(trace.type || "scatter"))) {
      if (!axes.includes("xaxis")) axes.push("xaxis");
      if (!axes.includes("yaxis")) axes.push("yaxis");
    }
    for (const name of axes) {
      const axis = layout[name] || {};
      layout[name] = {...axis, gridcolor: theme.grid, linecolor: theme.axis, zerolinecolor: theme.axis,
        tickfont: {...(axis.tickfont || {}), color: theme.muted}};
    }
    return axes;
  };
  const themeUpdate = (axes, theme) => {
    const update = {"font.color": theme.text, "legend.bgcolor": "rgba(0,0,0,0)",
      "legend.font.color": theme.text, "hoverlabel.bgcolor": theme.hover,
      "hoverlabel.bordercolor": theme.hoverBorder, "hoverlabel.font.color": theme.text};
    for (const name of axes) {
      update[name + ".gridcolor"] = theme.grid;
      update[name + ".linecolor"] = theme.axis;
      update[name + ".zerolinecolor"] = theme.axis;
      update[name + ".tickfont.color"] = theme.muted;
    }
    return update;
  };
  try {
    const jobs = [];
    for (const [index, chart] of charts.entries()) {
      const target = document.getElementById("chart-" + index);
      const layout = JSON.parse(JSON.stringify(chart.figure.layout || {}));
      delete layout.title;
      layout.autosize = true;
      const axes = applyPalette(layout, chart.figure.data, palette());
      const options = {responsive: true, displaylogo: false, scrollZoom: false};
      const job = window.Plotly.newPlot(target, chart.figure.data, layout, options)
        .then(function () {
          rendered.push({target, axes});
          if (Array.isArray(chart.figure.frames) && chart.figure.frames.length) {
            return window.Plotly.addFrames(target, chart.figure.frames);
          }
          return null;
        })
        .catch(function () {
          target.textContent = "此圖表無法呈現。";
          throw new Error("chart-render-failed");
        });
      jobs.push(job);
    }
    Promise.all(jobs).then(function () {
      pdfState.status = "ready";
    }).catch(function () {
      pdfState.status = "error";
      pdfState.error = "chart-render-failed";
    });
  } catch {
    pdfState.status = "error";
    pdfState.error = "chart-render-failed";
  }
  if (themeMode === "auto") {
    colorScheme.addEventListener("change", () => {
      const theme = palette();
      for (const {target, axes} of rendered) {
        window.Plotly.relayout(target, themeUpdate(axes, theme));
      }
    });
  }
}());
</script>"""
        if chart_payload
        else ""
    )
    pdf_render_status = "pending" if chart_payload else "ready"
    document = f"""<!doctype html>
<html lang="zh-Hant" data-theme="{theme}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<title>Open WebUI 對話匯出</title>
<style>
:root {{ color-scheme: light; font-family: "Noto Sans CJK TC", "Noto Sans TC", system-ui, -apple-system, "Segoe UI", sans-serif; background: #fff; color: #262626; }}
:root[data-theme="dark"] {{ color-scheme: dark; background: #171717; color: #e5e5e5; }}
@media (prefers-color-scheme: dark) {{ :root[data-theme="auto"] {{ color-scheme: dark; background: #171717; color: #e5e5e5; }} }}
/* 只隱藏瀏覽器插入 html 根層的空白 iframe，不影響正文或圖表。 */
html > iframe:empty:not([src]):not([srcdoc]) {{ display: none !important; }}
body {{ max-width: 960px; margin: 0 auto; padding: 24px 16px; line-height: 1.55; }}
.message {{ margin: 0 0 20px; padding: 16px; border: 1px solid #8885; border-radius: 10px; }}
.message[data-role="user"] {{ background: #8881; }}
.message header {{ margin-bottom: 8px; font-weight: 700; text-transform: capitalize; }}
.message-content {{ overflow-wrap: anywhere; }}
.message-content > :first-child {{ margin-top: 0; }}
.message-content > :last-child {{ margin-bottom: 0; }}
.message-content p {{ margin: 0 0 .75em; }}
.message-content h1, .message-content h2, .message-content h3 {{ line-height: 1.3; margin: 1em 0 .4em; }}
.message-content h1 {{ font-size: 1.4rem; }}
.message-content h2 {{ font-size: 1.2rem; }}
.message-content h3 {{ font-size: 1.05rem; }}
.message-content ul, .message-content ol {{ padding-left: 1.5em; margin: .5em 0; }}
.message-content li + li {{ margin-top: .25em; }}
.message-content blockquote {{ margin: .75em 0; padding: .2em 0 .2em 1em; border-left: 3px solid #8888; color: inherit; }}
.message-content pre {{ margin: .75em 0; padding: 12px; overflow-x: auto; border-radius: 6px; background: #8882; white-space: pre; }}
.message-content code {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .9em; }}
.message-content :not(pre) > code {{ padding: .1em .25em; border-radius: 4px; background: #8882; }}
.message-content table {{ display: block; max-width: 100%; overflow-x: auto; border-collapse: collapse; margin: .75em 0; }}
.message-content th, .message-content td {{ border: 1px solid #8885; padding: .35em .6em; text-align: left; }}
.message-content th {{ background: #8882; }}
.message-content a {{ color: #075bc1; }}
:root[data-theme="dark"] .message-content a {{ color: #93c5fd; }}
@media (prefers-color-scheme: dark) {{ :root[data-theme="auto"] .message-content a {{ color: #93c5fd; }} }}
.chart-card {{ margin-top: 16px; padding: 12px; border: 1px solid #8885; border-radius: 8px; }}
.chart-card h2 {{ margin: 0 0 8px; font-size: 1rem; }}
.chart {{ min-height: 300px; width: 100%; }}
.embed-warning {{ margin-top: 12px; padding: 10px 12px; color: #7f1d1d; background: #fee2e2; border-radius: 6px; overflow-wrap: anywhere; }}
:root[data-theme="dark"] .embed-warning {{ color: #fecaca; background: #450a0a; }}
@media (prefers-color-scheme: dark) {{ :root[data-theme="auto"] .embed-warning {{ color: #fecaca; background: #450a0a; }} }}
@media print {{
  @page {{ size: A4; margin: 14mm; }}
  :root,:root[data-theme="dark"] {{ color-scheme: light !important; background: #fff !important; color: #171717 !important; print-color-adjust: exact; -webkit-print-color-adjust: exact; }}
  body {{ max-width: none; margin: 0; padding: 0; background: #fff !important; color: #171717 !important; font-size: 10pt; }}
  .message {{ background: #fff !important; color: #171717 !important; border-color: #aaa; break-inside: auto; }}
  .message header {{ break-after: avoid-page; page-break-after: avoid; }}
  .message-content table {{ display: table; width: 100%; max-width: 100%; overflow: visible; }}
  .message-content thead {{ display: table-header-group; }}
  .message-content tr {{ break-inside: avoid; page-break-inside: avoid; }}
  .message-content pre {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
  .chart-card {{ break-inside: avoid; page-break-inside: avoid; }}
  .chart {{ min-height: 320px; }}
}}
</style>
</head>
<body>
<main aria-label="對話紀錄">
{"".join(message_markup)}
</main>
<script>window.__BADMINTON_PDF_RENDER__ = {{status: "{pdf_render_status}", error: null}};</script>
{library_block}
{chart_block}
{renderer_block}
</body>
</html>"""
    return document, len(branch), len(chart_payload), embed_warnings


def _load_chat_json(path: Path) -> Any:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ChatExportError(f"無法讀取輸入檔：{exc}") from exc
    if len(content) > MAX_CHAT_JSON_BYTES:
        raise ChatExportError("對話 JSON 超過 64 MiB 匯出上限")
    try:
        return json.loads(
            content.decode("utf-8-sig"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_object_pairs,
        )
    except ChatExportError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ChatExportError("輸入檔不是有效的 UTF-8 JSON") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="安全匯出 Open WebUI 對話為可離線開啟的單一 HTML。"
    )
    parser.add_argument("input_json", type=Path, help="單一對話原生 JSON 檔")
    parser.add_argument("output_html", type=Path, help="輸出的 HTML 檔")
    parser.add_argument(
        "--theme",
        choices=("light", "dark", "auto"),
        default="light",
        help="匯出主題；預設固定淺色，auto 才跟隨閱讀裝置",
    )
    args = parser.parse_args(argv)

    try:
        if args.input_json.resolve() == args.output_html.resolve():
            raise ChatExportError("輸入與輸出路徑不可相同")
        if args.output_html.exists():
            raise ChatExportError("輸出檔已存在，請使用新的輸出路徑")
        payload = _load_chat_json(args.input_json)
        document, message_count, chart_count, warning_count = build_chat_export_html(
            payload, theme=args.theme
        )
        with args.output_html.open("x", encoding="utf-8", newline="\n") as output:
            output.write(document)
    except (ChatExportError, OSError) as exc:
        print(f"匯出失敗：{exc}", file=sys.stderr)
        return 1

    print(
        f"已匯出 {message_count} 則 active branch 訊息、{chart_count} 張有效圖表；"
        f"{warning_count} 個 embed 顯示匯出提示：{args.output_html}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
