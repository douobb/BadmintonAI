"""安全地將 checkpoint 評測紀錄輸出為離線互動及可列印 HTML。"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

# 執行於 Open WebUI 時，核心驗證檔以精確唯讀掛載在 runtime/src。
_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if _SOURCE_ROOT.is_dir() and str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from badminton_ai.server.plotly_rich import (  # noqa: E402
    PLOTLY_ASSET_PATH,
    PLOTLY_JS_VERSION,
)
from scripts.export_chat_html import (  # noqa: E402
    ChatExportError,
    parse_embedded_plotly_charts,
    script_safe_json,
)

RUN_ID_PATTERN = re.compile(r"[a-f0-9]{32}\Z")
MAX_PLOTLY_JS_BYTES = 8 * 1024 * 1024
MAX_REPORT_CHART_JSON_BYTES = 64 * 1024 * 1024
MAX_REPORT_HTML_BYTES = 128 * 1024 * 1024
REPORT_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; connect-src 'none'; worker-src blob:; "
    "frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
)
_STATUS_LABELS = {
    "pending": "待執行",
    "running": "執行中",
    "awaiting_clarification": "等待補答",
    "completed": "已完成",
    "failed": "失敗",
}
_PLOTLY_ASSET_CACHE: dict[str, str] = {}
_PLOTLY_ASSET_CACHE_LOCK = threading.Lock()
_REPORT_MARKDOWN = MarkdownIt("js-default", {"breaks": True})


def _report_image(tokens: list[Any], index: int, options: Any, env: Any) -> str:
    """不輸出模型文字中的圖片 URL；圖表只由已驗證的 embed 呈現。"""

    del options, env
    token = tokens[index]
    source = token.attrGet("src") or ""
    if source.partition(":")[0].casefold() == "attachment":
        return ""
    return html.escape(token.content, quote=False) or "（未保存圖片）"


def _report_link_open(tokens: list[Any], index: int, options: Any, env: Any) -> str:
    """保留 Markdown 連結文字，但令離線報告不含可點選的外連。"""

    del tokens, index, options, env
    return '<span class="markdown-link">'


def _report_link_close(tokens: list[Any], index: int, options: Any, env: Any) -> str:
    del tokens, index, options, env
    return "</span>"


_REPORT_MARKDOWN.renderer.rules["image"] = _report_image
_REPORT_MARKDOWN.renderer.rules["link_open"] = _report_link_open
_REPORT_MARKDOWN.renderer.rules["link_close"] = _report_link_close


class EvaluationReportError(ValueError):
    """Checkpoint 或固定 Plotly 資產無法安全匯出。"""


def fetch_plotly_javascript() -> str:
    """只從核准的 Tool Server 取得固定路徑 Plotly bundle。"""

    base_url = os.environ.get(
        "BADMINTON_AI_EVALUATION_TOOL_SERVER_URL", "http://tool-server:8000"
    ).rstrip("/")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError:
        raise EvaluationReportError("Plotly 資產服務設定無效") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname != "tool-server"
        or port != 8000
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise EvaluationReportError("Plotly 資產僅允許固定內部 Tool Server")

    request = urllib.request.Request(
        f"{base_url}{PLOTLY_ASSET_PATH}",
        headers={"Accept": "application/javascript, text/javascript"},
    )
    asset_url = request.full_url
    with _PLOTLY_ASSET_CACHE_LOCK:
        cached_javascript = _PLOTLY_ASSET_CACHE.get(asset_url)
    if cached_javascript is not None:
        return cached_javascript

    class _RejectRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, new_url):
            del req, fp, code, msg, headers, new_url
            return None

    try:
        opener = urllib.request.build_opener(_RejectRedirects)
        with opener.open(request, timeout=60) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"application/javascript", "text/javascript"}:
                raise EvaluationReportError("Plotly 資產 MIME type 無效")
            raw = response.read(MAX_PLOTLY_JS_BYTES + 1)
    except EvaluationReportError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        raise EvaluationReportError("無法取得固定版 Plotly 資產") from None
    if not raw or len(raw) > MAX_PLOTLY_JS_BYTES:
        raise EvaluationReportError("Plotly 資產大小無效")
    try:
        javascript = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise EvaluationReportError("Plotly 資產不是 UTF-8") from None
    if "</script" in javascript.casefold():
        raise EvaluationReportError("Plotly 資產含不安全的 script 終止標記")
    if f"plotly.js v{PLOTLY_JS_VERSION}" not in javascript.casefold():
        raise EvaluationReportError("Plotly 資產版本不符合評測報告契約")
    with _PLOTLY_ASSET_CACHE_LOCK:
        return _PLOTLY_ASSET_CACHE.setdefault(asset_url, javascript)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _duration(value: Any) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return "未提供"
    seconds, milliseconds = divmod(value, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _nullable_number(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    return "未提供（null）"


def _usage(value: Any) -> str:
    if value is None:
        return "input_tokens=未提供（null）；output_tokens=未提供（null）；total_tokens=未提供（null）"
    if not isinstance(value, dict):
        return "input_tokens=未提供（null）；output_tokens=未提供（null）；total_tokens=未提供（null）"
    input_tokens = value.get("input_tokens", value.get("prompt_tokens"))
    output_tokens = value.get("output_tokens", value.get("completion_tokens"))
    return "；".join(
        (
            f"input_tokens={_nullable_number(input_tokens)}",
            f"output_tokens={_nullable_number(output_tokens)}",
            f"total_tokens={_nullable_number(value.get('total_tokens'))}",
        )
    )


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    except (TypeError, ValueError):
        return "null"


def _snapshot_value(value: Any) -> str:
    if value is None:
        return "未提供"
    if isinstance(value, (str, int, float, bool)):
        display = str(value)
    else:
        display = _json_text(value).replace("\n", " ")
    return display if len(display) <= 512 else display[:509] + "…"


def _snapshot_card(
    label: str, snapshot: Any, fields: tuple[tuple[str, str], ...]
) -> str:
    values = snapshot if isinstance(snapshot, dict) else {}
    rows = "".join(
        "<div><dt>"
        + html.escape(field_label)
        + "</dt><dd>"
        + html.escape(_snapshot_value(values.get(key)))
        + "</dd></div>"
        for key, field_label in fields
        if key in values
    )
    if not rows:
        rows = "<div><dt>版本資訊</dt><dd>未提供</dd></div>"
    return (
        '<section class="snapshot-card"><h4>'
        + html.escape(label)
        + "</h4><dl>"
        + rows
        + "</dl></section>"
    )


def _compact_snapshot_markup(snapshot: dict[str, Any]) -> str:
    return (
        '<div class="snapshot-compact" aria-label="列印用精簡版本快照">'
        + _snapshot_card(
            "題目來源",
            snapshot.get("question_source"),
            (("name", "檔名"), ("sha256", "SHA-256"), ("question_count", "題數")),
        )
        + _snapshot_card(
            "模型",
            snapshot.get("model_snapshot"),
            (
                ("model_id", "模型 ID"),
                ("model_name", "名稱"),
                ("updated_at", "模型更新時間"),
                ("tool_ids", "工具綁定"),
            ),
        )
        + _snapshot_card(
            "資料集",
            snapshot.get("data_snapshot"),
            (
                ("snapshot_id", "資料快照 ID"),
                ("snapshot_version", "資料版本"),
                ("tool_server_version", "Tool Server 版本"),
                ("source", "來源"),
                ("row_count", "資料列數"),
            ),
        )
        + "</div>"
    )


def _message_embeds(message: dict[str, Any]) -> list[Any]:
    embeds = message.get("embeds")
    if embeds is None and isinstance(message.get("metadata"), dict):
        embeds = message["metadata"].get("embeds")
    return embeds if isinstance(embeds, list) else []


def _turn_results(
    turn: dict[str, Any],
) -> list[tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    attempts = turn.get("attempts")
    if isinstance(attempts, list) and attempts:
        results = []
        for attempt in attempts:
            if isinstance(attempt, dict):
                result = attempt.get("result")
                results.append((attempt, result if isinstance(result, dict) else None))
        return results
    result = turn.get("result")
    return [(None, result if isinstance(result, dict) else None)]


def _role_label(role: Any) -> str:
    labels = {"assistant": "模型回答", "tool": "工具訊息", "user": "使用者訊息"}
    return labels.get(role, "訊息")


def build_evaluation_report_html(
    state: dict[str, Any],
    *,
    plotly_js_provider: Callable[[], str] = fetch_plotly_javascript,
) -> tuple[str, int]:
    """從 checkpoint 產生報告與已安全解析的圖表數；不呼叫模型或工具。"""

    run_id = state.get("run_id")
    questions = state.get("questions")
    if not isinstance(run_id, str) or RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise EvaluationReportError("run ID 格式無效")
    if not isinstance(questions, list):
        raise EvaluationReportError("評測 checkpoint 缺少題目清單")
    manifest = state.get("manifest") if isinstance(state.get("manifest"), dict) else {}
    chart_payload: list[dict[str, Any]] = []
    question_markup: list[str] = []
    total_tool_calls = 0
    total_chart_metadata = 0
    embed_warnings = 0
    chart_payload_bytes = 0
    status_counts: dict[str, int] = {}
    usage_values: dict[str, list[int | None]] = {
        "input_tokens": [],
        "output_tokens": [],
        "total_tokens": [],
    }

    for question_index, question in enumerate(questions, start=1):
        if not isinstance(question, dict):
            continue
        question_id = _text(question.get("id")) or str(question_index)
        status = _text(question.get("status")) or "未知"
        status_counts[status] = status_counts.get(status, 0) + 1
        usage_totals = question.get("usage_totals")
        for key, values in usage_values.items():
            value = usage_totals.get(key) if isinstance(usage_totals, dict) else None
            values.append(
                value
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                else None
            )
        turns = question.get("turns")
        if not isinstance(turns, list):
            turns = []
        pending_turn = question.get("pending_turn")
        if isinstance(pending_turn, dict):
            turns = [*turns, pending_turn]

        question_tools = 0
        question_chart_metadata = 0
        question_chart_start = len(chart_payload)
        turn_markup: list[str] = []
        for turn_index, turn in enumerate(turns, start=1):
            if not isinstance(turn, dict):
                continue
            kind = turn.get("kind")
            label = (
                "原始提問"
                if kind == "question"
                or (turn_index == 1 and kind not in {"clarification", "retry"})
                else "補答"
                if kind == "clarification"
                else "重試回合"
                if kind == "retry"
                else "補答"
            )
            turn_markup.append(
                '<section class="turn"><h3>'
                + f"第 {turn_index} 輪{label}</h3><h4>提問／補答</h4><pre>"
                + html.escape(_text(turn.get("request")))
                + "</pre>"
            )
            results = _turn_results(turn)
            for attempt_index, (attempt, result) in enumerate(results, start=1):
                if len(results) > 1 or attempt is not None:
                    turn_markup.append(f"<h4>執行嘗試 {attempt_index}</h4>")
                if attempt is not None:
                    duration = _duration(attempt.get("duration_ms"))
                    turn_markup.append(f"<p>此嘗試耗時：{html.escape(duration)}</p>")
                    error = attempt.get("error")
                    if isinstance(error, dict):
                        turn_markup.append(
                            '<p class="error">錯誤：'
                            + html.escape(
                                _text(error.get("message")) or _text(error.get("type"))
                            )
                            + "</p>"
                        )
                if result is None:
                    continue
                tool_calls = result.get("tool_calls")
                charts_meta = result.get("charts")
                tool_count = len(tool_calls) if isinstance(tool_calls, list) else 0
                chart_count = len(charts_meta) if isinstance(charts_meta, list) else 0
                question_tools += tool_count
                question_chart_metadata += chart_count
                messages = result.get("messages")
                assistant_messages = (
                    [
                        message
                        for message in messages
                        if isinstance(message, dict)
                        and message.get("role") in {"assistant", "tool"}
                    ]
                    if isinstance(messages, list)
                    else []
                )
                if not assistant_messages and isinstance(result.get("message"), dict):
                    assistant_messages = [result["message"]]
                if not assistant_messages:
                    turn_markup.append('<p class="muted">此回合未保存文字回覆。</p>')
                for message in assistant_messages:
                    role = message.get("role")
                    content = _text(message.get("content"))
                    turn_markup.append(
                        '<article class="reply"><h4>'
                        + html.escape(_role_label(role))
                        + '</h4><div class="reply-content">'
                        + (
                            _REPORT_MARKDOWN.render(content)
                            if content
                            else "<p>（無文字內容）</p>"
                        )
                        + "</div></article>"
                    )
                    for embed_index, embed in enumerate(
                        _message_embeds(message), start=1
                    ):
                        try:
                            if not isinstance(embed, str):
                                raise ChatExportError("embed 不是文字")
                            validated = parse_embedded_plotly_charts(
                                embed, native_validation=False
                            )
                        except ChatExportError:
                            embed_warnings += 1
                            turn_markup.append(
                                '<p class="warning">圖表 embed '
                                + str(embed_index)
                                + " 未通過安全檢查，未執行或複製其 HTML。</p>"
                            )
                            continue
                        for chart in validated.charts:
                            chart_bytes = len(
                                json.dumps(
                                    chart,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                    allow_nan=False,
                                ).encode("utf-8")
                            )
                            chart_payload_bytes += chart_bytes
                            if chart_payload_bytes > MAX_REPORT_CHART_JSON_BYTES:
                                raise EvaluationReportError(
                                    "報告圖表 JSON 總量超過 64 MiB 上限"
                                )
                            chart_index = len(chart_payload)
                            chart_payload.append(chart)
                            safe_title = html.escape(chart["title"], quote=True)
                            turn_markup.append(
                                '<section class="chart-card"><h4>'
                                + safe_title
                                + '</h4><div class="chart" id="chart-'
                                + str(chart_index)
                                + '" role="img" aria-label="'
                                + safe_title
                                + '"></div></section>'
                            )
                turn_markup.append(
                    '<p class="turn-stats">Token：'
                    + html.escape(_usage(result.get("usage")))
                    + f"；工具呼叫：{tool_count}；圖表 metadata：{chart_count}</p>"
                )
                error = result.get("error")
                if isinstance(error, dict):
                    turn_markup.append(
                        '<p class="error">回合錯誤：'
                        + html.escape(
                            _text(error.get("message")) or _text(error.get("type"))
                        )
                        + "</p>"
                    )
            turn_markup.append("</section>")

        question_chart_count = len(chart_payload) - question_chart_start
        total_tool_calls += question_tools
        total_chart_metadata += question_chart_metadata
        saved_errors = question.get("errors")
        error_markup = ""
        if isinstance(saved_errors, list) and saved_errors:
            entries = []
            for saved_error in saved_errors:
                if not isinstance(saved_error, dict):
                    continue
                stage = html.escape(_text(saved_error.get("stage")) or "未知階段")
                error_type = html.escape(_text(saved_error.get("type")) or "錯誤")
                message = html.escape(
                    _text(saved_error.get("message")) or "未提供錯誤內容"
                )
                entries.append(f"<li>{stage}／{error_type}：{message}</li>")
            if entries:
                error_markup = (
                    '<section class="errors"><h3>錯誤紀錄</h3><ul>'
                    + "".join(entries)
                    + "</ul></section>"
                )
        question_markup.append(
            '<article class="question"><header><h2>題目 '
            + html.escape(question_id)
            + '</h2><span class="status">'
            + html.escape(_STATUS_LABELS.get(status, status))
            + '</span></header><dl class="stats">'
            + "<div><dt>端到端耗時</dt><dd>"
            + html.escape(_duration(question.get("elapsed_ms")))
            + "</dd></div><div><dt>模型處理耗時</dt><dd>"
            + html.escape(_duration(question.get("processing_elapsed_ms")))
            + "</dd></div><div><dt>等待補答耗時</dt><dd>"
            + html.escape(_duration(question.get("user_wait_ms")))
            + "</dd></div><div><dt>Token 合計（只記實際 usage）</dt><dd>"
            + html.escape(_usage(question.get("usage_totals")))
            + "</dd></div><div><dt>工具呼叫</dt><dd>"
            + str(question_tools)
            + "</dd></div><div><dt>圖表 metadata</dt><dd>"
            + str(question_chart_metadata)
            + "</dd></div><div><dt>安全呈現圖表</dt><dd>"
            + str(question_chart_count)
            + "</dd></div></dl><section><h3>原始題目</h3><pre>"
            + html.escape(_text(question.get("prompt")))
            + "</pre></section>"
            + (
                "".join(turn_markup)
                if turn_markup
                else '<p class="muted">尚無執行回合。</p>'
            )
            + (
                '<p class="muted">此題未產生圖表。</p>'
                if question_chart_count == 0 and question_chart_metadata == 0
                else '<p class="warning">沒有保存可安全重現的圖表 embed；本報告不補繪。</p>'
                if question_chart_count == 0
                else ""
            )
            + error_markup
            + "</article>"
        )

    plotly_js = ""
    if chart_payload:
        try:
            plotly_js = plotly_js_provider()
        except EvaluationReportError:
            raise
        except Exception:
            raise EvaluationReportError("無法取得固定版 Plotly 資產") from None
        if (
            not isinstance(plotly_js, str)
            or not plotly_js
            or len(plotly_js.encode("utf-8")) > MAX_PLOTLY_JS_BYTES
            or "</script" in plotly_js.casefold()
            or f"plotly.js v{PLOTLY_JS_VERSION}" not in plotly_js.casefold()
        ):
            raise EvaluationReportError("固定版 Plotly 資產驗證失敗")

    run_usage = {
        key: (
            sum(values)
            if values
            and len(values) == len(questions)
            and all(value is not None for value in values)
            else None
        )
        for key, values in usage_values.items()
    }
    run_snapshot = {
        "run_id": run_id,
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "status_counts": status_counts,
        "usage_totals": run_usage,
        "question_source": manifest.get("question_source"),
        "model_snapshot": manifest.get("model_snapshot"),
        "data_snapshot": manifest.get("data_snapshot"),
        "question_count": len(questions),
        "tool_call_count": total_tool_calls,
        "chart_metadata_count": total_chart_metadata,
        "safe_embedded_chart_count": len(chart_payload),
        "rejected_embed_count": embed_warnings,
    }
    status_summary = (
        "、".join(
            f"{_STATUS_LABELS.get(key, key)} {value} 題"
            for key, value in sorted(status_counts.items())
        )
        or "未提供"
    )
    report_data = script_safe_json(chart_payload)
    library_block = f"<script>\n{plotly_js}\n</script>" if plotly_js else ""
    chart_data_block = (
        f'<script id="report-chart-data" type="application/json">{report_data}</script>'
        if chart_payload
        else ""
    )
    renderer_block = (
        """
