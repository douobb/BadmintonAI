"""TASK-016 題庫、獨立 oracle 與 live 紀錄工具回歸測試。"""

from __future__ import annotations

import base64
import csv
import json
import struct
from pathlib import Path

import pytest

from scripts.acceptance import (
    AcceptanceError,
    _compare_response,
    _location_xy_oracle,
    compute_snapshot_oracle,
    validate_fixtures,
    verify_q7_histogram2d,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUESTION_FILE = PROJECT_ROOT / "tests/fixtures/task_016_questions.json"
ORACLE_FILE = PROJECT_ROOT / "tests/fixtures/task_016_oracle.json"
SNAPSHOT_COLUMNS = (
    "rally_id",
    "ball_round",
    "player",
    "opponent",
    "type",
    "getpoint_player",
    "landing_area",
    "player_location_area",
    "player_location_x",
    "player_location_y",
    "opponent_location_area",
)


def test_question_bank_has_required_cases_and_historical_prompts() -> None:
    """題庫需涵蓋要求類型，並保留八道舊題的原文與 source。"""

    questions, oracle = validate_fixtures(QUESTION_FILE, ORACLE_FILE)
    by_id = {case["id"]: case for case in questions}

    assert len(questions) == 12
    assert {"Q1", "Q3", "Q5", "Q7", "Q8", "Q25", "Q32", "Q60"} <= set(by_id)
    assert by_id["Q32"]["turns"] == ["在10拍以上的回合中，周天成的得分率是多少？"]
    assert by_id["Q60"]["turns"] == [
        "周天成失分的回合中，倒數第二拍若由他擊出，最常使用什麼球種？"
    ]
    assert by_id["Q5"]["categories"].count("ambiguity") == 1
    assert "不可用主動得分總數 85 作此題分母" in by_id["Q1"]["acceptance_criteria"]
    assert oracle["cases"]["Q1"]["numerator"] == 61
    assert oracle["cases"]["Q1"]["denominator"] == 192
    assert oracle["cases"]["Q1"]["active_scoring_denominator"] == 85
    assert set(oracle["cases"]["Q5"]["accepted_interpretations"]) == {
        "zhou_winning_rallies_terminal_event",
        "zhou_direct_winning_stroke",
    }
    q5_oracles = oracle["cases"]["Q5"]["accepted_interpretations"]
    assert q5_oracles["zhou_winning_rallies_terminal_event"]["sample_events"] == 192
    assert q5_oracles["zhou_direct_winning_stroke"]["sample_events"] == 85
    assert by_id["Q25"]["mapping_source"].endswith(
        "court_place.txt (Spatial Relationships matrix)"
    )
    assert oracle["cases"]["Q25"]["left_side_zones"] == [1, 5, 9, 13, 17, 21]
    assert oracle["cases"]["Q25"]["right_side_zones"] == [4, 8, 12, 16, 20, 24]
    assert oracle["cases"]["Q7"]["player_location_xy"]["valid_coordinate_events"] == 299
    assert (
        len(oracle["cases"]["Q7"]["player_location_xy"]["coordinate_multiset_sha256"])
        == 64
    )
    assert len(oracle["cases"]["Q60"]["counts"]) > 1
    assert oracle["cases"]["Q32"]["long_rallies_at_least_10_events"] == 223
    ambiguous_behavior = oracle["behavior_cases"]["AMBIGUOUS"]
    assert "多個候選對象" in ambiguous_behavior["behavior"]
    assert "確認前可查資料目錄或知識以釐清" in ambiguous_behavior["behavior"]
    assert "不可執行分析工具" in ambiguous_behavior["behavior"]
    assert "為各候選對象預先試算並回報結果" in ambiguous_behavior["must_not"]
    assert {"AMBIGUOUS", "ERROR_502"} == set(oracle["behavior_cases"])

    present_categories = {
        category for case in questions for category in case["categories"]
    }
    assert {
        "success",
        "no_data",
        "ambiguity",
        "tool_error",
        "follow_up",
        "chart",
    } <= present_categories


def _write_rally(
    writer: csv.DictWriter,
    *,
    rally_id: str,
    winner: str,
    first_player: str,
    terminal_type: str,
) -> None:
    other_player = (
        "Kento MOMOTA" if first_player == "CHOU Tien Chen" else "CHOU Tien Chen"
    )
    for ball_round in range(1, 11):
        player = first_player if ball_round % 2 == 1 else other_player
        opponent = other_player if player == first_player else first_player
        terminal = ball_round == 10
        writer.writerow(
            {
                "rally_id": rally_id,
                "ball_round": f"{ball_round}.0",
                "player": player,
                "opponent": opponent,
                "type": terminal_type
                if terminal
                else ("長球" if ball_round == 9 else "網前球"),
                "getpoint_player": winner if terminal else "",
                "landing_area": "10.0" if ball_round != 10 else "33.0",
                "player_location_area": "1.0",
                "player_location_x": "1.5",
                "player_location_y": "2.5",
                "opponent_location_area": "4.0",
            }
        )


def test_csv_oracle_ignores_blank_winners_and_uses_ten_shot_threshold(
    tmp_path: Path,
) -> None:
    """空字串不是 winner；Q32 的「10 拍以上」按十筆逐拍事件計。"""

    snapshot = tmp_path / "events.csv"
    with snapshot.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SNAPSHOT_COLUMNS)
        writer.writeheader()
        _write_rally(
            writer,
            rally_id="rally-1",
            winner="CHOU Tien Chen",
            first_player="Kento MOMOTA",
            terminal_type="殺球",
        )
        _write_rally(
            writer,
            rally_id="rally-2",
            winner="Kento MOMOTA",
            first_player="CHOU Tien Chen",
            terminal_type="長球",
        )
        writer.writerow(
            {
                "rally_id": "rally-3",
                "ball_round": "1.0",
                "player": "Kento MOMOTA",
                "opponent": "CHOU Tien Chen",
                "type": "殺球",
                "getpoint_player": "CHOU Tien Chen",
                "landing_area": "12.0",
                "player_location_area": "10.0",
                "player_location_x": "3.5",
                "player_location_y": "4.5",
                "opponent_location_area": "8.0",
            }
        )

    result = compute_snapshot_oracle(snapshot)

    assert result["snapshot"]["row_count"] == 21
    assert result["snapshot"]["blank_getpoint_player_rows"] == 18
    assert result["snapshot"]["nonblank_getpoint_player_rows"] == 3
    assert result["snapshot"]["json_null_getpoint_player_rows"] == 0
    assert result["cases"]["Q1"]["numerator"] == 1
    assert result["cases"]["Q1"]["denominator"] == 2
    assert result["cases"]["Q1"]["percentage"] == 50.0
    assert result["cases"]["Q1"]["active_scoring_denominator"] == 1
    assert result["cases"]["Q1"]["active_scoring_percentage"] == 100.0
    with snapshot.open("r", encoding="utf-8", newline="") as stream:
        naive_type_matches = sum(
            row["getpoint_player"] == "CHOU Tien Chen" and row["type"] == "殺球"
            for row in csv.DictReader(stream)
        )
    assert naive_type_matches == 2
    assert naive_type_matches > result["cases"]["Q1"]["numerator"]
    assert result["cases"]["Q32"]["long_rallies_at_least_10_events"] == 2
    assert result["cases"]["Q32"]["zhou_wins"] == 1
    assert result["cases"]["Q32"]["percentage"] == 50.0
    assert result["cases"]["Q60"]["eligible_losing_rallies"] == 1
    assert result["cases"]["Q60"]["top_shot_type"] == "長球"
    assert result["cases"]["Q60"]["top_count"] == 1


