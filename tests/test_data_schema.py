"""canonical 逐拍資料 structural rules 測試。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from badminton_ai.data import (
    COURT_ZONE_CODES,
    UNDEFINED_LANDING_AREA_CODE,
    DatasetSnapshot,
    SchemaValidationError,
    validate_snapshot,
)


def _event_rows() -> list[dict[str, Any]]:
    return [
        {
            "match_id": "M1",
            "set": "1",
            "rally": "1",
            "rally_id": "R1",
            "ball_round": "1",
            "player": "Alice",
            "opponent": "Bob",
            "type": "發短球",
            "getpoint_player": "",
            "win_reason": "",
            "lose_reason": "",
            "landing_area": "1",
            "player_location_area": "2",
        },
        {
            "match_id": "M1",
            "set": "1",
            "rally": "1",
            "rally_id": "R1",
            "ball_round": "2",
            "player": "Bob",
            "opponent": "Alice",
            "type": "殺球",
            "getpoint_player": "Alice",
            "win_reason": "得分",
            "lose_reason": "",
            "landing_area": "2",
            "player_location_area": "3",
        },
    ]


def _snapshot(rows: list[dict[str, Any]]) -> DatasetSnapshot:
    return DatasetSnapshot(
        columns=tuple(rows[0]),
        rows=tuple(rows),
        source=Path("fixture.csv"),
    )


def test_validate_snapshot_accepts_valid_rally() -> None:
    snapshot = _snapshot(_event_rows())

    assert validate_snapshot(snapshot) is snapshot


@pytest.mark.parametrize("identity_field", ["match_id", "set", "rally"])
def test_validate_snapshot_rejects_rally_id_reuse_across_rally_keys(
    identity_field: str,
) -> None:
    rows = _event_rows()
    first = dict(rows[0])
    second = dict(rows[1])
    first[identity_field] = "M2" if identity_field == "match_id" else "2"
    second[identity_field] = first[identity_field]
    first["rally_id"] = "R1"
    second["rally_id"] = "R1"
    rows.extend([first, second])

    with pytest.raises(SchemaValidationError, match="第 1 列.*第 3 列"):
        validate_snapshot(_snapshot(rows))


def test_validate_snapshot_accepts_distinct_rally_ids_across_rally_keys() -> None:
    rows = _event_rows()
    first = dict(rows[0])
    second = dict(rows[1])
    first["rally"] = "2"
    second["rally"] = "2"
    first["rally_id"] = "R2"
    second["rally_id"] = "R2"
    rows.extend([first, second])

    assert validate_snapshot(_snapshot(rows)) is not None


@pytest.mark.parametrize(
    ("field", "value", "rule"),
    [
        ("match_id", "", "match_id"),
        ("set", "4", "set"),
        ("ball_round", "0", "正整數"),
        ("type", "未知球種", "核准球種"),
        ("landing_area", "34", "場區代碼"),
        ("player_location_area", "0", "正整數"),
    ],
)
def test_validate_snapshot_rejects_invalid_required_values(
    field: str,
    value: str,
    rule: str,
) -> None:
    rows = _event_rows()
    rows[0][field] = value

    with pytest.raises(SchemaValidationError, match=rule):
        validate_snapshot(_snapshot(rows))


def test_validate_snapshot_rejects_same_players_and_missing_row_column() -> None:
    rows = _event_rows()
    rows[0]["opponent"] = "Alice"
    with pytest.raises(SchemaValidationError, match="不得相同"):
        validate_snapshot(_snapshot(rows))

    missing_column = _event_rows()
    del missing_column[0]["rally_id"]
    with pytest.raises(SchemaValidationError, match="缺少必要欄位"):
        validate_snapshot(_snapshot(missing_column))


def test_validate_snapshot_accepts_undefined_landing_area_sentinel() -> None:
    rows = _event_rows()
    rows[1]["landing_area"] = str(UNDEFINED_LANDING_AREA_CODE)

    assert validate_snapshot(_snapshot(rows)) is not None
    assert UNDEFINED_LANDING_AREA_CODE not in COURT_ZONE_CODES


def test_validate_snapshot_rejects_sentinel_for_player_location_area() -> None:
    rows = _event_rows()
    rows[1]["player_location_area"] = str(UNDEFINED_LANDING_AREA_CODE)

    with pytest.raises(SchemaValidationError, match="player_location_area.*1 到 32"):
        validate_snapshot(_snapshot(rows))


@pytest.mark.parametrize(
    ("mutation", "rule"),
    [
        ("duplicate_key", "事件鍵"),
        ("inconsistent_rally_id", "rally_id 必須一致"),
        ("gapped_round", "ball_round 必須依輸入順序"),
        ("no_winner", "恰有一筆 getpoint_player"),
        ("multiple_winners", "恰有一筆 getpoint_player"),
        ("winner_not_terminal", "最大 ball_round"),
        ("unknown_winner", "參賽者"),
        ("nonterminal_reason", "非終局事件"),
    ],
)
def test_validate_snapshot_rejects_rally_integrity_violations(
    mutation: str,
    rule: str,
) -> None:
    rows = _event_rows()
    if mutation == "duplicate_key":
        rows.append(dict(rows[1]))
    elif mutation == "inconsistent_rally_id":
        rows[1]["rally_id"] = "R2"
    elif mutation == "gapped_round":
        rows[1]["ball_round"] = "3"
    elif mutation == "no_winner":
        rows[1]["getpoint_player"] = ""
    elif mutation == "multiple_winners":
        rows[0]["getpoint_player"] = "Bob"
    elif mutation == "winner_not_terminal":
        rows[0]["getpoint_player"] = "Alice"
        rows[1]["getpoint_player"] = ""
    elif mutation == "unknown_winner":
        rows[1]["getpoint_player"] = "Carol"
    elif mutation == "nonterminal_reason":
        rows[0]["win_reason"] = "過早"

    with pytest.raises(SchemaValidationError, match=rule):
        validate_snapshot(_snapshot(rows))


@pytest.mark.parametrize(
    "invalid_value",
    ["1.5", "NaN", "Infinity", "1e1", float("nan"), float("inf"), 1.5],
)
def test_validate_snapshot_rejects_non_integral_numeric_values(
    invalid_value: Any,
) -> None:
    rows = _event_rows()
    rows[1]["set"] = invalid_value

    with pytest.raises(SchemaValidationError, match="正整數"):
        validate_snapshot(_snapshot(rows))