<script>
(function () {
  "use strict";
  const chartData = JSON.parse(document.getElementById("report-chart-data").textContent);
  const rendered = [];
  const cartesianTypes = new Set(["bar", "box", "funnel", "heatmap", "histogram", "histogram2d", "scatter", "scattergl", "violin", "waterfall"]);
  const isLight = () => document.documentElement.dataset.theme === "light";
  const palette = () => isLight()
    ? {text: "#262626", muted: "#737373", grid: "rgba(0,0,0,.12)", axis: "rgba(0,0,0,.24)", hover: "#fff", hoverBorder: "#d4d4d4"}
    : {text: "#e5e5e5", muted: "#a3a3a3", grid: "rgba(255,255,255,.12)", axis: "rgba(255,255,255,.24)", hover: "#262626", hoverBorder: "#525252"};
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
  const paletteUpdate = axes => {
    const theme = palette();
    const update = {"font.color": theme.text, "legend.font.color": theme.text,
      "hoverlabel.bgcolor": theme.hover, "hoverlabel.bordercolor": theme.hoverBorder,
      "hoverlabel.font.color": theme.text};
    for (const name of axes) {
      update[name + ".gridcolor"] = theme.grid;
      update[name + ".linecolor"] = theme.axis;
      update[name + ".zerolinecolor"] = theme.axis;
      update[name + ".tickfont.color"] = theme.muted;
    }
    return update;
  };
  const promises = [];
  for (const [index, chart] of chartData.entries()) {
    const target = document.getElementById("chart-" + index);
    const layout = JSON.parse(JSON.stringify(chart.figure.layout || {}));
    delete layout.title;
    layout.autosize = true;
    const axes = applyPalette(layout, chart.figure.data, palette());
    const promise = window.Plotly.newPlot(target, chart.figure.data, layout,
      {responsive: true, displaylogo: false, scrollZoom: false})
      .then(() => {
        rendered.push({target, axes});
        if (Array.isArray(chart.figure.frames) && chart.figure.frames.length) {
          return window.Plotly.addFrames(target, chart.figure.frames);
        }
        return null;
      })
      .catch(() => { target.textContent = "此圖表無法呈現。"; });
    promises.push(promise);
  }
  window.evaluationReportChartsReady = Promise.all(promises);
  const setTheme = theme => {
    document.documentElement.dataset.theme = theme;
    for (const {target, axes} of rendered) window.Plotly.relayout(target, paletteUpdate(axes));
  };
  let automaticPrintTheme = null;
  window.addEventListener("beforeprint", () => {
    if (automaticPrintTheme !== null) return;
    automaticPrintTheme = document.documentElement.dataset.theme;
    setTheme("light");
  });
  window.addEventListener("afterprint", () => {
    if (automaticPrintTheme === null) return;
    const previousTheme = automaticPrintTheme;
    automaticPrintTheme = null;
    setTheme(previousTheme);
  });
  document.getElementById("report-theme").addEventListener("change", event => setTheme(event.currentTarget.value));
  document.getElementById("report-print").addEventListener("click", async () => {
    await window.evaluationReportChartsReady;
    const previousTheme = document.documentElement.dataset.theme;
    window.addEventListener("afterprint", () => setTheme(previousTheme), {once: true});
    setTheme("light");
    await Promise.all(rendered.map(({target, axes}) => window.Plotly.relayout(target, paletteUpdate(axes))));
    window.print();
  });
}());
</script>"""
        if chart_payload
        else """
