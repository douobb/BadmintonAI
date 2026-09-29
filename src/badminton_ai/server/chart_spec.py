"""解析 TASK-021 版本化、安全的圖表資料契約。"""

from __future__ import annotations

import base64
import binascii
import json
import math
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

CHART_SPEC_FILE = "chart_spec.json"
CHART_SPEC_VERSION = "badminton-chart/v1"
MAX_CHART_SPEC_BYTES = 256 * 1024
MAX_CATEGORIES = 48
MAX_SERIES = 6
MAX_SCATTER_POINTS = 2500
MAX_MATRIX_CELLS = 576
MAX_BOX_OUTLIERS_PER_GROUP = 100
_MAX_ABSOLUTE_VALUE = 1_000_000_000_000
_CHART_TYPES = frozenset(
    {
        "bar",
        "grouped_bar",
        "stacked_bar",
        "line",
        "radar",
        "histogram",
        "donut",
        "scatter",
        "boxplot",
        "matrix_heatmap",
        "court_heatmap",
    }
)
_MEASURES = frozenset({"count", "percent", "value"})
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "chart_type",
        "title",
        "subtitle",
        "sample_size",
        "denominator",
        "excluded_count",
        "measure",
        "unit",
        "x_label",
        "y_label",
        "data",
    }
)
_REPO_ROOT = Path(__file__).resolve().parents[3]
COURT_ZONE_MAPPING_PATH = _REPO_ROOT / "knowledge" / "court-zones.json"


class ChartSpecError(ValueError):
    """圖表 spec 未符合嚴格版本化資料契約。"""


@dataclass(frozen=True, slots=True)
class ValidatedChartSpec:
    """只含通過 schema 驗證之原始 JSON 資料的圖表規格。"""

    schema_version: str
    chart_type: str
    title: str
    subtitle: str | None
    sample_size: int
    denominator: int
    excluded_count: int
    measure: str
    unit: str
    x_label: str
    y_label: str
    data: dict[str, Any]


