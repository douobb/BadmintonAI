"""TASK-021 Plotly 原生 JSON Rich UI 契約測試。"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

import plotly.express as px
import plotly.graph_objects as go
import pytest

from badminton_ai.server.plotly_rich import (
    DEFAULT_PLOTLY_ASSET_URL,
    MAX_PLOTLY_CHARTS,
    MAX_PLOTLY_SPEC_BYTES,
    PLOTLY_ASSET_PATH,
    PLOTLY_CHARTS_FILE,
    PLOTLY_SPEC_VERSION,
    PLOTLY_VERSION,
    PlotlySpecError,
    parse_plotly_charts_artifact,
    render_plotly_charts_html,
    validate_plotly_asset_url,
    validate_plotly_charts,
)


@dataclass
class _Artifact:
    relative_path: str
    extension: str
    mime_type: str
    kind: str
    size_bytes: int
    content_base64: str


def _figure(title: str = "擊球分布") -> dict[str, Any]:
    fig = px.bar(x=["發短球", "殺球"], y=[3, 1], title=title)
    return json.loads(fig.to_json())


def _payload(*charts: dict[str, Any]) -> dict[str, Any]:
    if not charts:
        charts = ({"title": "擊球分布", "figure": _figure()},)
    return {"schema_version": PLOTLY_SPEC_VERSION, "charts": list(charts)}


def _artifact(
    payload: Any,
    *,
    path: str = PLOTLY_CHARTS_FILE,
    raw: bytes | None = None,
) -> _Artifact:
    content = raw or json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(
        "utf-8"
    )
    return _Artifact(
        relative_path=path,
        extension=".json",
        mime_type="application/json",
        kind="json",
        size_bytes=len(content),
        content_base64=base64.b64encode(content).decode("ascii"),
    )


def test_plotly_express_figure_json_is_accepted_and_rendered_as_single_card() -> None:
    first = {"title": "擊球分布", "figure": _figure()}
    second = {
        "title": "每局球數",
        "figure": {
            "data": [{"type": "scatter", "x": [1, 2], "y": [3, 4]}],
            "layout": {"xaxis": {"title": {"text": "局數"}}},
        },
    }
    validated = parse_plotly_charts_artifact(_artifact(_payload(first, second)))
    document = render_plotly_charts_html(validated)

    assert len(validated.charts) == 2
    assert document.count('class="chart-panel"') == 2
    assert document.count('class="plotly-figure" data-chart-index=') == 2
    assert DEFAULT_PLOTLY_ASSET_URL in document
    assert "plotly-6.6.0.min.js" in document
    assert "cdnjs.cloudflare.com" not in document
    assert "https://cdn.plot.ly" not in document
    assert "schema_version" not in document
    assert "iframe:height" in document
    assert f"sha256:{validated.fingerprint}" in document


def test_plotly_to_json_string_is_normalized_without_rerunning_analysis() -> None:
    figure = px.bar(x=["發短球", "殺球"], y=[3, 1])
    validated = validate_plotly_charts(
        _payload({"title": "擊球分布", "figure": figure.to_json()})
    )
    assert validated.charts[0]["figure"]["data"][0]["type"] == "bar"


def test_title_is_single_and_pie_legend_reflows_on_narrow_viewports() -> None:
    title = "前五種球＋其他（全體有效 type 事件分母）"
    pie = px.pie(names=["網前球", "殺球"], values=[3, 1], title=title)
    figure = json.loads(pie.to_json())
    validated = validate_plotly_charts(_payload({"title": title, "figure": figure}))
    document = render_plotly_charts_html(validated)

    assert f"<h2>{title}</h2>" in document
    assert "@media (max-width:600px)" in document
    assert 'const viewport = window.matchMedia("(max-width: 600px)")' in document
    assert "delete layout.title;" in document
    assert 'update["title.text"]' not in document
    assert ".gtitle{display:none!important}" not in document
    assert 'trace.type === "pie"' in document
    assert 'update["legend.orientation"] = narrow' in document
    assert 'update["legend.y"] = narrow ? -0.12' in document
    assert 'update["legend.font.size"] = narrow' in document
    assert 'viewport.addEventListener("change"' in document
    # h2 顯示外層標題，嵌入 JSON 保留原始 figure，renderer 只改執行期副本。
    assert f'"title":{{"text":"{title}"}}' in document
    assert figure["layout"]["title"]["text"] == title


def test_plot_uses_chat_friendly_palette_and_system_light_dark_preference() -> None:
    document = render_plotly_charts_html(validate_plotly_charts(_payload()))

    assert "background:transparent" in document
    assert "--chart-fg:#e5e5e5" in document
    assert "@media (prefers-color-scheme: light)" in document
    assert 'window.matchMedia("(prefers-color-scheme: light)")' in document
    assert 'layout.paper_bgcolor = "rgba(0,0,0,0)"' in document
    assert 'layout.plot_bgcolor = "rgba(0,0,0,0)"' in document
    assert "layout.hoverlabel =" in document
    assert '"legend.font.color": layout.legend.font.color' in document
    assert (
        ".plotly-figure .modebar-btn path{fill:var(--chart-modebar)!important}"
        in document
    )
    assert "applyPaletteToPlot(target, layout, axes, theme)" in document
    assert "parent.document" not in document
    assert "window.parent.postMessage" in document


def test_outside_bar_and_top_scatter_labels_get_room_without_changing_colors() -> None:
    figure = {
        "data": [
            {
                "type": "bar",
                "x": ["總數"],
                "y": [2180],
                "text": ["2180"],
                "textposition": "outside",
                "marker": {"color": "#e4572e"},
            },
            {
                "type": "scatter",
                "x": [1],
                "y": [2180],
                "text": ["2180"],
                "textposition": "top center",
                "mode": "lines+markers+text",
                "line": {"color": "#4c78a8"},
            },
        ],
        "layout": {"margin": {"l": 4, "r": 4, "t": 4, "b": 4}},
    }
    document = render_plotly_charts_html(
        validate_plotly_charts(_payload({"title": "標籤不裁切", "figure": figure}))
    )

    assert '["bar", "scatter", "scattergl"].includes(type)' in document
    assert "const labelCanOverflow = trace =>" in document
    assert "if (labelCanOverflow(copy)) copy.cliponaxis = false" in document
    assert "automargin: true" in document
    assert "layout.height = viewport.matches ? 300 : 340" in document
    assert "r: safeMargin(margin.r, hasOverflowingLabels ? 48 : 32)" in document
    assert "t: safeMargin(margin.t, hasOverflowingLabels ? 36 : 18)" in document
    assert '"textposition":"outside"' in document
    assert '"color":"#e4572e"' in document
    assert '"textposition":"top center"' in document
    assert '"color":"#4c78a8"' in document
    assert figure["data"][0]["marker"]["color"] == "#e4572e"
    assert figure["data"][1]["line"]["color"] == "#4c78a8"


def test_common_question_plot_types_share_the_generic_plotly_renderer() -> None:
    figures = [
        (
            "球種分布",
            px.bar(x=["網前球", "殺球"], y=[3, 1], title="球種分布"),
        ),
        (
            "局數趨勢",
            px.line(x=[1, 2, 3], y=[18, 21, 17], markers=True, title="局數趨勢"),
        ),
        (
            "數值關係",
            px.scatter(x=[1, 2, 3], y=[2, 5, 4], title="數值關係"),
        ),
        (
            "回合長度分布",
            px.box(
                x=["短回合", "短回合", "長回合", "長回合"],
                y=[3, 5, 11, 15],
                title="回合長度分布",
            ),
        ),
        (
            "球種與得分交叉",
            go.Figure(
                data=[
                    go.Heatmap(
                        x=["網前球", "殺球"],
                        y=["得分", "失分"],
                        z=[[3, 1], [1, 2]],
                    )
                ],
                layout={"title": {"text": "球種與得分交叉"}},
            ),
        ),
    ]
    for title, figure in figures:
        payload = _payload({"title": title, "figure": json.loads(figure.to_json())})
        validated = validate_plotly_charts(payload)
        document = render_plotly_charts_html(validated)
        trace = validated.charts[0]["figure"]["data"][0]

        assert title in document
        assert document.count('class="chart-panel"') == 1

        if title == "球種分布":
            assert trace["type"] == "bar"
        elif title == "局數趨勢":
            assert trace["type"] == "scatter"
            assert "lines" in trace["mode"]
        elif title == "數值關係":
            assert trace["type"] == "scatter"
            assert "markers" in trace["mode"]
        elif title == "回合長度分布":
            assert trace["type"] == "box"
        else:
            assert trace["type"] == "heatmap"


def test_script_json_escapes_markup_delimiters_and_safe_plotly_hover_tags() -> None:
    validated = validate_plotly_charts(
        _payload(
            {
                "title": "比較 A < B",
                "figure": {
                    "data": [
                        {
                            "type": "bar",
                            "x": ["A < B"],
                            "y": [1],
                            "hovertemplate": "<b>%{x}</b><br>%{y}<extra></extra>",
                        }
                    ],
                    "layout": {},
                },
            }
        )
    )
    document = render_plotly_charts_html(validated)

    assert "A \\u003c B" in document
    assert "<b>%{x}</b>" not in document
    assert "</script><script>" not in document


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra_html="<script>alert(1)</script>"),
        lambda value: value["charts"][0].update(
            title="</script><script>alert(1)</script>"
        ),
        lambda value: value["charts"][0]["figure"]["data"][0].update(
            x=["<img src=x onerror=alert(1)>"]
        ),
        lambda value: value["charts"][0]["figure"]["layout"].update(
            images=[{"source": "https://example.invalid/image.png"}]
        ),
    ],
)
def test_malicious_html_and_external_resources_are_rejected(mutate: Any) -> None:
    payload = _payload()
    mutate(payload)

    with pytest.raises(PlotlySpecError):
        validate_plotly_charts(payload)


def test_chart_count_and_figure_shape_are_bounded() -> None:
    chart = {"title": "擊球分布", "figure": _figure()}
    with pytest.raises(PlotlySpecError, match="數量"):
        validate_plotly_charts(_payload(*[chart] * (MAX_PLOTLY_CHARTS + 1)))
    with pytest.raises(PlotlySpecError, match="figure"):
        validate_plotly_charts(
            _payload({"title": "無效", "figure": {"data": [], "layout": {}}})
        )


def test_figure_validator_rejects_unknown_trace_and_non_finite_values() -> None:
    with pytest.raises(PlotlySpecError, match="Plotly figure"):
        validate_plotly_charts(
            _payload(
                {
                    "title": "未知 trace",
                    "figure": {"data": [{"type": "invented_trace"}], "layout": {}},
                }
            )
        )
    with pytest.raises(PlotlySpecError):
        validate_plotly_charts(
            _payload(
                {
                    "title": "非有限值",
                    "figure": {
                        "data": [{"type": "scatter", "x": [1], "y": [float("inf")]}],
                        "layout": {},
                    },
                }
            )
        )


def test_network_backed_geographic_trace_is_rejected_for_offline_rendering() -> None:
    with pytest.raises(PlotlySpecError, match="底圖"):
        validate_plotly_charts(
            _payload(
                {
                    "title": "地圖",
                    "figure": {
                        "data": [{"type": "choropleth", "locations": ["TW"]}],
                        "layout": {},
                    },
                }
            )
        )


def test_artifact_path_metadata_base64_and_size_are_enforced() -> None:
    valid = _artifact(_payload())
    assert parse_plotly_charts_artifact(valid).charts[0]["title"] == "擊球分布"

    invalid_path = _artifact(_payload(), path="nested/plotly_charts.json")
    with pytest.raises(PlotlySpecError, match="路徑"):
        parse_plotly_charts_artifact(invalid_path)

    bad_metadata = _artifact(_payload())
    bad_metadata.mime_type = "text/html"
    with pytest.raises(PlotlySpecError, match="metadata"):
        parse_plotly_charts_artifact(bad_metadata)

    too_large_content = b" " * (MAX_PLOTLY_SPEC_BYTES + 1)
    too_large = _artifact({}, raw=too_large_content)
    with pytest.raises(PlotlySpecError, match="大小"):
        parse_plotly_charts_artifact(too_large)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":"badminton-plotly/v1","schema_version":"x","charts":[]}',
        b'{"schema_version":"badminton-plotly/v1","charts":[],"value":NaN}',
    ],
)
def test_artifact_rejects_duplicate_keys_and_invalid_json_constants(raw: bytes) -> None:
    with pytest.raises(PlotlySpecError):
        parse_plotly_charts_artifact(_artifact({}, raw=raw))


def test_asset_url_is_loopback_only_and_version_pinned() -> None:
    assert validate_plotly_asset_url(DEFAULT_PLOTLY_ASSET_URL) == (
        DEFAULT_PLOTLY_ASSET_URL
    )
    assert PLOTLY_VERSION == "6.6.0"
    assert PLOTLY_ASSET_PATH.endswith("plotly-6.6.0.min.js")
    for url in (
        "https://cdn.plot.ly/plotly-6.6.0.min.js",
        "http://example.invalid/assets/plotly-6.6.0.min.js",
        "http://127.0.0.1:8000/plotly.min.js",
        "http://user:pass@127.0.0.1:8000/assets/plotly-6.6.0.min.js",
    ):
        with pytest.raises(ValueError):
            validate_plotly_asset_url(url)
