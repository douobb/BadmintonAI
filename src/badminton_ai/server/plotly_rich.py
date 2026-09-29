"""解析並包裝 Plotly 原生 figure JSON；不執行 artifact 內的程式碼。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .chart_display import MAX_EMBED_HTML_BYTES

PLOTLY_VERSION = "6.6.0"
PLOTLY_JS_VERSION = "3.4.0"
PLOTLY_ASSET_PATH = f"/assets/plotly-{PLOTLY_VERSION}.min.js"
DEFAULT_PLOTLY_ASSET_URL = f"http://127.0.0.1:8000{PLOTLY_ASSET_PATH}"
PLOTLY_CHARTS_FILE = "plotly_charts.json"
PLOTLY_SPEC_VERSION = "badminton-plotly/v1"
MAX_PLOTLY_SPEC_BYTES = 1024 * 1024
MAX_PLOTLY_CHARTS = 4
MAX_PLOTLY_TRACES_PER_CHART = 50
MAX_PLOTLY_FRAMES_PER_CHART = 200
MAX_PLOTLY_JSON_NODES = 100_000
MAX_PLOTLY_JSON_DEPTH = 32
MAX_PLOTLY_STRING_CHARS = 16_384
MAX_PLOTLY_TITLE_CHARS = 120

_MARKUP_PATTERN = re.compile(r"<\s*/?\s*[A-Za-z!][^>]*>")
_SAFE_PLOTLY_TAG_PATTERN = re.compile(
    r"</?(?:br|b|i|em|strong|sup|sub|extra)\s*>", re.IGNORECASE
)
_SCHEME_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9.+-])(?:https?|ftp|file|javascript|data):"
)
_URL_FIELD_NAMES = frozenset(
    {
        "url",
        "src",
        "href",
        "source",
        "maplibre",
        "tileurl",
        "tiles",
        "images",
    }
)
_REMOTE_TRACE_TYPES = frozenset(
    {
        "choropleth",
        "choroplethmap",
        "choroplethmapbox",
        "choroplethmaplibre",
        "densitymap",
        "densitymapbox",
        "scattergeo",
        "scattermap",
        "scattermapbox",
    }
)
_CHART_FINGERPRINT_MARKER = re.compile(
    r'<meta name="badmintonai-chart-fingerprint" content="sha256:([0-9a-f]{64})">'
)


class PlotlySpecError(ValueError):
    """Plotly artifact 未符合有限、可安全呈現的 JSON 契約。"""


@dataclass(frozen=True, slots=True)
class ValidatedPlotlyCharts:
    """通過外層限制與 Plotly 自身 figure validator 的圖表集合。"""

    charts: tuple[dict[str, Any], ...]
    fingerprint: str


def parse_plotly_charts_artifact(artifact: Any) -> ValidatedPlotlyCharts:
    """只解析根目錄的 plotly_charts.json，並限制大小、結構與值。"""

    if getattr(artifact, "relative_path", None) != PLOTLY_CHARTS_FILE:
        raise PlotlySpecError("Plotly artifact 路徑不符合契約")
    if (
        getattr(artifact, "extension", None) != ".json"
        or getattr(artifact, "mime_type", None) != "application/json"
        or getattr(artifact, "kind", None) != "json"
    ):
        raise PlotlySpecError("Plotly artifact metadata 不符合契約")
    content_base64 = getattr(artifact, "content_base64", None)
    size_bytes = getattr(artifact, "size_bytes", None)
    if (
        not isinstance(content_base64, str)
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not 1 <= size_bytes <= MAX_PLOTLY_SPEC_BYTES
    ):
        raise PlotlySpecError("Plotly artifact 大小或內容無效")
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PlotlySpecError("Plotly artifact base64 無效") from exc
    if len(raw) != size_bytes:
        raise PlotlySpecError("Plotly artifact 大小不一致")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_object_pairs,
        )
    except PlotlySpecError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PlotlySpecError("Plotly JSON 無法解析") from exc
    return validate_plotly_charts(payload)


def validate_plotly_charts(
    payload: Any, *, native_validation: bool = True
) -> ValidatedPlotlyCharts:
    """驗證通用 figure；離線檢視可略過未安裝在 Open WebUI 的 Python Plotly。"""

    if not isinstance(payload, dict) or set(payload) != {"schema_version", "charts"}:
        raise PlotlySpecError("Plotly JSON 根欄位不符合契約")
    if payload["schema_version"] != PLOTLY_SPEC_VERSION:
        raise PlotlySpecError("Plotly JSON 版本不受支援")
    charts_payload = payload["charts"]
    if (
        not isinstance(charts_payload, list)
        or not 1 <= len(charts_payload) <= MAX_PLOTLY_CHARTS
    ):
        raise PlotlySpecError("Plotly 圖表數量超出範圍")

    node_count = [0]
    charts: list[dict[str, Any]] = []
    for index, chart_payload in enumerate(charts_payload):
        if not isinstance(chart_payload, dict) or set(chart_payload) != {
            "title",
            "figure",
        }:
            raise PlotlySpecError("Plotly 圖表欄位不符合契約")
        title = _text(chart_payload["title"], "title", MAX_PLOTLY_TITLE_CHARS)
        figure = chart_payload["figure"]
        if isinstance(figure, str):
            # Plotly 的 fig.to_json() 直接產生字串；在相同限制下正規化，避免無謂重跑分析。
            if len(figure.encode("utf-8")) > MAX_PLOTLY_SPEC_BYTES:
                raise PlotlySpecError("Plotly figure 超過大小上限")
            try:
                figure = json.loads(
                    figure,
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_unique_object_pairs,
                )
            except PlotlySpecError:
                raise
            except (json.JSONDecodeError, RecursionError) as exc:
                raise PlotlySpecError("Plotly figure JSON 無法解析") from exc
        if (
            not isinstance(figure, dict)
            or not {"data"} <= set(figure)
            or not set(figure) <= {"data", "layout", "frames"}
        ):
            raise PlotlySpecError("Plotly figure 必須是 data/layout/frames JSON")
        data = figure["data"]
        layout = figure.get("layout", {})
        frames = figure.get("frames", [])
        if (
            not isinstance(data, list)
            or not 1 <= len(data) <= MAX_PLOTLY_TRACES_PER_CHART
            or not isinstance(layout, dict)
            or not isinstance(frames, list)
            or len(frames) > MAX_PLOTLY_FRAMES_PER_CHART
            or any(not isinstance(trace, dict) for trace in data)
            or any(not isinstance(frame, dict) for frame in frames)
        ):
            raise PlotlySpecError("Plotly figure 結構或 trace/frame 數量無效")
        _validate_json_tree(figure, node_count=node_count)
        _reject_remote_or_markup_fields(figure)
        _validate_plotly_figure(figure, native_validation=native_validation)
        charts.append({"title": title, "figure": figure})

    canonical = json.dumps(
        {"schema_version": PLOTLY_SPEC_VERSION, "charts": charts},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(canonical.encode("utf-8")) > MAX_PLOTLY_SPEC_BYTES:
        raise PlotlySpecError("Plotly JSON 正規化後超過大小上限")
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return ValidatedPlotlyCharts(tuple(charts), fingerprint)


def validate_plotly_asset_url(value: str) -> str:
    """只允許本機 Tool Server 提供的固定版本 JS，不載入遠端 CDN。"""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or value != value.strip()
        or any(character.isspace() or ord(character) < 0x20 for character in value)
    ):
        raise ValueError("Plotly asset URL 無效")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Plotly asset URL 埠號無效") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != PLOTLY_ASSET_PATH
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("Plotly asset URL 必須指向本機固定版 JS 資產")
    return value


def render_plotly_charts_html(
    charts: ValidatedPlotlyCharts,
    *,
    asset_url: str = DEFAULT_PLOTLY_ASSET_URL,
) -> str:
    """以固定通用 renderer 將圖表資料放入可持久化的單一卡片。"""

    asset_url = validate_plotly_asset_url(asset_url)
    chart_markup = "\n".join(
        (
            '<section class="chart-panel" aria-label="'
            + html.escape(chart["title"], quote=True)
            + '"><h2>'
            + html.escape(chart["title"])
            + f'</h2><div class="plotly-figure" data-chart-index="{index}" '
            + 'role="group" aria-label="'
            + html.escape(chart["title"], quote=True)
            + '"></div></section>'
        )
        for index, chart in enumerate(charts.charts)
    )
    json_content = _script_safe_json(
        {"charts": charts.charts},
    )
    document = f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="badmintonai-chart-fingerprint" content="sha256:{charts.fingerprint}">
<style>
:root{{color-scheme:dark;--chart-fg:#e5e5e5;--chart-muted:#a3a3a3;--chart-grid:rgba(255,255,255,.12);--chart-axis:rgba(255,255,255,.24);--chart-hover-bg:#262626;--chart-hover-border:#525252;--chart-modebar:#a3a3a3;--chart-modebar-hover:#f5f5f5;--chart-modebar-bg:rgba(38,38,38,.92)}}
@media (prefers-color-scheme: light){{:root{{color-scheme:light;--chart-fg:#262626;--chart-muted:#737373;--chart-grid:rgba(0,0,0,.12);--chart-axis:rgba(0,0,0,.24);--chart-hover-bg:#fff;--chart-hover-border:#d4d4d4;--chart-modebar:#737373;--chart-modebar-hover:#171717;--chart-modebar-bg:rgba(255,255,255,.92)}}}}
html,body{{margin:0;padding:0;background:transparent;color:var(--chart-fg);font-family:system-ui,-apple-system,"Segoe UI",sans-serif}}
.card{{box-sizing:border-box;width:100%;min-width:0;padding:4px 8px;background:transparent;border:0;border-radius:0;color:var(--chart-fg)}}
.chart-panel{{min-width:0;margin:0 0 10px;overflow:visible}}
.chart-panel:last-child{{margin-bottom:0}}
h2{{margin:0 0 2px;font-size:16px;font-weight:600;line-height:1.4;overflow-wrap:anywhere}}
.plotly-figure{{width:100%;min-width:0;min-height:260px;overflow:visible}}
.plotly-figure .modebar{{right:6px!important;top:4px!important}}
.plotly-figure .modebar-group{{background:var(--chart-modebar-bg)!important;border-radius:6px}}
.plotly-figure .modebar-btn path{{fill:var(--chart-modebar)!important}}
.plotly-figure .modebar-btn:hover path,.plotly-figure .modebar-btn--active path{{fill:var(--chart-modebar-hover)!important}}
.load-error{{margin:8px 0;color:#dc2626;font-size:14px}}
@media (prefers-color-scheme: dark){{.load-error{{color:#fca5a5}}}}
@media (max-width:600px){{.card{{padding:4px 2px}}.plotly-figure{{min-height:260px}}}}
</style>
</head>
<body>
<main class="card" aria-label="互動圖表">
{chart_markup}
</main>
<script src="{html.escape(asset_url, quote=True)}"></script>
<script id="plotly-figure-data" type="application/json">{json_content}</script>
<script>
(function(){{
  "use strict";
  const card = document.querySelector(".card");
  if (!window.Plotly) {{
    const error = document.createElement("p");
    error.className = "load-error";
    error.textContent = "本機 Plotly 資產未載入，互動圖表無法顯示。";
    card.appendChild(error);
    return;
  }}
  const payload = JSON.parse(document.getElementById("plotly-figure-data").textContent);
  const config = {{responsive: true, displaylogo: false, scrollZoom: false}};
  const viewport = window.matchMedia("(max-width: 600px)");
  const colorScheme = window.matchMedia("(prefers-color-scheme: light)");
  const fontFamily = 'system-ui, -apple-system, "Segoe UI", sans-serif';
  const renderedCharts = [];
  const palette = () => colorScheme.matches
    ? {{text: "#262626", muted: "#737373", grid: "rgba(0,0,0,.12)", axis: "rgba(0,0,0,.24)", hover: "#fff", hoverBorder: "#d4d4d4"}}
    : {{text: "#e5e5e5", muted: "#a3a3a3", grid: "rgba(255,255,255,.12)", axis: "rgba(255,255,255,.24)", hover: "#262626", hoverBorder: "#525252"}};
  const cartesianTypes = new Set(["bar", "box", "funnel", "heatmap", "histogram", "histogram2d", "scatter", "scattergl", "violin", "waterfall"]);
  const axisNames = layout => Object.keys(layout).filter(name => /^[xy]axis\\d*$/.test(name));
  const applyPalette = (layout, theme, hasCartesianTrace, hasOverflowingLabels) => {{
    layout.paper_bgcolor = "rgba(0,0,0,0)";
    layout.plot_bgcolor = "rgba(0,0,0,0)";
    layout.autosize = true;
    layout.font = {{...(layout.font || {{}}), family: fontFamily, color: theme.text}};
    layout.legend = {{...(layout.legend || {{}}), bgcolor: "rgba(0,0,0,0)", font: {{...((layout.legend && layout.legend.font) || {{}}), family: fontFamily, color: theme.text}}}};
    layout.hoverlabel = {{...(layout.hoverlabel || {{}}), bgcolor: theme.hover, bordercolor: theme.hoverBorder, font: {{...((layout.hoverlabel && layout.hoverlabel.font) || {{}}), family: fontFamily, color: theme.text}}}};
    const axes = axisNames(layout);
    if (hasCartesianTrace) {{
      if (!axes.includes("xaxis")) axes.push("xaxis");
      if (!axes.includes("yaxis")) axes.push("yaxis");
    }}
    for (const name of axes) {{
      const axis = layout[name] || {{}};
      const title = axis.title && typeof axis.title === "object" ? axis.title : {{text: axis.title}};
      layout[name] = {{
        ...axis,
        automargin: true,
        gridcolor: theme.grid,
        linecolor: theme.axis,
        zerolinecolor: theme.axis,
        tickfont: {{...(axis.tickfont || {{}}), family: fontFamily, color: theme.muted}},
        title: {{...title, font: {{...((title && title.font) || {{}}), family: fontFamily, color: theme.text}}}}
      }};
    }}
    const margin = layout.margin || {{}};
    const safeMargin = (value, minimum) => Number.isFinite(value) ? Math.max(minimum, value) : minimum;
    layout.margin = {{
      ...margin,
      l: safeMargin(margin.l, 48),
      r: safeMargin(margin.r, hasOverflowingLabels ? 48 : 32),
      t: safeMargin(margin.t, hasOverflowingLabels ? 36 : 18),
      b: safeMargin(margin.b, 48),
      autoexpand: true
    }};
    return axes;
  }};
  const applyPaletteToPlot = (target, layout, axes, theme) => {{
    const update = {{
      paper_bgcolor: layout.paper_bgcolor,
      plot_bgcolor: layout.plot_bgcolor,
      "font.family": layout.font.family,
      "font.color": layout.font.color,
      "legend.bgcolor": layout.legend.bgcolor,
      "legend.font.family": layout.legend.font.family,
      "legend.font.color": layout.legend.font.color,
      "hoverlabel.bgcolor": layout.hoverlabel.bgcolor,
      "hoverlabel.bordercolor": layout.hoverlabel.bordercolor,
      "hoverlabel.font.family": layout.hoverlabel.font.family,
      "hoverlabel.font.color": layout.hoverlabel.font.color
    }};
    for (const name of axes) {{
      update[name + ".gridcolor"] = theme.grid;
      update[name + ".linecolor"] = theme.axis;
      update[name + ".zerolinecolor"] = theme.axis;
      update[name + ".tickfont.family"] = fontFamily;
      update[name + ".tickfont.color"] = theme.muted;
      update[name + ".title.font.family"] = fontFamily;
      update[name + ".title.font.color"] = theme.text;
    }}
    return window.Plotly.relayout(target, update);
  }};
  const labelCanOverflow = trace => {{
    const type = trace.type || "scatter";
    const positions = Array.isArray(trace.textposition) ? trace.textposition : [trace.textposition];
    const labelsStayInside = positions.length > 0
      && positions.every(position => position === "inside" || position === "none");
    const hasLabels = trace.text !== undefined || trace.texttemplate !== undefined;
    return ["bar", "scatter", "scattergl"].includes(type) && hasLabels && !labelsStayInside;
  }};
  for (const [index, chart] of payload.charts.entries()) {{
    const target = card.querySelector('[data-chart-index="' + index + '"]');
    const figure = chart.figure;
    const layout = JSON.parse(JSON.stringify(figure.layout || {{}}));
    // 外層 h2 是唯一標題；移除複製出的 Plotly 標題，避免桌面重複顯示。
    delete layout.title;
    const usesRendererHeight = !Number.isFinite(layout.height) || layout.height <= 0;
    if (usesRendererHeight) layout.height = viewport.matches ? 300 : 340;
    const hasCartesianTrace = figure.data.some(trace => cartesianTypes.has(trace.type || "scatter"));
    const hasOverflowingLabels = figure.data.some(labelCanOverflow);
    const axes = applyPalette(layout, palette(), hasCartesianTrace, hasOverflowingLabels);
    const data = figure.data.map(trace => {{
      const copy = {{...trace}};
      if (labelCanOverflow(copy)) copy.cliponaxis = false;
      return copy;
    }});
    const hasPieLegend = figure.data.some(trace => trace.type === "pie")
      && layout.showlegend !== false;
    const applyViewportLayout = (plot, narrow) => {{
      const update = {{}};
      if (usesRendererHeight) update.height = narrow ? 300 : 340;
      if (hasPieLegend) {{
        const originalLegend = layout.legend || {{}};
        update["legend.orientation"] = narrow
          ? "h"
          : (originalLegend.orientation || "v");
        update["legend.x"] = narrow ? 0 : (originalLegend.x ?? null);
        update["legend.xanchor"] = narrow
          ? "left"
          : (originalLegend.xanchor ?? null);
        update["legend.y"] = narrow ? -0.12 : (originalLegend.y ?? null);
        update["legend.yanchor"] = narrow
          ? "top"
          : (originalLegend.yanchor ?? null);
        update["legend.font.size"] = narrow
          ? 10
          : (originalLegend.font && originalLegend.font.size) ?? null;
      }}
      if (Object.keys(update).length) window.Plotly.relayout(plot, update);
    }};
    window.Plotly.newPlot(target, data, layout, config).then(() => {{
      renderedCharts.push({{target, layout, axes, hasOverflowingLabels}});
      applyViewportLayout(target, viewport.matches);
      if (Array.isArray(figure.frames) && figure.frames.length) {{
        window.Plotly.addFrames(target, figure.frames);
      }}
    }}).catch(() => {{
      target.textContent = "此圖表無法呈現。";
    }});
  }}
  viewport.addEventListener("change", event => {{
    for (const {{target}} of renderedCharts) applyViewportLayout(target, event.matches);
  }});
  colorScheme.addEventListener("change", () => {{
    const theme = palette();
    for (const {{target, layout, axes, hasOverflowingLabels}} of renderedCharts) {{
      applyPalette(layout, theme, axes.length > 0, hasOverflowingLabels);
      applyPaletteToPlot(target, layout, axes, theme);
    }}
  }});
  const reportHeight = () => window.parent.postMessage({{
    type: "iframe:height",
    height: Math.ceil(document.documentElement.scrollHeight)
  }}, "*");
  window.addEventListener("load", reportHeight);
  if ("ResizeObserver" in window) {{
    new ResizeObserver(reportHeight).observe(card);
  }}
}})();
</script>
</body>
</html>"""
    if len(document.encode("utf-8")) > MAX_EMBED_HTML_BYTES:
        raise PlotlySpecError("Plotly Rich UI HTML 超過大小上限")
    return document


