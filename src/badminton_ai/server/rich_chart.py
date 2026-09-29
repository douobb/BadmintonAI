"""由已驗證 chart spec 產生固定模板、無外部依賴的 Rich UI HTML。"""

from __future__ import annotations

import hashlib
import html
import json
import math
from typing import Any

from .chart_spec import (
    ChartSpecError,
    ValidatedChartSpec,
)

MAX_RICH_CHART_HTML_BYTES = 2 * 1024 * 1024
_COLORS = (
    "#159b74",
    "#7c6ee6",
    "#df8b27",
    "#2f83c5",
    "#d25b79",
    "#55a84f",
)
_FIXED_HEIGHT_SCRIPT = """<script>
(() => {
  const report = () => parent.postMessage({
    type: 'iframe:height',
    height: Math.ceil(document.documentElement.scrollHeight)
  }, '*');
  window.addEventListener('load', report);
  if ('ResizeObserver' in window) new ResizeObserver(report).observe(document.documentElement);
  requestAnimationFrame(report);
})();
</script>"""
_FIXED_SERIES_TOGGLE_SCRIPT = """<script>
(() => {
  document.querySelectorAll('[data-series-toggle]').forEach((button) => {
    button.addEventListener('click', () => {
      const index = button.dataset.seriesToggle;
      if (!/^\\d+$/.test(index || '')) return;
      const group = document.getElementById(`chart-series-${index}`);
      if (!group) return;
      const show = button.getAttribute('aria-pressed') !== 'true';
      button.setAttribute('aria-pressed', String(show));
      group.classList.toggle('series-hidden', !show);
    });
  });
})();
</script>"""


def render_chart_html(spec: ValidatedChartSpec) -> str:
    """把已驗證資料繪成安全 SVG、tooltip 與可存取資料表。"""

    if not isinstance(spec, ValidatedChartSpec):
        raise ChartSpecError("Rich UI renderer 僅接受已驗證 chart spec")
    fingerprint = chart_semantic_fingerprint(spec)
    chart_svg = _render_svg(spec)
    legend = _render_legend(spec)
    table = _render_table(spec)
    toggle_script = (
        _FIXED_SERIES_TOGGLE_SCRIPT
        if spec.chart_type in {"bar", "grouped_bar", "line", "radar"}
        and len(spec.data["series"]) > 1
        else ""
    )
    subtitle = (
        f'<p class="subtitle">{_escape(spec.subtitle)}</p>' if spec.subtitle else ""
    )
    summary = (
        f"樣本 {spec.sample_size:,} · 分母 {spec.denominator:,} · "
        f"排除 {spec.excluded_count:,}"
    )
    if spec.chart_type == "court_heatmap":
        data = spec.data
        summary += (
            f" · 出界 {data['out_of_court_count']:,}"
            f" · 未定義 {data['undefined_count']:,}"
            f" · 缺失 {data['missing_count']:,}"
        )
    unit = f"（{_escape(spec.unit)}）" if spec.unit else ""
    x_label = f"<span>{_escape(spec.x_label)}</span>" if spec.x_label else ""
    if spec.chart_type == "histogram":
        density_unit = f"（{_escape(spec.unit)}／數值單位）" if spec.unit else ""
        y_label = f"<span>Y 軸：頻數密度{density_unit}</span>"
    else:
        y_label = f"<span>{_escape(spec.y_label)}</span>" if spec.y_label else ""
    document = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="badmintonai-chart-fingerprint" content="sha256:{fingerprint}">