def parse_chart_spec_artifact(
    artifact: Any,
    *,
    mapping_path: str | Path | None = None,
) -> ValidatedChartSpec:
    """僅接受根目錄精確命名的 chart_spec.json artifact。"""

    if getattr(artifact, "relative_path", None) != CHART_SPEC_FILE:
        raise ChartSpecError("chart spec artifact 路徑不符合契約")
    if (
        getattr(artifact, "extension", None) != ".json"
        or getattr(artifact, "mime_type", None) != "application/json"
        or getattr(artifact, "kind", None) != "json"
    ):
        raise ChartSpecError("chart spec artifact metadata 不符合契約")
    content_base64 = getattr(artifact, "content_base64", None)
    size_bytes = getattr(artifact, "size_bytes", None)
    if not isinstance(content_base64, str):
        raise ChartSpecError("chart spec artifact 缺少內容")
    if not _is_integer(size_bytes) or not 1 <= size_bytes <= MAX_CHART_SPEC_BYTES:
        raise ChartSpecError("chart spec artifact 大小無效")
    try:
        content = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ChartSpecError("chart spec artifact base64 無效") from exc
    if len(content) != size_bytes:
        raise ChartSpecError("chart spec artifact 大小不一致")
    try:
        payload = json.loads(
            content.decode("utf-8"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_object_pairs,
        )
    except ChartSpecError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChartSpecError("chart spec JSON 無法解析") from exc
    return validate_chart_spec(payload, mapping_path=mapping_path)


def validate_chart_spec(
    payload: Any,
    *,
    mapping_path: str | Path | None = None,
) -> ValidatedChartSpec:
    """驗證 version、維度、數值、分母與各圖型限定欄位。"""

    data = _require_object(payload, "chart spec")
    _require_keys(
        data,
        required={
            "schema_version",
            "chart_type",
            "title",
            "sample_size",
            "denominator",
            "excluded_count",
            "measure",
            "data",
        },
        optional={"subtitle", "unit", "x_label", "y_label"},
        where="chart spec",
    )
    if data["schema_version"] != CHART_SPEC_VERSION:
        raise ChartSpecError("chart spec 版本不受支援")
    chart_type = data["chart_type"]
    if not isinstance(chart_type, str) or chart_type not in _CHART_TYPES:
        raise ChartSpecError("chart spec 圖型不受支援")
    title = _text(data["title"], "title", max_length=120)
    subtitle = (
        _text(data["subtitle"], "subtitle", max_length=180)
        if "subtitle" in data
        else None
    )
    sample_size = _integer(data["sample_size"], "sample_size", minimum=1)
    denominator = _integer(data["denominator"], "denominator", minimum=1)
    excluded_count = _integer(data["excluded_count"], "excluded_count", minimum=0)
    if denominator > sample_size or excluded_count != sample_size - denominator:
        raise ChartSpecError("sample_size、denominator 與 excluded_count 不一致")
    measure = data["measure"]
    if not isinstance(measure, str) or measure not in _MEASURES:
        raise ChartSpecError("chart spec measure 不受支援")
    unit = _text(data.get("unit", ""), "unit", max_length=40, allow_empty=True)
    x_label = _text(data.get("x_label", ""), "x_label", max_length=64, allow_empty=True)
    y_label = _text(data.get("y_label", ""), "y_label", max_length=64, allow_empty=True)
    chart_data = _require_object(data["data"], "data")

    if chart_type in {"bar", "grouped_bar", "stacked_bar", "line", "radar"}:
        chart_data = _validate_category_series(
            chart_data,
            chart_type=chart_type,
            measure=measure,
            denominator=denominator,
        )
    elif chart_type == "histogram":
        chart_data = _validate_histogram(chart_data, measure, denominator)
    elif chart_type == "donut":
        chart_data = _validate_donut(chart_data, measure, denominator)
    elif chart_type == "scatter":
        if not x_label or not y_label:
            raise ChartSpecError("scatter 必須指定 x_label 與 y_label")
        chart_data = _validate_scatter(chart_data, denominator)
    elif chart_type == "boxplot":
        chart_data = _validate_boxplot(chart_data, measure, denominator)
    elif chart_type == "matrix_heatmap":
        chart_data = _validate_matrix_heatmap(chart_data, measure, denominator)
    elif chart_type == "court_heatmap":
        chart_data = _validate_court_heatmap(
            chart_data,
            measure,
            denominator,
            excluded_count,
            mapping_path=Path(mapping_path)
            if mapping_path
            else COURT_ZONE_MAPPING_PATH,
        )

    return ValidatedChartSpec(
        schema_version=CHART_SPEC_VERSION,
        chart_type=chart_type,
        title=title,
        subtitle=subtitle,
        sample_size=sample_size,
        denominator=denominator,
        excluded_count=excluded_count,
        measure=measure,
        unit=unit,
        x_label=x_label,
        y_label=y_label,
        data=chart_data,
    )


def _validate_category_series(
    data: dict[str, Any],
    *,
    chart_type: str,
    measure: str,
    denominator: int,
) -> dict[str, Any]:
    _require_keys(
        data,
        required={"categories", "series"},
        optional=set(),
        where="data",
    )
    categories = _labels(data["categories"], "categories", maximum=MAX_CATEGORIES)
    series_payload = _require_list(data["series"], "series")
    if not 1 <= len(series_payload) <= MAX_SERIES:
        raise ChartSpecError("series 數量超出範圍")
    if chart_type in {"grouped_bar", "stacked_bar"} and len(series_payload) < 2:
        raise ChartSpecError("群組或堆疊長條圖至少需要兩組資料")
    if chart_type == "line" and len(categories) < 2:
        raise ChartSpecError("折線圖至少需要兩個類別")
    if chart_type == "radar" and not 3 <= len(categories) <= 12:
        raise ChartSpecError("radar 維度數必須介於 3–12")
    series: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, item in enumerate(series_payload):
        entry = _require_object(item, f"series[{index}]")
        required = {"name", "values"}
        optional: set[str] = set()
        if measure == "percent":
            required.add("numerators")
            required.add("denominator")
        _require_keys(entry, required=required, optional=optional, where="series")
        name = _text(entry["name"], f"series[{index}].name", max_length=64)
        if name in names:
            raise ChartSpecError("series 名稱不可重複")
        names.add(name)
        values = _numeric_list(entry["values"], f"series[{index}].values")
        if len(values) != len(categories):
            raise ChartSpecError("series values 長度必須等於 categories")
        clean: dict[str, Any] = {"name": name, "values": values}
        if measure == "count":
            clean["values"] = [
                _integer(value, "count value", minimum=0, maximum=denominator)
                for value in values
            ]
        elif measure == "percent":
            series_denominator = _integer(
                entry["denominator"],
                f"series[{index}].denominator",
                minimum=1,
                maximum=denominator,
            )
            numerators = _integer_list(
                entry["numerators"],
                f"series[{index}].numerators",
                maximum=series_denominator,
            )
            if len(numerators) != len(categories):
                raise ChartSpecError("percent numerators 長度必須等於 categories")
            values = [_percentage(value, series_denominator) for value in values]
            if any(
                not math.isclose(
                    value,
                    numerator * 100 / series_denominator,
                    rel_tol=0.0,
                    abs_tol=0.011,
                )
                for value, numerator in zip(values, numerators, strict=True)
            ):
                raise ChartSpecError("percentage 與 numerator/denominator 不一致")
            clean.update(
                values=values,
                numerators=numerators,
                denominator=series_denominator,
            )
        else:
            clean["values"] = [
                _number(value, "value", minimum=-_MAX_ABSOLUTE_VALUE)
                for value in values
            ]
        series.append(clean)
    return {"categories": categories, "series": series}


def _validate_histogram(
    data: dict[str, Any], measure: str, denominator: int
) -> dict[str, Any]:
    if measure != "count":
        raise ChartSpecError("histogram 僅支援 count measure")
    _require_keys(data, required={"bins"}, optional=set(), where="data")
    bins_payload = _require_list(data["bins"], "bins")
    if not 1 <= len(bins_payload) <= MAX_CATEGORIES:
        raise ChartSpecError("histogram bins 數量超出範圍")
    bins: list[dict[str, Any]] = []
    previous_upper: float | None = None
    counts_total = 0
    for index, item in enumerate(bins_payload):
        entry = _require_object(item, f"bins[{index}]")
        _require_keys(
            entry,
            required={"label", "lower", "upper", "count"},
            optional=set(),
            where="bin",
        )
        label = _text(entry["label"], "bin.label", max_length=64)
        lower = _number(entry["lower"], "bin.lower")
        upper = _number(entry["upper"], "bin.upper")
        count = _integer(entry["count"], "bin.count", minimum=0, maximum=denominator)
        if upper <= lower or (previous_upper is not None and lower < previous_upper):
            raise ChartSpecError("histogram bins 必須依序且不可重疊")
        previous_upper = upper
        counts_total += count
        bins.append({"label": label, "lower": lower, "upper": upper, "count": count})
    if counts_total != denominator:
        raise ChartSpecError("histogram bin count 合計必須等於 denominator")
    return {"bins": bins}


def _validate_donut(
    data: dict[str, Any], measure: str, denominator: int
) -> dict[str, Any]:
    if measure not in {"count", "percent"}:
        raise ChartSpecError("donut 僅支援 count 或 percent measure")
    normalized = _validate_category_series(
        data,
        chart_type="bar",
        measure=measure,
        denominator=denominator,
    )
    if len(normalized["series"]) != 1:
        raise ChartSpecError("donut 必須只有一組 series")
    series = normalized["series"][0]
    if measure == "count" and sum(series["values"]) != denominator:
        raise ChartSpecError("donut counts 合計必須等於 denominator")
    if measure == "percent":
        if series["denominator"] != denominator:
            raise ChartSpecError("donut series denominator 必須等於 denominator")
        if sum(series["numerators"]) != denominator:
            raise ChartSpecError("donut numerators 合計必須等於 denominator")
        if not math.isclose(sum(series["values"]), 100.0, abs_tol=0.03):
            raise ChartSpecError("donut percentages 合計必須等於 100")
    return normalized


def _validate_scatter(data: dict[str, Any], denominator: int) -> dict[str, Any]:
    _require_keys(
        data,
        required={"points", "sampled"},
        optional=set(),
        where="data",
    )
    sampled = data["sampled"]
    if not isinstance(sampled, bool):
        raise ChartSpecError("scatter sampled 必須是 boolean")
    points_payload = _require_list(data["points"], "points")
    if not 1 <= len(points_payload) <= MAX_SCATTER_POINTS:
        raise ChartSpecError("scatter points 數量超出範圍")
    if sampled and len(points_payload) >= denominator:
        raise ChartSpecError("scatter 宣告 sampled 時點數必須少於 denominator")
    if not sampled and len(points_payload) != denominator:
        raise ChartSpecError("scatter points 必須等於 denominator 或明確抽樣")
    points: list[dict[str, Any]] = []
    groups: set[str] = set()
    for index, item in enumerate(points_payload):
        entry = _require_object(item, f"points[{index}]")
        _require_keys(
            entry,
            required={"x", "y"},
            optional={"label", "group"},
            where="point",
        )
        point: dict[str, Any] = {
            "x": _number(entry["x"], "point.x"),
            "y": _number(entry["y"], "point.y"),
        }
        if "label" in entry:
            point["label"] = _text(entry["label"], "point.label", max_length=80)
        if "group" in entry:
            group = _text(entry["group"], "point.group", max_length=48)
            groups.add(group)
            if len(groups) > MAX_SERIES:
                raise ChartSpecError("scatter group 數量超出範圍")
            point["group"] = group
        points.append(point)
    return {"points": points, "sampled": sampled}


def _validate_boxplot(
    data: dict[str, Any], measure: str, denominator: int
) -> dict[str, Any]:
    if measure != "value":
        raise ChartSpecError("boxplot 僅支援 value measure")
    _require_keys(data, required={"groups"}, optional=set(), where="data")
    groups_payload = _require_list(data["groups"], "groups")
    if not 1 <= len(groups_payload) <= MAX_CATEGORIES:
        raise ChartSpecError("boxplot groups 數量超出範圍")
    groups: list[dict[str, Any]] = []
    names: set[str] = set()
    total = 0
    for index, item in enumerate(groups_payload):
        entry = _require_object(item, f"groups[{index}]")
        _require_keys(
            entry,
            required={"label", "min", "q1", "median", "q3", "max", "count"},
            optional={"outliers"},
            where="boxplot group",
        )
        label = _text(entry["label"], "group.label", max_length=80)
        if label in names:
            raise ChartSpecError("boxplot group label 不可重複")
        names.add(label)
        values = {
            key: _number(entry[key], f"group.{key}")
            for key in ("min", "q1", "median", "q3", "max")
        }
        if not (
            values["min"]
            <= values["q1"]
            <= values["median"]
            <= values["q3"]
            <= values["max"]
        ):
            raise ChartSpecError("boxplot min、quartiles 與 max 順序無效")
        count = _integer(entry["count"], "group.count", minimum=1, maximum=denominator)
        total += count
        outliers_payload = entry.get("outliers", [])
        outliers = _numeric_list(outliers_payload, "group.outliers")
        if len(outliers) > MAX_BOX_OUTLIERS_PER_GROUP:
            raise ChartSpecError("boxplot outliers 數量超出上限")
        group = {"label": label, **values, "count": count, "outliers": outliers}
        groups.append(group)
    if total != denominator:
        raise ChartSpecError("boxplot group counts 合計必須等於 denominator")
    return {"groups": groups}


def _validate_matrix_heatmap(
    data: dict[str, Any], measure: str, denominator: int
) -> dict[str, Any]:
    if measure != "count":
        raise ChartSpecError("matrix_heatmap 僅支援 count measure")
    _require_keys(
        data,
        required={"x_categories", "y_categories", "values"},
        optional=set(),
        where="data",
    )
    x_categories = _labels(data["x_categories"], "x_categories", maximum=MAX_CATEGORIES)
    y_categories = _labels(data["y_categories"], "y_categories", maximum=MAX_CATEGORIES)
    if len(x_categories) * len(y_categories) > MAX_MATRIX_CELLS:
        raise ChartSpecError("matrix_heatmap 格數超出上限")
    rows_payload = _require_list(data["values"], "matrix values")
    if len(rows_payload) != len(y_categories):
        raise ChartSpecError("matrix rows 必須等於 y_categories")
    values: list[list[int]] = []
    total = 0
    for row in rows_payload:
        numeric = _numeric_list(row, "matrix row")
        if len(numeric) != len(x_categories):
            raise ChartSpecError("matrix 欄數必須等於 x_categories")
        converted = [
            _integer(item, "matrix count", minimum=0, maximum=denominator)
            for item in numeric
        ]
        total += sum(converted)
        values.append(converted)
    if total != denominator:
        raise ChartSpecError("matrix count 合計必須等於 denominator")
    return {
        "x_categories": x_categories,
        "y_categories": y_categories,
        "values": values,
    }


def _validate_court_heatmap(
    data: dict[str, Any],
    measure: str,
    denominator: int,
    excluded_count: int,
    *,
    mapping_path: Path,
) -> dict[str, Any]:
    if measure != "count":
        raise ChartSpecError("court_heatmap 僅支援 count measure")
    _require_keys(
        data,
        required={
            "zones",
            "out_of_court_count",
            "undefined_count",
            "missing_count",
        },
        optional=set(),
        where="data",
    )
    zone_map = _load_court_grid_codes(mapping_path)
    zones_payload = _require_list(data["zones"], "zones")
    if not 1 <= len(zones_payload) <= 24:
        raise ChartSpecError("court_heatmap zones 數量無效")
    zones: list[dict[str, int]] = []
    seen_codes: set[int] = set()
    total = 0
    for index, item in enumerate(zones_payload):
        entry = _require_object(item, f"zones[{index}]")
        _require_keys(
            entry,
            required={"code", "count"},
            optional=set(),
            where="court zone",
        )
        code = _integer(entry["code"], "zone.code", minimum=1, maximum=33)
        if code not in zone_map:
            raise ChartSpecError("court_heatmap 僅接受官方場內 1–24 網格")
        if code in seen_codes:
            raise ChartSpecError("court_heatmap zone code 不可重複")
        seen_codes.add(code)
        count = _integer(entry["count"], "zone.count", minimum=0, maximum=denominator)
        total += count
        zones.append({"code": code, "count": count})
    if total != denominator:
        raise ChartSpecError("場內 zone count 合計必須等於 denominator")
    out_of_court = _integer(
        data["out_of_court_count"],
        "out_of_court_count",
        minimum=0,
        maximum=excluded_count,
    )
    undefined = _integer(
        data["undefined_count"],
        "undefined_count",
        minimum=0,
        maximum=excluded_count,
    )
    missing = _integer(
        data["missing_count"],
        "missing_count",
        minimum=0,
        maximum=excluded_count,
    )
    if out_of_court + undefined + missing != excluded_count:
        raise ChartSpecError("出界、未定義與缺失排除數不一致")
    zones.sort(key=lambda item: item["code"])
    return {
        "zones": zones,
        "out_of_court_count": out_of_court,
        "undefined_count": undefined,
        "missing_count": missing,
        "zone_mapping": zone_map,
    }


@lru_cache(maxsize=4)
def _load_court_grid_codes(mapping_path: Path) -> dict[int, dict[str, Any]]:
    """只從權威 mapping 讀取有明確場內列欄座標的代碼。"""

    try:
        payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChartSpecError("權威 court-zone mapping 無法讀取") from exc
    zones = payload.get("zones") if isinstance(payload, dict) else None
    if not isinstance(zones, list):
        raise ChartSpecError("權威 court-zone mapping 格式無效")
    result: dict[int, dict[str, Any]] = {}
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        code = zone.get("code")
        row = zone.get("court_row")
        column = zone.get("court_column")
        if (
            zone.get("is_official_code") is True
            and zone.get("region") == "場內"
            and _is_integer(code)
            and 1 <= code <= 24
            and _is_integer(row)
            and 1 <= row <= 6
            and column in {"A", "B", "C", "D"}
        ):
            result[code] = {
                "court_row": row,
                "court_column": column,
                "category": _text(zone.get("category"), "zone.category", max_length=24),
                "position": _text(zone.get("position"), "zone.position", max_length=24),
            }
    if set(result) != set(range(1, 25)):
        raise ChartSpecError("權威 court-zone mapping 必須完整提供 1–24 場內格位")
    return result


def _reject_json_constant(value: str) -> None:
    raise ChartSpecError(f"JSON 常數 {value} 不允許")


def _unique_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ChartSpecError("chart spec JSON 不可包含重複欄位")
        result[key] = value
    return result


def _require_object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ChartSpecError(f"{where} 必須是 object")
    return value


def _require_list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChartSpecError(f"{where} 必須是 array")
    return value


def _require_keys(
    value: dict[str, Any],
    *,
    required: set[str],
    optional: set[str],
    where: str,
) -> None:
    keys = set(value)
    if not required <= keys or keys - required - optional:
        raise ChartSpecError(f"{where} 欄位不符合版本化 schema")


def _text(
    value: Any,
    where: str,
    *,
    max_length: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or len(value) > max_length:
        raise ChartSpecError(f"{where} 必須是有長度上限的文字")
    if (not value and not allow_empty) or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
    ):
        raise ChartSpecError(f"{where} 為空或包含控制字元")
    return value


def _labels(value: Any, where: str, *, maximum: int) -> list[str]:
    labels_payload = _require_list(value, where)
    if not 1 <= len(labels_payload) <= maximum:
        raise ChartSpecError(f"{where} 數量超出範圍")
    labels = [_text(item, where, max_length=80) for item in labels_payload]
    if len(set(labels)) != len(labels):
        raise ChartSpecError(f"{where} 不可重複")
    return labels


def _numeric_list(value: Any, where: str) -> list[int | float]:
    return [_number(item, where) for item in _require_list(value, where)]


def _integer_list(value: Any, where: str, *, maximum: int) -> list[int]:
    return [
        _integer(item, where, minimum=0, maximum=maximum)
        for item in _require_list(value, where)
    ]


def _number(
    value: Any,
    where: str,
    *,
    minimum: float = -_MAX_ABSOLUTE_VALUE,
    maximum: float = _MAX_ABSOLUTE_VALUE,
) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChartSpecError(f"{where} 必須是範圍內有限數值")
    # Compare integers before math.isfinite: converting an attacker-sized JSON
    # integer to float can raise OverflowError instead of a contract error.
    if isinstance(value, int):
        if not minimum <= value <= maximum:
            raise ChartSpecError(f"{where} 必須是範圍內有限數值")
        return value
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ChartSpecError(f"{where} 必須是範圍內有限數值")
    return value


def _integer(
    value: Any,
    where: str,
    *,
    minimum: int,
    maximum: int = 1_000_000_000,
) -> int:
    if not _is_integer(value) or not minimum <= value <= maximum:
        raise ChartSpecError(f"{where} 必須是範圍內整數")
    return value


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _percentage(value: Any, denominator: int) -> float:
    numeric = _number(value, "percentage", minimum=0, maximum=100)
    return float(numeric)


__all__ = [
    "CHART_SPEC_FILE",
    "CHART_SPEC_VERSION",
    "MAX_CHART_SPEC_BYTES",
    "ChartSpecError",
    "ValidatedChartSpec",
    "parse_chart_spec_artifact",
    "validate_chart_spec",
]