<script>
(function () {
  "use strict";
  document.getElementById("report-theme").addEventListener("change", event => {
    document.documentElement.dataset.theme = event.currentTarget.value;
  });
  document.getElementById("report-print").addEventListener("click", () => window.print());
}());
</script>"""
    )
    document = f"""<!doctype html>
<html lang="zh-Hant" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{REPORT_CSP}">
<meta name="referrer" content="no-referrer">
<title>評測報告 {html.escape(run_id)}</title>
<style>
:root {{ color-scheme: light; --bg:#fff; --fg:#262626; --muted:#5b5b5b; --panel:#f5f5f5; --border:#c9c9c9; font-family:system-ui,-apple-system,"Segoe UI",sans-serif; background:var(--bg); color:var(--fg); }}
:root[data-theme="dark"] {{ color-scheme:dark; --bg:#171717; --fg:#e5e5e5; --muted:#b3b3b3; --panel:#262626; --border:#525252; }}
* {{ box-sizing:border-box; }}
body {{ max-width:1100px; margin:0 auto; padding:24px 18px 48px; background:var(--bg); color:var(--fg); line-height:1.55; }}
h1,h2,h3,h4 {{ line-height:1.3; }}
h1 {{ margin:.2em 0; font-size:1.8rem; }}
h2 {{ margin:0; font-size:1.25rem; }}
h3 {{ margin:.9em 0 .45em; font-size:1.05rem; }}
h4 {{ margin:.6em 0 .35em; font-size:.98rem; }}
.toolbar {{ position:sticky; top:0; z-index:2; display:flex; align-items:center; gap:12px; flex-wrap:wrap; padding:10px; margin:0 0 18px; background:var(--panel); border:1px solid var(--border); border-radius:8px; }}
.toolbar label {{ font-weight:650; }}
button,select {{ min-height:40px; padding:7px 12px; color:var(--fg); background:var(--bg); border:1px solid var(--border); border-radius:6px; font:inherit; }}
button {{ cursor:pointer; font-weight:650; }}
button:focus-visible,select:focus-visible {{ outline:3px solid #2563eb; outline-offset:2px; }}
a {{ color:#075bc1; }}
:root[data-theme="dark"] a {{ color:#93c5fd; }}
a:focus-visible {{ outline:3px solid #2563eb; outline-offset:2px; }}
.skip-link {{ position:absolute; left:-10000px; top:auto; width:1px; height:1px; overflow:hidden; }}
.skip-link:focus {{ left:12px; top:12px; z-index:3; width:auto; height:auto; padding:8px 12px; background:var(--bg); border:2px solid #2563eb; }}
.report-meta {{ margin:16px 0 24px; padding:16px; border:1px solid var(--border); border-radius:10px; }}
.report-meta dl,.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:8px 16px; }}
dt {{ color:var(--muted); font-size:.9rem; }}
dd {{ margin:0; overflow-wrap:anywhere; }}
.snapshot {{ margin-top:16px; }}
.snapshot pre,pre {{ overflow-wrap:anywhere; white-space:pre-wrap; word-break:break-word; }}
.snapshot pre,.question pre {{ margin:.25em 0 .8em; padding:12px; background:var(--panel); border:1px solid var(--border); border-radius:6px; font: .92rem/1.5 ui-monospace,SFMono-Regular,Consolas,monospace; }}
.snapshot-compact {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:10px; }}
.snapshot-card {{ min-width:0; padding:10px; border:1px solid var(--border); border-radius:6px; background:var(--panel); }}
.snapshot-card h4 {{ margin:0 0 6px; }}
.snapshot-card dl {{ display:block; margin:0; font-size:.88rem; }}
.snapshot-card dl > div {{ display:grid; grid-template-columns:minmax(80px,auto) minmax(0,1fr); align-items:start; gap:3px 10px; }}
.snapshot-card dt,.snapshot-card dd {{ min-width:0; }}
.snapshot-card dd {{ overflow-wrap:anywhere; }}
.snapshot-full {{ margin-top:10px; }}
.snapshot-full summary {{ width:max-content; max-width:100%; cursor:pointer; text-decoration:underline; text-underline-offset:2px; }}
.snapshot-full summary:focus-visible {{ outline:3px solid #2563eb; outline-offset:2px; }}
.snapshot-full pre {{ max-height:50vh; overflow:auto; }}
.question {{ margin:0 0 22px; padding:16px; border:1px solid var(--border); border-radius:10px; break-inside:auto; }}
.question > header {{ display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:12px; }}
.status {{ padding:3px 9px; border:1px solid var(--border); border-radius:999px; font-size:.86rem; }}
.turn {{ margin:16px 0 0; padding:14px; background:var(--panel); border-radius:8px; break-inside:auto; }}
.reply {{ margin:12px 0; padding-left:12px; border-left:3px solid #64748b; }}
.reply-content p {{ margin:.35em 0 .65em; }}
.reply-content ul,.reply-content ol {{ padding-left:1.5em; }}
.reply-content blockquote {{ margin:.5em 0; padding-left:1em; border-left:3px solid var(--border); color:var(--muted); }}
.reply-content code {{ overflow-wrap:anywhere; padding:.08em .25em; background:var(--panel); border-radius:3px; }}
.reply-content pre {{ max-width:100%; overflow:auto; }}
.reply-content table {{ border-collapse:collapse; max-width:100%; }}
.reply-content th,.reply-content td {{ padding:4px 7px; border:1px solid var(--border); }}
.markdown-link {{ text-decoration:underline; text-decoration-style:dotted; }}
.chart-card {{ margin:14px 0; padding:10px; border:1px solid var(--border); border-radius:8px; background:var(--bg); break-inside:avoid; }}
.chart {{ width:100%; min-height:350px; }}
.warning {{ color:#9a3412; }}
.error {{ color:#b91c1c; }}
:root[data-theme="dark"] .warning {{ color:#fdba74; }}
:root[data-theme="dark"] .error {{ color:#fca5a5; }}
.muted,.turn-stats {{ color:var(--muted); font-size:.9rem; }}
@media print {{
  @page {{ margin:14mm; }}
  :root,:root[data-theme="dark"] {{ color-scheme:light !important; --bg:#fff !important; --fg:#171717 !important; --muted:#444 !important; --panel:#f5f5f5 !important; --border:#aaa !important; background:#fff !important; color:#171717 !important; print-color-adjust:exact; -webkit-print-color-adjust:exact; }}
  body {{ max-width:none; padding:0; background:#fff !important; color:#171717 !important; font-size:10pt; }}
  .toolbar {{ display:none !important; }}
  .skip-link {{ display:none !important; }}
  .report-meta,.question,.turn,.chart-card {{ background:#fff !important; color:#171717 !important; border-color:#aaa !important; box-shadow:none !important; }}
  .question {{ margin:0 0 8mm; padding:5mm; }}
  .report-meta {{ margin:3mm 0 4mm; padding:3mm; }}
  .report-meta > dl,.question > .stats {{ gap:1.5mm 4mm; }}
  .snapshot {{ margin-top:2mm; }}
  .snapshot-full {{ display:none !important; }}
  .snapshot-compact {{ grid-template-columns:minmax(0,1fr); gap:1mm; }}
  .snapshot-card {{ padding:1.5mm 2mm; break-inside:avoid; page-break-inside:avoid; }}
  .snapshot-card h4 {{ margin-bottom:.5mm; font-size:8.5pt; }}
  .snapshot-card dl {{ font-size:7.5pt; line-height:1.12; }}
  .snapshot-card dl > div {{ grid-template-columns:minmax(35mm,42mm) minmax(0,1fr); gap:0 2mm; }}
  .snapshot-card dd {{ overflow-wrap:anywhere; word-break:normal; hyphens:auto; }}
  .turn {{ padding:3mm; }}
  .question > header,.question > section > h3,.turn > h3,.turn > h4,.reply > h4 {{ break-after:avoid-page; page-break-after:avoid; }}
  .turn-stats {{ display:table; margin-top:2mm; break-before:avoid-page; break-inside:avoid; page-break-before:avoid; page-break-inside:avoid; }}
  .reply-content p,.reply-content li {{ orphans:3; widows:3; }}
  .chart-card {{ break-inside:avoid; page-break-inside:avoid; }}
  .chart {{ min-height:320px; }}
  .chart .modebar {{ display:none !important; }}
  pre {{ color:#171717 !important; background:#f5f5f5 !important; }}
  .warning,.error {{ color:#7f1d1d !important; }}
}}
</style>
</head>
<body>
<a class="skip-link" href="#questions">跳至逐題紀錄</a>
<header><p>BadmintonAI · Evaluation</p><h1>評測執行報告</h1><p>HTML 離線報告；PDF 請使用瀏覽器「列印／另存 PDF」，本系統不提供直接 PDF API。</p></header>
<nav class="toolbar" aria-label="報告控制">
  <label for="report-theme">報告主題</label>
  <select id="report-theme"><option value="light">淺色</option><option value="dark">深色</option></select>
  <button id="report-print" type="button">列印／另存 PDF</button>
</nav>
<main>
<section class="report-meta" aria-labelledby="run-heading"><h2 id="run-heading">Run 快照與總計</h2>
<dl>
<div><dt>Run ID</dt><dd>{html.escape(run_id)}</dd></div>
<div><dt>建立時間</dt><dd>{html.escape(_text(state.get("created_at")) or "未提供")}</dd></div>
<div><dt>更新時間</dt><dd>{html.escape(_text(state.get("updated_at")) or "未提供")}</dd></div>
<div><dt>題數</dt><dd>{len(questions)}</dd></div>
<div><dt>題目狀態</dt><dd>{html.escape(status_summary)}</dd></div>
<div><dt>Token 合計（僅完整實測值）</dt><dd>{html.escape(_usage(run_usage))}</dd></div>
<div><dt>工具呼叫</dt><dd>{total_tool_calls}</dd></div>
<div><dt>圖表 metadata</dt><dd>{total_chart_metadata}</dd></div>
<div><dt>安全呈現圖表</dt><dd>{len(chart_payload)}</dd></div>
<div><dt>未通過安全檢查的 embed</dt><dd>{embed_warnings}</dd></div>
</dl>
<div class="snapshot"><h3>模型／資料與題目來源快照</h3>
{_compact_snapshot_markup(run_snapshot)}
<details class="snapshot-full"><summary>展開完整快照 JSON（螢幕檢視）</summary><pre>{html.escape(_json_text(run_snapshot))}</pre></details>
</div>
</section>
<section id="questions" aria-label="逐題評測紀錄">{"".join(question_markup)}</section>
</main>
{library_block}
{chart_data_block}
{renderer_block}
</body>
</html>"""
    if len(document.encode("utf-8")) > MAX_REPORT_HTML_BYTES:
        raise EvaluationReportError("HTML 報告超過 128 MiB 上限")
    return document, len(chart_payload)


__all__ = [
    "EvaluationReportError",
    "MAX_REPORT_HTML_BYTES",
    "REPORT_CSP",
    "build_evaluation_report_html",
    "fetch_plotly_javascript",
]