def _validate_plotly_figure(
    figure: dict[str, Any], *, native_validation: bool = True
) -> None:
    """拒絕外部資源 trace；若有安裝 Plotly 再執行原生 schema 驗證。"""

    traces = list(figure["data"])
    for frame in figure.get("frames", []):
        frame_data = frame.get("data", [])
        if isinstance(frame_data, list):
            traces.extend(trace for trace in frame_data if isinstance(trace, dict))
    if any(
        isinstance(trace.get("type"), str)
        and trace["type"].casefold() in _REMOTE_TRACE_TYPES
        for trace in traces
    ):
        raise PlotlySpecError("Plotly figure 不支援需要外部底圖的 trace")
    if native_validation:
        try:
            import plotly.io as pio

            pio.from_json(json.dumps(figure, ensure_ascii=False, allow_nan=False))
        except Exception as exc:
            raise PlotlySpecError("Plotly figure 不符合 Plotly JSON 契約") from exc


def _validate_json_tree(value: Any, *, node_count: list[int], depth: int = 0) -> None:
    if depth > MAX_PLOTLY_JSON_DEPTH:
        raise PlotlySpecError("Plotly figure 巢狀深度超出上限")
    node_count[0] += 1
    if node_count[0] > MAX_PLOTLY_JSON_NODES:
        raise PlotlySpecError("Plotly figure 節點數超出上限")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > MAX_PLOTLY_STRING_CHARS:
                raise PlotlySpecError("Plotly figure 欄位名稱無效")
            _validate_text_safety(key)
            _validate_json_tree(child, node_count=node_count, depth=depth + 1)
        return
    if isinstance(value, list):
        for child in value:
            _validate_json_tree(child, node_count=node_count, depth=depth + 1)
        return
    if isinstance(value, str):
        if len(value) > MAX_PLOTLY_STRING_CHARS:
            raise PlotlySpecError("Plotly figure 字串超出上限")
        _validate_text_safety(value)
        return
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if value.bit_length() > 128:
            raise PlotlySpecError("Plotly figure 數值超出範圍")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PlotlySpecError("Plotly figure 包含非有限數值")
        return
    raise PlotlySpecError("Plotly figure 只能包含標準 JSON 值")