<title>{_escape(spec.title)}</title>
<style>
:root {{ color-scheme: light dark; font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  --bg: #ffffff; --fg: #202a2b; --muted: #667477; --grid: #d7e0df; --border: #dce4e3;
  --surface: #f5f8f7; --accent: #159b74; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #0b1d19; --fg: #e5efec; --muted: #a5b7b1; --grid: #344b45;
    --border: #29443c; --surface: #102720; --accent: #24b888; }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 10px; color: var(--fg); background: transparent; font-size: 14px; }}
.card {{ width: 100%; border: 1px solid var(--border); border-radius: 16px; padding: 16px;
  background: var(--bg); }}
h1 {{ margin: 0 0 5px; font-size: clamp(18px, 2.5vw, 24px); line-height: 1.25; }}
.subtitle {{ margin: 0 0 10px; color: var(--muted); }}
.summary {{ display: flex; flex-wrap: wrap; gap: 4px 12px; margin: 0 0 12px; color: var(--muted); }}
.legend {{ display: flex; flex-wrap: wrap; gap: 8px 16px; margin: 6px 0 10px; color: var(--muted); }}
.legend-item {{ display: inline-flex; align-items: center; gap: 6px; }}
.legend-toggle {{ border: 1px solid var(--border); border-radius: 8px; padding: 5px 8px;
  color: var(--fg); background: var(--surface); font: inherit; cursor: pointer; }}
.legend-toggle:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
.legend-toggle[aria-pressed="false"] {{ opacity: .48; text-decoration: line-through; }}
.swatch {{ width: 12px; height: 12px; border-radius: 3px; }}
.plot {{ width: 100%; overflow-x: auto; overscroll-behavior-inline: contain; }}
svg {{ display: block; width: 100%; height: auto; min-width: min(100%, 640px); overflow: visible; }}
svg text {{ fill: var(--muted); font: 12px system-ui, sans-serif; }}
svg .axis {{ stroke: var(--grid); stroke-width: 1; }}
svg .series-mark {{ cursor: help; outline: none; }}
svg .series-mark:focus {{ stroke: var(--fg); stroke-width: 3px; }}
svg .series-hidden {{ display: none; }}
.axis-labels {{ display: flex; justify-content: space-between; color: var(--muted); font-size: 12px; }}
details {{ margin-top: 12px; border-top: 1px solid var(--border); padding-top: 10px; }}
summary {{ width: fit-content; cursor: pointer; color: var(--accent); font-weight: 600; }}
.table-wrap {{ overflow: auto; max-height: 320px; margin-top: 8px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
caption {{ text-align: left; padding: 4px 0 8px; color: var(--muted); }}
th, td {{ border-bottom: 1px solid var(--border); padding: 6px 8px; text-align: right; }}
th:first-child, td:first-child {{ text-align: left; }}
thead {{ position: sticky; top: 0; background: var(--surface); }}
th {{ color: var(--muted); }}
.sr-only {{ position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }}
</style></head><body><main class="card">
<h1>{_escape(spec.title)}</h1>{subtitle}
<p class="summary"><span>{summary}</span><span>指標：{_escape(spec.measure)}{unit}</span>
{x_label}{y_label}</p>
{legend}<div class="plot">{chart_svg}</div>{table}
</main>{toggle_script}{_FIXED_HEIGHT_SCRIPT}</body></html>"""
    try:
        size = len(document.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ChartSpecError("Rich UI HTML 無法編碼") from exc
    if size > MAX_RICH_CHART_HTML_BYTES:
        raise ChartSpecError("Rich UI HTML 超過大小上限")
    return document


def chart_semantic_fingerprint(spec: ValidatedChartSpec) -> str:
    """以完整可見語意計算穩定 SHA-256 指紋供聊天重複圖表抑制。"""

    if not isinstance(spec, ValidatedChartSpec):
        raise ChartSpecError("圖表指紋只接受已驗證 chart spec")
    semantic_payload = {
        "schema_version": spec.schema_version,
        "chart_type": spec.chart_type,
        "title": spec.title,
        "subtitle": spec.subtitle,
        "sample_size": spec.sample_size,
        "denominator": spec.denominator,
        "excluded_count": spec.excluded_count,
        "measure": spec.measure,
        "unit": spec.unit,
        "x_label": spec.x_label,
        "y_label": spec.y_label,
        "data": _canonicalize_fingerprint_value(spec.data),
    }
    serialized = json.dumps(
        semantic_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _canonicalize_fingerprint_value(value: Any) -> Any:
    """讓整數型浮點與整數採同一表示，其他值依結構排序序列化。"""

    if isinstance(value, dict):
        return {
            key: _canonicalize_fingerprint_value(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_canonicalize_fingerprint_value(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ChartSpecError("圖表指紋資料必須為有限數值")
        if value.is_integer():
            return int(value)
    return value


def _render_svg(spec: ValidatedChartSpec) -> str:
    renderers = {
        "bar": _render_bars,
        "grouped_bar": _render_bars,
        "stacked_bar": _render_bars,
        "line": _render_line,
        "radar": _render_radar,
        "histogram": _render_histogram,
        "donut": _render_donut,
        "scatter": _render_scatter,
        "boxplot": _render_boxplot,
        "matrix_heatmap": _render_matrix_heatmap,
        "court_heatmap": _render_court_heatmap,
    }
    try:
        return renderers[spec.chart_type](spec)
    except (KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
        raise ChartSpecError("Rich UI 圖表無法安全繪製") from exc


def _render_bars(spec: ValidatedChartSpec) -> str:
    categories = spec.data["categories"]
    series = spec.data["series"]
    width = max(760, 220 + len(categories) * 18)
    row_height = 38
    top = 28
    bottom = 44
    height = top + row_height * len(categories) + bottom
    left, right = min(245, max(150, width // 4)), width - 28
    plot_width = right - left
    if spec.chart_type == "stacked_bar":
        category_totals = [
            [float(item["values"][row]) for item in series]
            for row in range(len(categories))
        ]
        minimum = min(
            0.0,
            min((sum(value for value in row if value < 0) for row in category_totals)),
        )
        maximum = max(
            1.0,
            max((sum(value for value in row if value > 0) for row in category_totals)),
        )
    else:
        values = [value for item in series for value in item["values"]]
        minimum = min(0.0, float(min(values)))
        maximum = max(1.0, float(max(values)))
    if math.isclose(minimum, maximum):
        maximum = minimum + 1
    zero_x = left + (0 - minimum) / (maximum - minimum) * plot_width
    pieces = [_svg_open(width, height, f"{spec.title} 長條圖", fixed_css_width=True)]
    for tick in range(6):
        value = minimum + (maximum - minimum) * tick / 5
        x = left + plot_width * tick / 5
        pieces.append(
            f'<line class="axis" x1="{_n(x)}" y1="{top - 6}" x2="{_n(x)}" y2="{height - bottom + 4}"/>'
        )
        pieces.append(
            _svg_text(x, height - 15, _fmt(value, spec.measure), anchor="middle")
        )
    for row, category in enumerate(categories):
        y_center = top + row_height * row + row_height / 2
        label_limit = max(5, (left - 24) // 12)
        axis_label = _truncate_label(category, label_limit)
        pieces.append(
            _svg_text(left - 12, y_center, axis_label, anchor="end", baseline="middle")
        )

    if spec.chart_type != "stacked_bar" and len(series) > 1:
        band = row_height * 0.72 / len(series)
        for series_index, item in enumerate(series):
            pieces.append(_series_group_open(series_index, item["name"]))
            for row, category in enumerate(categories):
                y_center = top + row_height * row + row_height / 2
                value = float(item["values"][row])
                x_value = left + (value - minimum) / (maximum - minimum) * plot_width
                x = min(zero_x, x_value)
                bar_width = max(0.4, abs(x_value - zero_x))
                y = y_center - row_height * 0.36 + series_index * band
                pieces.append(
                    _mark_rect(
                        x,
                        y,
                        bar_width,
                        band * 0.84,
                        _COLORS[series_index],
                        _point_description(spec, item, row, category),
                    )
                )
            pieces.append("</g>")
    else:
        for row, category in enumerate(categories):
            y_center = top + row_height * row + row_height / 2
            if spec.chart_type == "stacked_bar":
                positive_start = zero_x
                negative_start = zero_x
                band = row_height * 0.67
                for series_index, item in enumerate(series):
                    value = float(item["values"][row])
                    x_value = (
                        left + (value - minimum) / (maximum - minimum) * plot_width
                    )
                    if value >= 0:
                        x = positive_start
                        bar_width = max(0.4, x_value - zero_x)
                        positive_start += bar_width
                    else:
                        bar_width = max(0.4, zero_x - x_value)
                        x = negative_start - bar_width
                        negative_start = x
                    pieces.append(
                        _mark_rect(
                            x,
                            y_center - band / 2,
                            bar_width,
                            band,
                            _COLORS[series_index],
                            _point_description(spec, item, row, category),
                        )
                    )
            else:
                item = series[0]
                value = float(item["values"][row])
                x_value = left + (value - minimum) / (maximum - minimum) * plot_width
                x = min(zero_x, x_value)
                bar_width = max(0.4, abs(x_value - zero_x))
                y = y_center - row_height * 0.36
                pieces.append(
                    _mark_rect(
                        x,
                        y,
                        bar_width,
                        row_height * 0.6,
                        _COLORS[0],
                        _point_description(spec, item, row, category),
                    )
                )
    pieces.append("</svg>")
    return "".join(pieces)


def _render_line(spec: ValidatedChartSpec) -> str:
    categories = spec.data["categories"]
    series = spec.data["series"]
    width = max(820, 130 + len(categories) * 64)
    height = 410
    left, top, right, bottom = 66, 28, width - 24, height - 70
    values = [float(value) for item in series for value in item["values"]]
    minimum = min(0.0, min(values))
    maximum = max(1.0, max(values))
    if math.isclose(minimum, maximum):
        maximum = minimum + 1
    pieces = [_svg_open(width, height, f"{spec.title} 折線圖", fixed_css_width=True)]
    for tick in range(6):
        value = minimum + (maximum - minimum) * tick / 5
        y = bottom - (value - minimum) / (maximum - minimum) * (bottom - top)
        pieces.append(
            f'<line class="axis" x1="{left}" y1="{_n(y)}" x2="{right}" y2="{_n(y)}"/>'
        )
        pieces.append(
            _svg_text(
                left - 8, y, _fmt(value, spec.measure), anchor="end", baseline="middle"
            )
        )
    for index, label in enumerate(categories):
        x = left + (right - left) * index / (len(categories) - 1)
        pieces.append(_svg_text(x, bottom + 25, label, anchor="middle"))
    for series_index, item in enumerate(series):
        pieces.append(_series_group_open(series_index, item["name"]))
        coords: list[tuple[float, float]] = []
        for index, value in enumerate(item["values"]):
            x = left + (right - left) * index / (len(categories) - 1)
            y = bottom - (float(value) - minimum) / (maximum - minimum) * (bottom - top)
            coords.append((x, y))
        point_list = " ".join(f"{_n(x)},{_n(y)}" for x, y in coords)
        pieces.append(
            f'<polyline points="{point_list}" fill="none" '
            f'stroke="{_COLORS[series_index]}" stroke-width="3"/>'
        )
        for index, (x, y) in enumerate(coords):
            pieces.append(
                _mark_circle(
                    x,
                    y,
                    5,
                    _COLORS[series_index],
                    _point_description(spec, item, index, categories[index]),
                )
            )
        pieces.append("</g>")
    pieces.append("</svg>")
    return "".join(pieces)


def _render_radar(spec: ValidatedChartSpec) -> str:
    categories = spec.data["categories"]
    series = spec.data["series"]
    values = [float(value) for item in series for value in item["values"]]
    minimum = min(0.0, min(values))
    maximum = max(0.0, max(values))
    if math.isclose(minimum, maximum):
        maximum = minimum + 1

    width, height = 720, 560
    center_x, center_y, radius = 360.0, 280.0, 190.0
    angle_start = -math.pi / 2
    angle_step = 2 * math.pi / len(categories)

    def point_at(index: int, point_radius: float) -> tuple[float, float]:
        angle = angle_start + angle_step * index
        return (
            center_x + point_radius * math.cos(angle),
            center_y + point_radius * math.sin(angle),
        )

    pieces = [_svg_open(width, height, f"{spec.title} 雷達圖", fixed_css_width=True)]
    for tick in range(1, 6):
        ring_radius = radius * tick / 5
        ring_points = [point_at(index, ring_radius) for index in range(len(categories))]
        point_text = " ".join(
            f"{_n(x)},{_n(y)}" for x, y in [*ring_points, ring_points[0]]
        )
        pieces.append(f'<polygon class="axis" points="{point_text}" fill="none"/>')
        tick_value = minimum + (maximum - minimum) * tick / 5
        pieces.append(
            _svg_text(
                center_x + 7,
                center_y - ring_radius - 3,
                _fmt(tick_value, spec.measure),
                baseline="middle",
            )
        )

    for index, category in enumerate(categories):
        x, y = point_at(index, radius)
        pieces.append(
            f'<line class="axis" x1="{_n(center_x)}" y1="{_n(center_y)}" '
            f'x2="{_n(x)}" y2="{_n(y)}"/>'
        )
        label_x, label_y = point_at(index, radius + 30)
        cosine = math.cos(angle_start + angle_step * index)
        anchor = "middle" if abs(cosine) < 0.25 else "end" if cosine < 0 else "start"
        label = _truncate_label(category, 14)
        pieces.append(
            _svg_text(label_x, label_y, label, anchor=anchor, baseline="middle")
        )

    value_range = maximum - minimum
    for series_index, item in enumerate(series):
        pieces.append(_series_group_open(series_index, item["name"]))
        coordinates = [
            point_at(
                index,
                radius * (float(value) - minimum) / value_range,
            )
            for index, value in enumerate(item["values"])
        ]
        polygon_points = " ".join(
            f"{_n(x)},{_n(y)}" for x, y in [*coordinates, coordinates[0]]
        )
        series_description = " · ".join(
            _point_description(spec, item, index, categories[index])
            for index in range(len(categories))
        )
        color = _COLORS[series_index]
        pieces.append(
            f'<polygon class="series-mark" tabindex="0" role="img" '
            f'aria-label="{_escape(series_description)}" points="{polygon_points}" '
            f'fill="{color}" fill-opacity="0.18" stroke="{color}" stroke-width="2">'
            f"<title>{_escape(series_description)}</title></polygon>"
        )
        for index, (x, y) in enumerate(coordinates):
            pieces.append(
                _mark_circle(
                    x,
                    y,
                    5,
                    color,
                    _point_description(spec, item, index, categories[index]),
                )
            )
        pieces.append("</g>")
    pieces.append("</svg>")
    return "".join(pieces)


def _render_histogram(spec: ValidatedChartSpec) -> str:
    bins = spec.data["bins"]
    width = max(780, 180 + len(bins) * 42)
    height = 390
    left, top, right, bottom = 66, 26, width - 24, height - 74
    minimum_value = float(min(item["lower"] for item in bins))
    maximum_value = float(max(item["upper"] for item in bins))
    value_range = maximum_value - minimum_value
    if not math.isfinite(value_range) or value_range <= 0:
        raise ChartSpecError("histogram 數值範圍無法繪製")
    densities = [
        item["count"] / (float(item["upper"]) - float(item["lower"])) for item in bins
    ]
    if any(not math.isfinite(density) for density in densities):
        raise ChartSpecError("histogram 頻數密度超出可繪製範圍")
    maximum_density = max(densities)
    display_maximum = maximum_density if maximum_density > 0 else 1.0
    plot_height = bottom - top
    plot_width = right - left
    pieces = [_svg_open(width, height, f"{spec.title} 直方圖")]
    for tick in range(6):
        density = display_maximum * tick / 5
        y = bottom - plot_height * tick / 5
        pieces.append(
            f'<line class="axis" x1="{left}" y1="{_n(y)}" x2="{right}" y2="{_n(y)}"/>'
        )
        pieces.append(
            _svg_text(
                left - 8, y, _fmt(density, "value"), anchor="end", baseline="middle"
            )
        )
    pieces.append(
        f'<text x="16" y="{_n((top + bottom) / 2)}" '
        f'transform="rotate(-90 16 {_n((top + bottom) / 2)})" '
        'text-anchor="middle" fill="var(--muted)" font-size="12">頻數密度</text>'
    )
    for tick in range(6):
        value = minimum_value + value_range * tick / 5
        x = left + plot_width * tick / 5
        pieces.append(
            f'<line class="axis" x1="{_n(x)}" y1="{top}" x2="{_n(x)}" y2="{bottom}"/>'
        )
        pieces.append(_svg_text(x, bottom + 20, _fmt(value, "value"), anchor="middle"))
    pieces.append(
        f'<line class="axis" x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}"/>'
    )
    for item, density in zip(bins, densities, strict=True):
        x = left + (float(item["lower"]) - minimum_value) / value_range * plot_width
        x_end = left + (float(item["upper"]) - minimum_value) / value_range * plot_width
        bar_width = x_end - x
        if bar_width <= 0:
            raise ChartSpecError("histogram bin 寬度無法繪製")
        bar_height = plot_height * density / display_maximum
        description = (
            f"{item['label']}（{_fmt(item['lower'], 'value')}–"
            f"{_fmt(item['upper'], 'value')}）：{item['count']:,} {spec.unit}；"
            f"頻數密度 {_fmt(density, 'value')}"
        ).strip()
        if item["count"] == 0:
            # 零計數沒有可見的矩形高度，以可聚焦基線點保留其可辨識性。
            pieces.append(
                _mark_circle(
                    (x + x_end) / 2,
                    bottom,
                    3,
                    _COLORS[0],
                    description,
                )
            )
            continue
        pieces.append(
            _mark_rect(
                x,
                bottom - bar_height,
                bar_width,
                bar_height,
                _COLORS[0],
                description,
                stroke="var(--bg)",
            )
        )
    pieces.append("</svg>")
    return "".join(pieces)


def _render_donut(spec: ValidatedChartSpec) -> str:
    categories = spec.data["categories"]
    series = spec.data["series"][0]
    values = series["values"]
    if spec.measure == "percent":
        counts = series["numerators"]
        total = series["denominator"]
    else:
        counts = values
        total = sum(counts)
    width, height = 900, 460
    cx, cy, outer, inner = 245, 230, 160, 88
    angle = -math.pi / 2
    pieces = [_svg_open(width, height, f"{spec.title} 環形圖")]
    for index, (label, count) in enumerate(zip(categories, counts, strict=True)):
        sweep = (float(count) / total) * 2 * math.pi
        path = _donut_path(cx, cy, outer, inner, angle, angle + sweep)
        pct = float(count) * 100 / total
        pieces.append(
            f'<path class="series-mark" tabindex="0" role="img" '
            f'aria-label="{_escape(label)}：{_fmt(float(count), "count")}，{pct:.1f}%" '
            f'd={path} fill="{_COLORS[index % len(_COLORS)]}">'
            f"<title>{_escape(label)}：{_fmt(float(count), 'count')} / {total:,}，{pct:.1f}%</title></path>"
        )
        angle += sweep
    pieces.append(
        _svg_text(cx, cy - 4, f"{total:,}", anchor="middle", baseline="middle", size=22)
    )
    pieces.append(_svg_text(cx, cy + 20, "分母", anchor="middle", baseline="middle"))
    pieces.append("</svg>")
    return "".join(pieces)


def _donut_path(
    cx: float, cy: float, outer: float, inner: float, start: float, end: float
) -> str:
    if end - start >= 2 * math.pi - 1e-8:
        end = start + 2 * math.pi - 1e-8
    x1 = cx + outer * math.cos(start)
    y1 = cy + outer * math.sin(start)
    x2 = cx + outer * math.cos(end)
    y2 = cy + outer * math.sin(end)
    ix2 = cx + inner * math.cos(end)
    iy2 = cy + inner * math.sin(end)
    ix1 = cx + inner * math.cos(start)
    iy1 = cy + inner * math.sin(start)
    large = 1 if end - start > math.pi else 0
    return (
        f"M {_n(x1)} {_n(y1)} A {outer} {outer} 0 {large} 1 {_n(x2)} {_n(y2)} "
        f"L {_n(ix2)} {_n(iy2)} A {inner} {inner} 0 {large} 0 {_n(ix1)} {_n(iy1)} Z"
    )


def _render_scatter(spec: ValidatedChartSpec) -> str:
    points = spec.data["points"]
    width, height = 920, 480
    left, top, right, bottom = 76, 24, width - 26, height - 72
    xs = [float(point["x"]) for point in points]
    ys = [float(point["y"]) for point in points]
    min_x, max_x = _padded_range(min(xs), max(xs))
    min_y, max_y = _padded_range(min(ys), max(ys))
    pieces = [_svg_open(width, height, f"{spec.title} 數值散佈圖")]
    for tick in range(6):
        ratio = tick / 5
        x = left + ratio * (right - left)
        y = bottom - ratio * (bottom - top)
        xv = min_x + ratio * (max_x - min_x)
        yv = min_y + ratio * (max_y - min_y)
        pieces.append(
            f'<line class="axis" x1="{_n(x)}" y1="{top}" x2="{_n(x)}" y2="{bottom}"/>'
        )
        pieces.append(
            f'<line class="axis" x1="{left}" y1="{_n(y)}" x2="{right}" y2="{_n(y)}"/>'
        )
        pieces.append(_svg_text(x, bottom + 20, _fmt(xv, "value"), anchor="middle"))
        pieces.append(
            _svg_text(left - 8, y, _fmt(yv, "value"), anchor="end", baseline="middle")
        )
    groups = sorted({point.get("group", "資料點") for point in points})
    for index, group in enumerate(groups):
        pieces.append(
            f'<g aria-label="{_escape(group)}" fill="{_COLORS[index % len(_COLORS)]}">'
        )
        for point in points:
            if point.get("group", "資料點") != group:
                continue
            x = left + (float(point["x"]) - min_x) / (max_x - min_x) * (right - left)
            y = bottom - (float(point["y"]) - min_y) / (max_y - min_y) * (bottom - top)
            label = point.get("label", group)
            pieces.append(
                _mark_circle(
                    x,
                    y,
                    4,
                    _COLORS[index % len(_COLORS)],
                    f"{label} · {spec.x_label}={_fmt(point['x'], 'value')} · "
                    f"{spec.y_label}={_fmt(point['y'], 'value')}",
                )
            )
        pieces.append("</g>")
    pieces.append("</svg>")
    return "".join(pieces)


def _render_boxplot(spec: ValidatedChartSpec) -> str:
    groups = spec.data["groups"]
    width = 920
    row_height = 62
    height = 60 + row_height * len(groups)
    left, right = 190, width - 28
    low = min(item["min"] for item in groups)
    high = max(item["max"] for item in groups)
    minimum, maximum = _padded_range(float(low), float(high))
    pieces = [_svg_open(width, height, f"{spec.title} 箱型圖")]
    for tick in range(6):
        ratio = tick / 5
        x = left + ratio * (right - left)
        value = minimum + ratio * (maximum - minimum)
        pieces.append(
            f'<line class="axis" x1="{_n(x)}" y1="20" x2="{_n(x)}" y2="{height - 28}"/>'
        )
        pieces.append(_svg_text(x, height - 9, _fmt(value, "value"), anchor="middle"))
    for index, group in enumerate(groups):
        y = 38 + row_height * index
        pieces.append(
            _svg_text(left - 12, y, group["label"], anchor="end", baseline="middle")
        )

        def map_x(value: float) -> float:
            return left + (float(value) - minimum) / (maximum - minimum) * (
                right - left
            )

        x_min, x_q1, x_med, x_q3, x_max = (
            map_x(group[key]) for key in ("min", "q1", "median", "q3", "max")
        )
        pieces.append(
            f'<line class="axis" x1="{_n(x_min)}" y1="{y}" x2="{_n(x_max)}" y2="{y}"/>'
        )
        pieces.append(
            f'<line class="axis" x1="{_n(x_min)}" y1="{y - 8}" x2="{_n(x_min)}" y2="{y + 8}"/>'
        )
        pieces.append(
            f'<line class="axis" x1="{_n(x_max)}" y1="{y - 8}" x2="{_n(x_max)}" y2="{y + 8}"/>'
        )
        pieces.append(
            _mark_rect(
                x_q1,
                y - 14,
                max(2, x_q3 - x_q1),
                28,
                _COLORS[index % len(_COLORS)],
                f"{group['label']}：Q1 {group['q1']}、中位數 {group['median']}、Q3 {group['q3']}、n={group['count']}",
                opacity="0.62",
            )
        )
        pieces.append(
            f'<line class="series-mark" tabindex="0" x1="{_n(x_med)}" y1="{y - 14}" x2="{_n(x_med)}" y2="{y + 14}" stroke="var(--fg)" stroke-width="3"><title>中位數 {_fmt(group["median"], "value")}</title></line>'
        )
        for outlier in group["outliers"]:
            x = map_x(outlier)
            pieces.append(
                _mark_circle(
                    x,
                    y,
                    3,
                    _COLORS[index % len(_COLORS)],
                    f"{group['label']} 離群值 {_fmt(outlier, 'value')}",
                )
            )
    pieces.append("</svg>")
    return "".join(pieces)


def _render_matrix_heatmap(spec: ValidatedChartSpec) -> str:
    xs = spec.data["x_categories"]
    ys = spec.data["y_categories"]
    values = spec.data["values"]
    cell_width, cell_height = 62, 36
    left, top = max(140, max(len(item) for item in ys) * 9 + 24), 60
    width = left + cell_width * len(xs) + 20
    height = top + cell_height * len(ys) + 54
    maximum = max(1, max(max(row) for row in values))
    pieces = [_svg_open(width, height, f"{spec.title} 類別矩陣熱圖")]
    for x_index, label in enumerate(xs):
        x = left + cell_width * (x_index + 0.5)
        pieces.append(_svg_text(x, top - 16, label, anchor="middle"))
    for y_index, label in enumerate(ys):
        y = top + cell_height * (y_index + 0.5)
        pieces.append(_svg_text(left - 12, y, label, anchor="end", baseline="middle"))
        for x_index, count in enumerate(values[y_index]):
            x = left + cell_width * x_index
            color = _heat_color(count, maximum)
            title = f"{label} × {xs[x_index]}：{count:,} {spec.unit}".strip()
            pieces.append(
                _mark_rect(
                    x,
                    top + cell_height * y_index,
                    cell_width - 2,
                    cell_height - 2,
                    color,
                    title,
                    stroke="var(--border)",
                )
            )
            pieces.append(
                _svg_text(
                    x + cell_width / 2,
                    y,
                    f"{count:,}",
                    anchor="middle",
                    baseline="middle",
                    fill="var(--fg)",
                )
            )
    pieces.append("</svg>")
    return "".join(pieces)


def _render_court_heatmap(spec: ValidatedChartSpec) -> str:
    data = spec.data
    mapping = data["zone_mapping"]
    counts = {item["code"]: item["count"] for item in data["zones"]}
    maximum = max(1, max(counts.values(), default=0))
    width, height = 700, 560
    left, top, cell_width, cell_height = 210, 48, 92, 64
    pieces = [_svg_open(width, height, f"{spec.title} 官方場區熱圖")]
    for column_index, column in enumerate(("A", "B", "C", "D")):
        x = left + cell_width * (column_index + 0.5)
        pieces.append(_svg_text(x, top - 18, column, anchor="middle"))
    pieces.append(
        _svg_text(left + 2 * cell_width, top - 36, "官方 6×4 場內格位", anchor="middle")
    )
    for code in range(1, 25):
        zone = mapping[code]
        row = zone["court_row"] - 1
        column_index = ord(zone["court_column"]) - ord("A")
        x = left + column_index * cell_width
        y = top + row * cell_height
        count = counts.get(code, 0)
        pct = count * 100 / spec.denominator
        title = (
            f"場區 {code} · {zone['category']} {zone['position']}："
            f"{count:,} / {spec.denominator:,}（{pct:.1f}%）"
        )
        pieces.append(
            _mark_rect(
                x,
                y,
                cell_width - 3,
                cell_height - 3,
                _heat_color(count, maximum),
                title,
                stroke="var(--border)",
            )
        )
        pieces.append(
            _svg_text(
                x + cell_width / 2,
                y + 25,
                f"{code} · {count:,}",
                anchor="middle",
                baseline="middle",
                fill="var(--fg)",
            )
        )
        pieces.append(
            _svg_text(
                x + cell_width / 2,
                y + 45,
                f"{pct:.1f}%",
                anchor="middle",
                baseline="middle",
            )
        )
    pieces.append("</svg>")
    return "".join(pieces)


def _render_legend(spec: ValidatedChartSpec) -> str:
    entries: list[tuple[str, int]] = []
    if spec.chart_type in {"bar", "grouped_bar", "stacked_bar", "line", "radar"}:
        entries = [
            (item["name"], item.get("denominator", spec.denominator))
            for item in spec.data["series"]
        ]
    elif spec.chart_type == "donut":
        entries = [(label, spec.denominator) for label in spec.data["categories"]]
    elif spec.chart_type == "scatter":
        entries = [
            (group, spec.denominator)
            for group in sorted(
                {point.get("group", "資料點") for point in spec.data["points"]}
            )
        ]
    elif spec.chart_type == "boxplot":
        entries = [(item["label"], item["count"]) for item in spec.data["groups"]]
    if len(entries) <= 1:
        return ""
    interactive = spec.chart_type in {"bar", "grouped_bar", "line", "radar"}
    pieces = ['<div class="legend" aria-label="圖例">']
    for index, (name, count) in enumerate(entries):
        suffix = f"（n={count:,}）" if spec.measure == "percent" else ""
        color = _COLORS[index % len(_COLORS)]
        swatch = f'<span class="swatch" style="background:{color}" aria-hidden="true"></span>'
        if interactive:
            pieces.append(
                f'<button type="button" class="legend-item legend-toggle" '
                f'data-series-toggle="{index}" aria-pressed="true" '
                f'aria-label="切換系列：{_escape(name)}">'
                f"{swatch}{_escape(name)}{suffix}</button>"
            )
        else:
            pieces.append(
                f'<span class="legend-item">{swatch}{_escape(name)}{suffix}</span>'
            )
    pieces.append("</div>")
    return "".join(pieces)


def _render_table(spec: ValidatedChartSpec) -> str:
    headers: list[str] = []
    rows: list[list[str]] = []
    data = spec.data
    if spec.chart_type in {"bar", "grouped_bar", "stacked_bar", "line", "radar"}:
        headers = ["類別"] + [
            f"{item['name']}（n={item['denominator']:,}）"
            if "denominator" in item
            else item["name"]
            for item in data["series"]
        ]
        for row_index, category in enumerate(data["categories"]):
            values = []
            for series in data["series"]:
                value = series["values"][row_index]
                if spec.measure == "percent":
                    numerator = series["numerators"][row_index]
                    values.append(
                        f"{value:.2f}%（{numerator:,}/{series['denominator']:,}）"
                    )
                else:
                    values.append(_fmt(value, spec.measure))
            rows.append([category, *values])
    elif spec.chart_type == "histogram":
        headers = ["區間", "下界", "上界", "樣本數"]
        rows = [
            [
                item["label"],
                _fmt(item["lower"], "value"),
                _fmt(item["upper"], "value"),
                f"{item['count']:,}",
            ]
            for item in data["bins"]
        ]
    elif spec.chart_type == "donut":
        headers = ["類別", "樣本數", "比例"]
        series = data["series"][0]
        if spec.measure == "percent":
            for label, value, numerator in zip(
                data["categories"], series["values"], series["numerators"], strict=True
            ):
                rows.append(
                    [
                        label,
                        f"{numerator:,} / {series['denominator']:,}",
                        f"{value:.2f}%",
                    ]
                )
        else:
            for label, count in zip(data["categories"], series["values"], strict=True):
                rows.append(
                    [label, f"{count:,}", f"{count * 100 / spec.denominator:.2f}%"]
                )
    elif spec.chart_type == "scatter":
        headers = ["標籤", spec.x_label, spec.y_label, "群組"]
        rows = [
            [
                item.get("label", f"點 {index + 1}"),
                _fmt(item["x"], "value"),
                _fmt(item["y"], "value"),
                item.get("group", "資料點"),
            ]
            for index, item in enumerate(data["points"])
        ]
    elif spec.chart_type == "boxplot":
        headers = ["群組", "n", "最小值", "Q1", "中位數", "Q3", "最大值"]
        rows = [
            [
                item["label"],
                f"{item['count']:,}",
                *[
                    _fmt(item[key], "value")
                    for key in ("min", "q1", "median", "q3", "max")
                ],
            ]
            for item in data["groups"]
        ]
    elif spec.chart_type == "matrix_heatmap":
        headers = [spec.y_label or "類別", *data["x_categories"]]
        rows = [
            [label, *[f"{value:,}" for value in values]]
            for label, values in zip(data["y_categories"], data["values"], strict=True)
        ]
    elif spec.chart_type == "court_heatmap":
        headers = ["場區代碼", "前中後場", "位置", "樣本數", "比例"]
        mapping = data["zone_mapping"]
        counts = {item["code"]: item["count"] for item in data["zones"]}
        rows = [
            [
                str(code),
                mapping[code]["category"],
                mapping[code]["position"],
                f"{counts.get(code, 0):,}",
                f"{counts.get(code, 0) * 100 / spec.denominator:.2f}%",
            ]
            for code in range(1, 25)
        ]
    header_html = "".join(f'<th scope="col">{_escape(item)}</th>' for item in headers)
    row_html = "".join(
        "<tr>" + "".join(f"<td>{_escape(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return (
        f'<details><summary>資料表（{len(rows):,} 列）</summary><div class="table-wrap">'
        f"<table><caption>{_escape(spec.title)}；樣本 {spec.sample_size:,}、分母 {spec.denominator:,}</caption>"
        f"<thead><tr>{header_html}</tr></thead><tbody>{row_html}</tbody></table></div></details>"
    )


def _svg_open(
    width: int,
    height: int,
    accessible_name: str,
    *,
    fixed_css_width: bool = False,
) -> str:
    css_width = (
        f' style="width:{width}px;min-width:{width}px;max-width:none"'
        if fixed_css_width
        else ""
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{_escape(accessible_name)}" preserveAspectRatio="xMinYMin meet"{css_width}>'
        f"<title>{_escape(accessible_name)}</title>"
    )


def _series_group_open(index: int, label: str) -> str:
    """以伺服器序號建立 group；固定互動腳本只依序號切換可見性。"""

    return (
        f'<g id="chart-series-{index}" data-series-index="{index}" '
        f'role="group" aria-label="{_escape(label)}">'
    )


def _svg_text(
    x: float,
    y: float,
    value: str,
    *,
    anchor: str = "start",
    baseline: str = "auto",
    size: int = 12,
    fill: str = "var(--muted)",
) -> str:
    return (
        f'<text x="{_n(x)}" y="{_n(y)}" text-anchor="{anchor}" '
        f'dominant-baseline="{baseline}" font-size="{size}" fill="{fill}">'
        f"{_escape(value)}</text>"
    )


def _mark_rect(
    x: float,
    y: float,
    width: float,
    height: float,
    fill: str,
    description: str,
    *,
    opacity: str = "1",
    stroke: str = "none",
) -> str:
    return (
        f'<rect class="series-mark" tabindex="0" role="img" '
        f'aria-label="{_escape(description)}" x="{_n(x)}" y="{_n(y)}" '
        f'width="{_n(width)}" height="{_n(height)}" rx="3" fill="{fill}" '
        f'opacity="{opacity}" stroke="{stroke}"><title>{_escape(description)}</title></rect>'
    )


def _mark_circle(
    x: float,
    y: float,
    radius: float,
    fill: str,
    description: str,
) -> str:
    return (
        f'<circle class="series-mark" tabindex="0" role="img" '
        f'aria-label="{_escape(description)}" cx="{_n(x)}" cy="{_n(y)}" '
        f'r="{_n(radius)}" fill="{fill}"><title>{_escape(description)}</title></circle>'
    )


def _point_description(
    spec: ValidatedChartSpec,
    series: dict[str, Any],
    index: int,
    category: str,
) -> str:
    value = series["values"][index]
    if spec.measure == "percent":
        numerator = series["numerators"][index]
        denominator = series["denominator"]
        return f"{category} · {series['name']}：{value:.2f}%（{numerator:,}/{denominator:,}）"
    return f"{category} · {series['name']}：{_fmt(value, spec.measure)} {spec.unit}".strip()


def _heat_color(value: int, maximum: int) -> str:
    ratio = math.log1p(value) / math.log1p(maximum) if maximum else 0.0
    # 固定綠色系透明度，避免任何 spec 欄位進入 CSS 色彩字串。
    return f"rgba(21, 155, 116, {0.16 + 0.76 * ratio:.3f})"


def _padded_range(minimum: float, maximum: float) -> tuple[float, float]:
    if math.isclose(minimum, maximum):
        delta = max(abs(minimum) * 0.05, 1.0)
        return minimum - delta, maximum + delta
    pad = (maximum - minimum) * 0.04
    return minimum - pad, maximum + pad


def _fmt(value: int | float, measure: str) -> str:
    if measure == "percent":
        return f"{float(value):.2f}%"
    if measure == "count":
        return f"{int(value):,}"
    return f"{float(value):,.4g}"


def _n(value: int | float) -> str:
    return f"{float(value):.3f}".rstrip("0").rstrip(".") if value else "0"


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _truncate_label(value: str, maximum_length: int) -> str:
    """限制長類別標籤在軸邊界內；完整名稱仍保留於 tooltip 與資料表。"""

    if len(value) <= maximum_length:
        return value
    return value[: maximum_length - 1] + "…"


__all__ = [
    "MAX_RICH_CHART_HTML_BYTES",
    "chart_semantic_fingerprint",
    "render_chart_html",
]
