"""TASK-021 版本化圖表資料驗證與安全 renderer 測試。"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass

import pytest

from badminton_ai.server.chart_spec import (
    CHART_SPEC_VERSION,
    MAX_CHART_SPEC_BYTES,
    ChartSpecError,
    parse_chart_spec_artifact,
    validate_chart_spec,
)
from badminton_ai.server.rich_chart import (
    chart_semantic_fingerprint,
    render_chart_html,
)


@dataclass
class _Artifact:
    relative_path: str
    extension: str
    mime_type: str
    kind: str
    size_bytes: int
    content_base64: str


def _spec(
    chart_type: str,
    data: dict[str, object],
    *,
    measure: str = "count",
    sample_size: int = 4,
    denominator: int = 4,
    excluded_count: int = 0,
    **extra: object,
) -> dict[str, object]:
    return {
        "schema_version": CHART_SPEC_VERSION,
        "chart_type": chart_type,
        "title": "羽球分析",
        "sample_size": sample_size,
        "denominator": denominator,
        "excluded_count": excluded_count,
        "measure": measure,
        "unit": "球",
        "data": data,
        **extra,
    }


def _artifact(payload: object, *, path: str = "chart_spec.json") -> _Artifact:
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
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


@pytest.mark.parametrize(
    "payload",
    [
        _spec(
            "bar",
            {"categories": ["A", "B"], "series": [{"name": "次數", "values": [3, 1]}]},
        ),
        _spec(
            "grouped_bar",
            {
                "categories": ["A", "B"],
                "series": [
                    {"name": "甲", "values": [3, 1]},
                    {"name": "乙", "values": [1, 3]},
                ],
            },
        ),
        _spec(
            "stacked_bar",
            {
                "categories": ["A", "B"],
                "series": [
                    {"name": "甲", "values": [3, 1]},
                    {"name": "乙", "values": [1, 3]},
                ],
            },
        ),
        _spec(
            "line",
            {
                "categories": ["第1局", "第2局"],
                "series": [{"name": "得分", "values": [2.5, 3.5]}],
            },
            measure="value",
        ),
        _spec(
            "radar",
            {
                "categories": ["力量", "速度", "準確"],
                "series": [
                    {"name": "甲", "values": [3, 2, 1]},
                    {"name": "乙", "values": [1, 2, 3]},
                ],
            },
        ),
        _spec(
            "histogram",
            {
                "bins": [
                    {"label": "0–1", "lower": 0, "upper": 1, "count": 2},
                    {"label": "1–2", "lower": 1, "upper": 2, "count": 2},
                ]
            },
        ),
        _spec(
            "donut",
            {"categories": ["A", "B"], "series": [{"name": "樣本", "values": [3, 1]}]},
        ),
        _spec(
            "scatter",
            {
                "sampled": False,
                "points": [
                    {"x": 1.0, "y": 2.0, "label": "點一"},
                    {"x": 2.0, "y": 1.0, "group": "B"},
                ],
            },
            measure="value",
            sample_size=3,
            denominator=2,
            excluded_count=1,
            x_label="擊球點 X",
            y_label="落點 Y",
        ),
        _spec(
            "boxplot",
            {
                "groups": [
                    {
                        "label": "甲",
                        "min": 1,
                        "q1": 2,
                        "median": 3,
                        "q3": 4,
                        "max": 5,
                        "count": 4,
                    }
                ]
            },
            measure="value",
        ),
        _spec(
            "matrix_heatmap",
            {
                "x_categories": ["A", "B"],
                "y_categories": ["前場", "後場"],
                "values": [[1, 1], [1, 1]],
            },
        ),
        _spec(
            "court_heatmap",
            {
                "zones": [{"code": 1, "count": 2}, {"code": 24, "count": 2}],
                "out_of_court_count": 2,
                "undefined_count": 1,
                "missing_count": 1,
            },
            sample_size=8,
            denominator=4,
            excluded_count=4,
        ),
    ],
)
def test_supported_templates_validate_and_render_safe_self_contained_html(
    payload: dict[str, object],
) -> None:
    validated = validate_chart_spec(payload)

    document = render_chart_html(validated)

    assert "<!doctype html>" in document.lower()
    assert "<svg " in document
    assert "資料表" in document
    assert "parent.postMessage" in document
    assert "<script src=" not in document
    assert "https://" not in document
    assert re.search(
        r'<meta name="badmintonai-chart-fingerprint" content="sha256:[0-9a-f]{64}">',
        document,
    )


def test_semantic_fingerprint_is_stable_and_changes_with_chart_data() -> None:
    first = validate_chart_spec(
        _spec(
            "grouped_bar",
            {
                "categories": ["網前", "後場"],
                "series": [
                    {"name": "甲", "values": [3, 1]},
                    {"name": "乙", "values": [1, 3]},
                ],
            },
        )
    )
    same_semantics = validate_chart_spec(
        _spec(
            "grouped_bar",
            {
                "series": [
                    {"values": [3, 1], "name": "甲"},
                    {"values": [1, 3], "name": "乙"},
                ],
                "categories": ["網前", "後場"],
            },
        )
    )
    corrected_data = validate_chart_spec(
        _spec(
            "grouped_bar",
            {
                "categories": ["網前", "後場"],
                "series": [
                    {"name": "甲", "values": [2, 2]},
                    {"name": "乙", "values": [1, 3]},
                ],
            },
        )
    )
    changed_sample = validate_chart_spec(
        _spec(
            "grouped_bar",
            {
                "categories": ["網前", "後場"],
                "series": [
                    {"name": "甲", "values": [3, 1]},
                    {"name": "乙", "values": [1, 3]},
                ],
            },
            sample_size=6,
            denominator=4,
            excluded_count=2,
        )
    )

    assert chart_semantic_fingerprint(first) == chart_semantic_fingerprint(
        same_semantics
    )
    assert chart_semantic_fingerprint(first) != chart_semantic_fingerprint(
        corrected_data
    )
    assert chart_semantic_fingerprint(first) != chart_semantic_fingerprint(
        changed_sample
    )
    assert chart_semantic_fingerprint(first) in render_chart_html(first)


def test_percentages_must_match_each_series_denominator() -> None:
    payload = _spec(
        "bar",
        {
            "categories": ["甲", "乙"],
            "series": [
                {
                    "name": "比例",
                    "values": [50.0, 50.0],
                    "numerators": [2, 2],
                    "denominator": 4,
                }
            ],
        },
        measure="percent",
    )
    assert validate_chart_spec(payload).data["series"][0]["denominator"] == 4

    payload["data"]["series"][0]["values"] = [25.0, 75.0]  # type: ignore[index]
    with pytest.raises(ChartSpecError, match="percentage"):
        validate_chart_spec(payload)


@pytest.mark.parametrize("dimension_count", [2, 13])
def test_radar_rejects_dimension_counts_outside_three_to_twelve(
    dimension_count: int,
) -> None:
    payload = _spec(
        "radar",
        {
            "categories": [f"維度{i}" for i in range(dimension_count)],
            "series": [{"name": "比較", "values": [1] * dimension_count}],
        },
    )

    with pytest.raises(ChartSpecError, match="radar 維度數"):
        validate_chart_spec(payload)


def test_radar_reuses_percent_numerator_and_denominator_validation() -> None:
    payload = _spec(
        "radar",
        {
            "categories": ["力量", "速度", "準確"],
            "series": [
                {
                    "name": "比例",
                    "values": [50.0, 25.0, 25.0],
                    "numerators": [2, 1, 1],
                    "denominator": 4,
                }
            ],
        },
        measure="percent",
        sample_size=6,
        denominator=6,
    )
    assert validate_chart_spec(payload).data["series"][0]["denominator"] == 4
    document = render_chart_html(validate_chart_spec(payload))
    assert "50.00%（2/4）" in document

    payload["data"]["series"][0]["values"][0] = 40.0  # type: ignore[index]
    with pytest.raises(ChartSpecError, match="percentage"):
        validate_chart_spec(payload)

    payload["data"]["series"][0]["values"][0] = 50.0  # type: ignore[index]
    payload["data"]["series"][0]["denominator"] = 7  # type: ignore[index]
    with pytest.raises(ChartSpecError, match="denominator"):
        validate_chart_spec(payload)


def test_radar_renders_value_series_and_allows_a_single_series() -> None:
    payload = _spec(
        "radar",
        {
            "categories": ["維度甲", "維度乙", "維度丙"],
            "series": [{"name": "測量值", "values": [1.5, -2.0, 4.0]}],
        },
        measure="value",
        unit="分",
    )

    document = render_chart_html(validate_chart_spec(payload))

    assert "1.5" in document
    assert "-2" in document
    assert '<th scope="col">測量值</th>' in document
    assert "data-series-toggle" not in document
    assert len(re.findall(r"<script>(.*?)</script>", document, flags=re.DOTALL)) == 1


def test_radar_rejects_more_than_six_series() -> None:
    payload = _spec(
        "radar",
        {
            "categories": ["力量", "速度", "準確"],
            "series": [{"name": f"選手{i}", "values": [1, 2, 3]} for i in range(7)],
        },
    )

    with pytest.raises(ChartSpecError, match="series 數量"):
        validate_chart_spec(payload)


@pytest.mark.parametrize("chart_type", ["grouped_bar", "line", "radar"])
def test_comparison_chart_legend_buttons_toggle_indexed_svg_groups_only(
    chart_type: str,
) -> None:
    categories = ["力量", "速度", "準確"]
    series = [
        {"name": "選手甲 </button><script>secret</script>", "values": [3, 2, 1]},
        {"name": "選手乙", "values": [1, 2, 3]},
    ]
    payload = _spec(
        chart_type,
        {"categories": categories, "series": series},
        measure="value" if chart_type in {"line", "radar"} else "count",
    )

    document = render_chart_html(validate_chart_spec(payload))
    scripts = re.findall(r"<script>(.*?)</script>", document, flags=re.DOTALL)

    assert 'data-series-toggle="0" aria-pressed="true"' in document
    assert 'data-series-toggle="1" aria-pressed="true"' in document
    assert 'type="button" class="legend-item legend-toggle"' in document
    assert 'id="chart-series-0" data-series-index="0"' in document
    assert 'id="chart-series-1" data-series-index="1"' in document
    assert "getElementById(`chart-series-${index}`)" in "".join(scripts)
    assert "classList.toggle('series-hidden', !show)" in "".join(scripts)
    assert "aria-pressed" in "".join(scripts)
    assert len(scripts) == 2
    assert all("選手甲" not in script and "secret" not in script for script in scripts)
    assert "選手甲 &lt;/button&gt;&lt;script&gt;secret&lt;/script&gt;" in document
    assert '<th scope="col">選手乙</th>' in document
    assert "<td>力量</td>" in document
    assert "<td>3</td>" in document


def test_histogram_uses_numeric_bin_widths_density_and_numeric_axis_ticks() -> None:
    payload = _spec(
        "histogram",
        {
            "bins": [
                {"label": "窄區間", "lower": 0, "upper": 1, "count": 2},
                {"label": "寬區間", "lower": 1, "upper": 10, "count": 2},
            ]
        },
        sample_size=4,
        denominator=4,
        x_label="連續數值",
    )
    document = render_chart_html(validate_chart_spec(payload))

    rectangles = re.findall(
        r'<rect class="series-mark"[^>]* x="([^"]+)" y="([^"]+)" '
        r'width="([^"]+)" height="([^"]+)"',
        document,
    )
    assert len(rectangles) == 2
    assert float(rectangles[0][0]) == pytest.approx(66)
    assert float(rectangles[0][2]) == pytest.approx(69)
    assert float(rectangles[1][0]) == pytest.approx(135)
    assert float(rectangles[1][2]) == pytest.approx(621)
    # 相同筆數落在寬度 1 與 9 的區間時，頻數密度及柱高相差 9 倍。
    assert float(rectangles[0][3]) / float(rectangles[1][3]) == pytest.approx(
        9,
        rel=1e-4,
    )
    assert "頻數密度" in document
    for tick in ("0", "2", "4", "6", "8", "10"):
        assert f">{tick}</text>" in document


def test_histogram_zero_count_bin_remains_visible_and_accessible() -> None:
    payload = _spec(
        "histogram",
        {
            "bins": [
                {"label": "零樣本", "lower": 0, "upper": 1, "count": 0},
                {"label": "有樣本", "lower": 1, "upper": 10, "count": 4},
            ]
        },
        sample_size=4,
        denominator=4,
    )
    document = render_chart_html(validate_chart_spec(payload))

    assert '<circle class="series-mark" tabindex="0" role="img"' in document
    assert "零樣本（0–1）：0 球" in document
    assert "頻數密度" in document


def test_histogram_keeps_numeric_gaps_between_bins() -> None:
    payload = _spec(
        "histogram",
        {
            "bins": [
                {"label": "低值", "lower": 0, "upper": 1, "count": 2},
                {"label": "高值", "lower": 3, "upper": 10, "count": 2},
            ]
        },
        sample_size=4,
        denominator=4,
    )
    document = render_chart_html(validate_chart_spec(payload))
    rectangles = re.findall(
        r'<rect class="series-mark"[^>]* x="([^"]+)" y="([^"]+)" '
        r'width="([^"]+)" height="([^"]+)"',
        document,
    )

    assert len(rectangles) == 2
    first_end = float(rectangles[0][0]) + float(rectangles[0][2])
    second_start = float(rectangles[1][0])
    assert first_end == pytest.approx(135)
    assert second_start == pytest.approx(273)
    assert second_start > first_end


@pytest.mark.parametrize(
    "payload",
    [
        _spec(
            "bar",
            {"categories": ["A"], "series": [{"name": "n", "values": [1]}]},
            sample_size=5,
            denominator=4,
            excluded_count=0,
        ),
        _spec(
            "bar",
            {"categories": ["A"], "series": [{"name": "n", "values": [1]}]},
            extra_chart_options={"onload": "alert(1)"},
        ),
        _spec("not-a-template", {"html": "<script>alert(1)</script>"}),
        _spec(
            "bar",
            {"categories": ["A"], "series": [{"name": "n", "values": [float("nan")]}]},
        ),
    ],
)
def test_unknown_options_nonfinite_values_and_bad_sample_denominator_are_rejected(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ChartSpecError):
        validate_chart_spec(payload)


@pytest.mark.parametrize("code", [25, 32, 33])
def test_court_heatmap_rejects_out_of_court_and_undefined_codes(code: int) -> None:
    payload = _spec(
        "court_heatmap",
        {
            "zones": [{"code": code, "count": 4}],
            "out_of_court_count": 0,
            "undefined_count": 0,
            "missing_count": 0,
        },
    )
    with pytest.raises(ChartSpecError, match="場內"):
        validate_chart_spec(payload)


def test_court_heatmap_requires_explicit_exclusion_breakdown() -> None:
    payload = _spec(
        "court_heatmap",
        {
            "zones": [{"code": 1, "count": 3}],
            "out_of_court_count": 0,
            "undefined_count": 0,
            "missing_count": 0,
        },
        sample_size=4,
        denominator=3,
        excluded_count=1,
    )
    with pytest.raises(ChartSpecError, match="排除數"):
        validate_chart_spec(payload)


def test_chart_spec_artifact_is_size_version_path_and_metadata_limited() -> None:
    valid = _spec(
        "bar", {"categories": ["A"], "series": [{"name": "n", "values": [4]}]}
    )
    artifact = _artifact(valid)
    assert parse_chart_spec_artifact(artifact).chart_type == "bar"

    with pytest.raises(ChartSpecError, match="路徑"):
        parse_chart_spec_artifact(_artifact(valid, path="nested/chart_spec.json"))
    invalid_metadata = _artifact(valid)
    invalid_metadata.kind = "file"
    with pytest.raises(ChartSpecError, match="metadata"):
        parse_chart_spec_artifact(invalid_metadata)

    too_large = _Artifact(
        "chart_spec.json",
        ".json",
        "application/json",
        "json",
        MAX_CHART_SPEC_BYTES + 1,
        "AA==",
    )
    with pytest.raises(ChartSpecError, match="大小"):
        parse_chart_spec_artifact(too_large)


def test_chart_spec_rejects_duplicate_keys_and_nan_json_constants() -> None:
    for content in (
        b'{"schema_version":"badminton-chart/v1","schema_version":"badminton-chart/v1"}',
        b'{"value":NaN}',
    ):
        artifact = _Artifact(
            "chart_spec.json",
            ".json",
            "application/json",
            "json",
            len(content),
            base64.b64encode(content).decode("ascii"),
        )
        with pytest.raises(ChartSpecError):
            parse_chart_spec_artifact(artifact)


def test_chart_spec_converts_extremely_large_json_integer_to_contract_error() -> None:
    payload = _spec(
        "bar",
        {"categories": ["A"], "series": [{"name": "數值", "values": [10**1000]}]},
    )

    with pytest.raises(ChartSpecError, match="有限數值"):
        validate_chart_spec(payload)


def test_renderer_escapes_untrusted_labels_and_never_inserts_their_script() -> None:
    payload = _spec(
        "bar",
        {
            "categories": ["</title><script>alert(1)</script>"],
            "series": [{"name": "測試", "values": [4]}],
        },
    )
    document = render_chart_html(validate_chart_spec(payload))

    assert "&lt;/title&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in document
    assert document.count("<script>") == 1
    assert "alert(1)</script>" not in document


def test_horizontal_bar_stays_readable_and_truncates_long_axis_labels() -> None:
    long_label = "超長標籤" * 19 + "A"
    categories = [long_label, *[f"類別{index}" for index in range(1, 48)]]
    payload = _spec(
        "bar",
        {
            "categories": categories,
            "series": [{"name": "次數", "values": [1] * len(categories)}],
        },
        sample_size=48,
        denominator=48,
    )

    document = render_chart_html(validate_chart_spec(payload))
    svg = document.split("</svg>", maxsplit=1)[0]

    assert 'viewBox="0 0 1084 1896"' in svg
    assert 'style="width:1084px;min-width:1084px;max-width:none"' in svg
    assert ".plot { width: 100%; overflow-x: auto;" in document
    assert f">{long_label[:17]}…</text>" in svg
    assert f">{long_label}</text>" not in svg
    assert f"<td>{long_label}</td>" in document