def _reject_remote_or_markup_fields(value: Any, *, in_template: bool = False) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered_key = key.casefold()
            if lowered_key in _URL_FIELD_NAMES or (
                lowered_key in {"geo", "map", "mapbox"} and not in_template
            ):
                raise PlotlySpecError("Plotly figure 不支援外部資源欄位")
            _reject_remote_or_markup_fields(
                child,
                in_template=in_template or lowered_key == "template",
            )
    elif isinstance(value, list):
        for child in value:
            _reject_remote_or_markup_fields(child, in_template=in_template)


def _validate_text_safety(value: str) -> None:
    if any(
        _SAFE_PLOTLY_TAG_PATTERN.fullmatch(tag) is None
        for tag in _MARKUP_PATTERN.findall(value)
    ):
        raise PlotlySpecError("Plotly figure 含不支援的 HTML 標記")
    if _SCHEME_PATTERN.search(value) or re.search(r"(?i)\burl\s*\(", value):
        raise PlotlySpecError("Plotly figure 不接受外部 URL")


def _text(value: Any, where: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise PlotlySpecError(f"Plotly {where} 必須是文字")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > maximum:
        raise PlotlySpecError(f"Plotly {where} 長度無效")
    _validate_text_safety(normalized)
    return normalized


def _script_safe_json(value: Any) -> str:
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


def _unique_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PlotlySpecError("Plotly JSON 不可重複定義欄位")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise PlotlySpecError("Plotly JSON 不接受 NaN 或 Infinity")


__all__ = [
    "DEFAULT_PLOTLY_ASSET_URL",
    "MAX_PLOTLY_CHARTS",
    "MAX_PLOTLY_SPEC_BYTES",
    "PLOTLY_ASSET_PATH",
    "PLOTLY_CHARTS_FILE",
    "PLOTLY_SPEC_VERSION",
    "PLOTLY_JS_VERSION",
    "PLOTLY_VERSION",
    "PlotlySpecError",
    "ValidatedPlotlyCharts",
    "parse_plotly_charts_artifact",
    "render_plotly_charts_html",
    "validate_plotly_asset_url",
    "validate_plotly_charts",
]
