"""羽球術語 Knowledge 與 v2 場區契約邊界測試。"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_DIR = ROOT / "knowledge"
TERMINOLOGY_PATH = KNOWLEDGE_DIR / "badminton-terminology.md"
ZONE_MAPPING_PATH = KNOWLEDGE_DIR / "court-zones.json"

EXPECTED_ALIASES = {
    "攻擊性球種": {"攻擊性球種", "進攻球種", "攻擊球種"},
    "後場": {"後場", "後排", "底線區"},
    "比分膠著": {"比分膠著", "膠著", "比分接近"},
    "前中後場": {"前中後場", "前、中、後場", "前場、中場、後場", "前場、中場與後場"},
    "四角拉吊": {"四角拉吊", "四角調動", "調動四角"},
    "前場": {"前場", "前排", "網前區"},
    "前後場": {"前後場", "前、後場", "前場與後場", "前場和後場"},
    "比賽後半段": {"比賽後半段", "比賽後段"},
    "長回合": {"長回合", "多拍回合"},
    "中場": {"中場", "中排", "中場區"},
    "網前交換": {"網前交換", "網前互動", "網前推撥", "網前互相推撥"},
    "被動救球": {"被動救球", "被動情況", "跨步救球"},
    "短中長回合": {"短中長回合", "短、中、長回合", "回合長度分組"},
    "領先或落後較多": {
        "領先或落後較多",
        "領先較多",
        "落後較多",
        "領先 3 分以上",
        "落後 3 分以上",
    },
    "短回合": {"短回合", "短拍數回合"},
    "邊線附近": {"邊線附近", "靠近邊線", "球場兩側", "場地兩側"},
    "局末追分": {"局末追分", "局末落後", "關鍵分落後"},
}

SPATIAL_TERM_NAMES = {"後場", "前中後場", "前場", "前後場", "中場", "邊線附近"}
USER_DEFINED_TERM_NAMES = {
    "攻擊性球種",
    "比分膠著",
    "比賽後半段",
    "長回合",
    "網前交換",
    "短中長回合",
    "領先或落後較多",
    "短回合",
    "局末追分",
}
RESEARCH_PROXY_TERM_NAMES = {"四角拉吊", "被動救球"}


def _section_table_rows(heading: str, expected_cells: int) -> list[list[str]]:
    document = TERMINOLOGY_PATH.read_text(encoding="utf-8")
    section = document.split(heading, maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    rows: list[list[str]] = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) == expected_cells and not set(cells[0]) <= {"-", ":"}:
            if cells[0] not in {"標準詞", "用語"}:
                rows.append(cells)
    return rows


def _zone_mapping() -> dict[str, object]:
    return json.loads(ZONE_MAPPING_PATH.read_text(encoding="utf-8"))


def _codes_in_cell(text: str) -> set[int]:
    codes: set[int] = set()
    for first, last in re.findall(r"(\d+)(?:\s*[–-]\s*(\d+))?", text):
        start = int(first)
        stop = int(last) if last else start
        codes.update(range(start, stop + 1))
    return codes


def test_terminology_covers_all_17_names_aliases_and_explanations() -> None:
    rows = _section_table_rows("## 17 項術語與別名", expected_cells=4)
    actual = {
        row[0]: {
            "aliases": set(row[1].split("；")),
            "status": row[2],
            "explanation": row[3],
        }
        for row in rows
    }

    assert len(rows) == 17
    assert set(actual) == set(EXPECTED_ALIASES)
    for name, aliases in EXPECTED_ALIASES.items():
        assert actual[name]["aliases"] == aliases
        assert actual[name]["explanation"].strip()


def test_spatial_terms_match_v2_zone_mapping() -> None:
    mapping = _zone_mapping()
    zones = mapping["zones"]
    in_court = [zone for zone in zones if zone["region"] == "場內"]
    by_area = {
        category: {zone["code"] for zone in in_court if zone["category"] == category}
        for category in ("前場", "中場", "後場")
    }
    expected = {
        "前場": by_area["前場"],
        "中場": by_area["中場"],
        "後場": by_area["後場"],
        "前中後場": by_area["前場"] | by_area["中場"] | by_area["後場"],
        "前後場": by_area["前場"] | by_area["後場"],
        "邊線附近": {
            zone["code"] for zone in in_court if zone["court_column"] in {"A", "D"}
        },
    }
    rows = _section_table_rows("## 場區對照", expected_cells=2)
    actual = {row[0]: _codes_in_cell(row[1]) for row in rows}

    assert set(actual) == SPATIAL_TERM_NAMES
    assert actual == expected


def test_mapping_separates_out_of_bounds_and_undefined_landing() -> None:
    mapping = _zone_mapping()
    zones = {zone["code"]: zone for zone in mapping["zones"]}
    sentinel = mapping["landing_area_sentinel"]
    document = TERMINOLOGY_PATH.read_text(encoding="utf-8")

    assert {code for code, zone in zones.items() if zone["region"] == "出界區"} == set(
        range(25, 33)
    )
    assert sentinel["code"] == 33
    assert sentinel["is_official_code"] is False
    assert sentinel["is_out_of_bounds"] is False
    assert "25–32 是正式代碼中的出界區" in document
    assert "33 是未定義落點，既不是正式場區，也不是出界區" in document
    assert "空間對象依問題選擇" in document
    assert "landing_area" in document
    assert "player_location_area" in document


def test_terminology_states_mapping_user_definitions_and_proxy_limits() -> None:
    rows = _section_table_rows("## 17 項術語與別名", expected_cells=4)
    status_by_name = {row[0]: row[2] for row in rows}
    explanation_by_name = {row[0]: row[3] for row in rows}
    document = TERMINOLOGY_PATH.read_text(encoding="utf-8")

    assert all("場區對照已確認" in status_by_name[name] for name in SPATIAL_TERM_NAMES)
    assert all(
        "需使用者指定分析口徑" in status_by_name[name]
        for name in USER_DEFINED_TERM_NAMES
    )
    assert all(
        "推論性概念；資料目前無法直接證實" in status_by_name[name]
        for name in RESEARCH_PROXY_TERM_NAMES
    )
    assert "不能證明球員意圖或身體姿勢" in document
    assert "不能直接證實姿勢、被動狀態或意圖" in document
    assert "都只是 proxy，不是真實標籤" in document
    score_margin_explanation = explanation_by_name["領先或落後較多"]
    assert "只在使用者題目明示該條件時適用" in score_margin_explanation
    assert "泛稱「領先／落後較多」仍需使用者指定分差口徑" in score_margin_explanation


def test_four_corners_lobbing_requires_a_user_selected_proxy() -> None:
    rows = _section_table_rows("## 17 項術語與別名", expected_cells=4)
    four_corners = next(row for row in rows if row[0] == "四角拉吊")

    assert "推論性概念；資料目前無法直接證實" in four_corners[2]
    assert "四角選取及事件序列定義若要量化，須另由使用者界定" in four_corners[3]
    assert "proxy，不是真實意圖標籤" in four_corners[3]


def test_terminology_has_user_boundaries_and_no_unapproved_defaults() -> None:
    document = TERMINOLOGY_PATH.read_text(encoding="utf-8")
    content = document.casefold()

    assert "不是統計資料或執行規則" in document
    assert "使用者對分析範圍" in document
    assert "比分快照欄位的事件前／後語意尚未確認" in document

    forbidden_defaults = (
        "<=4",
        "<= 4",
        ">=11",
        ">= 11",
        "分差不超過 1",
        "分差至少 3",
        "領先 3 分以上為預設",
        "落後 3 分以上為預設",
        "第 2 局與第 3 局",
        "18 分",
        "75 百分位",
        "第 75 百分位",
        "連續至少兩拍",
        "連續 2 拍",
        "殺球與推撲球作為攻擊性球種",
    )
    assert all(value not in content for value in forbidden_defaults)
    assert "player_score" not in content
    assert "opponent_score" not in content
