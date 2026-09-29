"""Knowledge 參考文件與結構化場區對照的一致性測試。"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_DIR = ROOT / "knowledge"
ZONE_MAP_PATH = KNOWLEDGE_DIR / "court-zones.json"
ZONE_DOC_PATH = KNOWLEDGE_DIR / "court-zones.md"
TERMS_DOC_PATH = KNOWLEDGE_DIR / "event-terms-and-shot-types.md"
TERMINOLOGY_DOC_PATH = KNOWLEDGE_DIR / "badminton-terminology.md"
UPLOAD_MARKDOWN_PATHS = (ZONE_DOC_PATH, TERMS_DOC_PATH, TERMINOLOGY_DOC_PATH)


def _zone_mapping() -> dict[str, object]:
    return json.loads(ZONE_MAP_PATH.read_text(encoding="utf-8"))


def _documented_zone_rows() -> list[dict[str, object]]:
    document = ZONE_DOC_PATH.read_text(encoding="utf-8")
    rows: list[dict[str, object]] = []
    for line in document.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 7 or not cells[0].isdigit():
            continue
        rows.append(
            {
                "code": int(cells[0]),
                "is_official_code": cells[1] == "正式代碼",
                "region": cells[2],
                "category": cells[3],
                "court_row": None if cells[4] == "—" else int(cells[4]),
                "court_column": None if cells[5] == "—" else cells[5],
                "position": cells[6],
            }
        )
    return rows


def test_official_zone_codes_cover_1_through_32_once() -> None:
    zones = _zone_mapping()["zones"]
    codes = [zone["code"] for zone in zones]

    assert len(codes) == 32
    assert len(set(codes)) == 32
    assert sorted(codes) == list(range(1, 33))
    assert all(zone["is_official_code"] is True for zone in zones)


def test_landing_area_33_is_separate_non_official_non_out_of_bounds_sentinel() -> None:
    mapping = _zone_mapping()
    sentinel = mapping["landing_area_sentinel"]
    zone_codes = {zone["code"] for zone in mapping["zones"]}

    assert sentinel["code"] == 33
    assert sentinel["is_official_code"] is False
    assert sentinel["is_out_of_bounds"] is False
    assert sentinel["category"] == "未定義落點"
    assert 33 not in zone_codes


def test_human_zone_table_matches_structured_authority() -> None:
    mapping = _zone_mapping()
    expected = [
        {
            "code": zone["code"],
            "is_official_code": zone["is_official_code"],
            "region": zone["region"],
            "category": zone["category"],
            "court_row": zone["court_row"],
            "court_column": zone["court_column"],
            "position": zone["position"],
        }
        for zone in mapping["zones"]
    ]
    sentinel = mapping["landing_area_sentinel"]
    expected.append(
        {
            "code": sentinel["code"],
            "is_official_code": sentinel["is_official_code"],
            "region": sentinel["region"],
            "category": sentinel["category"],
            "court_row": sentinel["court_row"],
            "court_column": sentinel["court_column"],
            "position": sentinel["position"],
        }
    )

    actual = _documented_zone_rows()

    assert len(actual) == 33
    assert [row["code"] for row in actual] == list(range(1, 34))
    assert actual == expected


def test_knowledge_docs_include_current_data_facts() -> None:
    zones_doc = ZONE_DOC_PATH.read_text(encoding="utf-8")
    terms_doc = TERMS_DOC_PATH.read_text(encoding="utf-8")
    mapping = _zone_mapping()

    assert "mapping_version" in mapping
    assert "正式代碼" in zones_doc
    assert "33 是未定義落點 sentinel，不是正式場區，也不表示出界" in zones_doc
    assert "1–24 是場內網格，25–32 是出界區代碼" in zones_doc
    assert "球場兩側" in zones_doc
    assert "最外側 A、D 兩欄的全部六列" in zones_doc
    assert "對手站在兩側看 `opponent_location_area`" in zones_doc

    shot_types = (
        "切球",
        "平球",
        "長球",
        "挑球",
        "接殺防守",
        "推撲球",
        "殺球",
        "發長球",
        "發短球",
        "網前球",
    )
    for shot_type in shot_types:
        assert f"| {shot_type} |" in terms_doc

    for term in (
        "擊球事件",
        "回合數",
        "ball_round",
        "球種",
        "getpoint_player",
        "主動得分",
        "失分",
        "lose_reason",
    ):
        assert term in terms_doc

    assert "主動得分" in terms_doc
    assert "`type` 值" in terms_doc
    assert '`getpoint_player == ""`' in terms_doc
    assert "不是 JSON null" in terms_doc
    assert "舊版代碼" not in terms_doc


def test_event_term_column_table_is_contiguous_and_explains_blank_winner() -> None:
    lines = TERMS_DOC_PATH.read_text(encoding="utf-8").splitlines()
    header_index = next(
        index for index, line in enumerate(lines) if line.startswith("| 欄位 |")
    )
    table_end = header_index + 1
    while table_end < len(lines) and lines[table_end].startswith("|"):
        table_end += 1
    table = "\n".join(lines[header_index:table_end])
    explanation = next(line for line in lines[table_end:] if line.strip())

    assert "`getpoint_player`" in table
    assert "`landing_area`" in table
    assert explanation.startswith('`getpoint_player == ""`')


def test_upload_markdown_excludes_internal_history_and_private_paths() -> None:
    content = "\n".join(
        path.read_text(encoding="utf-8") for path in UPLOAD_MARKDOWN_PATHS
    ).casefold()
    forbidden_markers = (
        "v2",
        "舊版",
        "migration",
        "single_researcher_draft",
        "runtime_enabled",
        "docs/private",
        "data/metadata",
        "badmintonai",
        "court-zones.json",
        "column_definition.json",
        "court_place.txt",
        "文件版本",
        "資料版本",
        "版本",
        "來源",
    )

    assert all(marker not in content for marker in forbidden_markers)


def test_knowledge_docs_exclude_research_defaults_and_prompt_instructions() -> None:
    content = "\n".join(
        path.read_text(encoding="utf-8") for path in UPLOAD_MARKDOWN_PATHS
    ).casefold()
    forbidden_terms = (
        "研究預設",
        "研究門檻",
        "role definition",
        "logic rules for analysis",
        "when i provide",
        "you are an expert",
    )

    assert all(term not in content for term in forbidden_terms)
    assert re.search(r"(?im)^\s*#\s*(task|role definition)\b", content) is None
    assert "請回答以下" not in content