def test_csv_oracle_rejects_a_rally_with_only_blank_winner_values(
    tmp_path: Path,
) -> None:
    """只有非終局空白值的回合不得默默歸類為任一方勝負。"""

    snapshot = tmp_path / "events.csv"
    with snapshot.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SNAPSHOT_COLUMNS)
        writer.writeheader()
        writer.writerow(
            {
                "rally_id": "unfinished",
                "ball_round": "1.0",
                "player": "CHOU Tien Chen",
                "opponent": "Kento MOMOTA",
                "type": "長球",
                "getpoint_player": "",
                "landing_area": "1.0",
                "player_location_area": "1.0",
                "player_location_x": "1.5",
                "player_location_y": "2.5",
                "opponent_location_area": "4.0",
            }
        )

    with pytest.raises(AcceptanceError, match="缺少終局得分者"):
        compute_snapshot_oracle(snapshot)


def test_q7_coordinate_oracle_verifies_plotly_points_independent_of_order() -> None:
    """Q7 需比對每筆原始座標，不能只看樣本數或圖表是否出現。"""

    rows = [
        {"player_location_x": "1.5", "player_location_y": "2.5"},
        {"player_location_x": "3.5", "player_location_y": "4.5"},
        {"player_location_x": "1.5", "player_location_y": "2.5"},
        {"player_location_x": "", "player_location_y": "2.5"},
    ]
    oracle = {"player_location_xy": _location_xy_oracle(rows)}
    trace = {
        "type": "histogram2d",
        "histfunc": "count",
        "nbinsx": 30,
        "nbinsy": 30,
        "x": [3.5, 1.5, 1.5],
        "y": [4.5, 2.5, 2.5],
    }

    assert oracle["player_location_xy"]["excluded_unusable_coordinates"] == 1
    assert verify_q7_histogram2d({"data": [trace]}, oracle)["distinct_locations"] == 2
    trace["x"] = {
        "dtype": "f8",
        "bdata": base64.b64encode(struct.pack("<3d", 3.5, 1.5, 1.5)).decode("ascii"),
    }
    trace["y"] = {
        "dtype": "f8",
        "bdata": base64.b64encode(struct.pack("<3d", 4.5, 2.5, 2.5)).decode("ascii"),
    }
    assert (
        verify_q7_histogram2d({"data": [trace]}, oracle)["valid_coordinate_events"] == 3
    )
    trace["y"] = [4.5, 2.5, 2.0]
    with pytest.raises(AcceptanceError, match="snapshot 不符"):
        verify_q7_histogram2d({"data": [trace]}, oracle)


def test_live_response_comparison_checks_percentage_and_shot_type() -> None:
    """Q60 live 回答需同時符合獨立 oracle 的主球種與數值。"""

    oracle = {
        "top_shot_type": "長球",
        "top_count": 17,
        "eligible_losing_rallies": 62,
        "percentage": 27.42,
    }

    assert _compare_response("最常是長球，17/62，約 27.4%。", oracle) == "pass"
    assert _compare_response("最常是長球，17／62，約 27.42%。", oracle) == "pass"
    assert _compare_response("最常是殺球，17/62，約 27.4%。", oracle) == "mismatch"
    assert _compare_response("最常是長球，17/61，約 27.42%。", oracle) == "mismatch"
    assert _compare_response("最常是長球，約 31%。", oracle) == "mismatch"


def test_q1_response_uses_total_points_denominator_not_active_points() -> None:
    """原題的「總得分」必須比對全部得分回合，而非主動得分分母。"""

    oracle = {
        "numerator": 61,
        "denominator": 192,
        "percentage": 31.77,
        "active_scoring_denominator": 85,
        "active_scoring_percentage": 71.76,
    }

    assert (
        _compare_response(
            "殺球佔總得分 61/192=31.77%；主動得分內為 61/85=71.76%。", oracle
        )
        == "pass"
    )
    assert _compare_response("殺球佔總得分 61/85=71.76%。", oracle) == "mismatch"


def test_fixture_verification_does_not_need_private_snapshot(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """品質閘門可在未掛載 private runtime data 的 CI 驗證題集結構。"""

    from scripts.acceptance import main

    result = main(["--verify-fixtures"])

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output == {"fixture_status": "valid", "question_count": 12}
