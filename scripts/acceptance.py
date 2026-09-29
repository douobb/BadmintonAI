"""TASK-016 離線 oracle 與人工 live 驗收紀錄工具。"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import hashlib
import json
import math
import re
import struct
import sys
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUESTION_FILE = PROJECT_ROOT / "tests/fixtures/task_016_questions.json"
ORACLE_FILE = PROJECT_ROOT / "tests/fixtures/task_016_oracle.json"
DEFAULT_SNAPSHOT = PROJECT_ROOT / ".runtime/approved-data/processed_new_3.csv"
CHOU = "CHOU Tien Chen"
REQUIRED_COLUMNS = frozenset(
    {
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
    }
)
# 取 court_place.txt 空間矩陣最左 Col A 與最右 Col D 的 6 個區碼。
LEFT_SIDE_ZONES = frozenset({1, 5, 9, 13, 17, 21})
RIGHT_SIDE_ZONES = frozenset({4, 8, 12, 16, 20, 24})
SIDE_ZONES = LEFT_SIDE_ZONES | RIGHT_SIDE_ZONES
PERCENT_PATTERN = re.compile(r"(?<![\w.])(-?\d+(?:\.\d+)?)\s*%")


class AcceptanceError(ValueError):
    """驗收題集、snapshot 或 live 紀錄不符合契約。"""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"無法讀取 JSON：{path}") from exc


def validate_fixtures(
    question_file: Path = QUESTION_FILE,
    oracle_file: Path = ORACLE_FILE,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """驗證題目與預期結果的 ID、覆蓋類型及 snapshot provenance。"""

    questions = _load_json(question_file)
    oracle = _load_json(oracle_file)
    if not isinstance(questions, list) or not isinstance(oracle, dict):
        raise AcceptanceError("題集必須是 JSON array，oracle 必須是 JSON object")

    ids = [case.get("id") for case in questions]
    if len(ids) < 10 or any(not isinstance(item, str) for item in ids):
        raise AcceptanceError("題集至少需要 10 個具唯一字串 ID 的案例")
    if len(ids) != len(set(ids)):
        raise AcceptanceError("題集 ID 不可重複")
    required_categories = {
        "success",
        "no_data",
        "ambiguity",
        "tool_error",
        "follow_up",
        "chart",
    }
    present_categories = {
        category for case in questions for category in case.get("categories", [])
    }
    missing_categories = required_categories - present_categories
    if missing_categories:
        raise AcceptanceError("題集缺少類型：" + ", ".join(sorted(missing_categories)))

    oracle_cases = oracle.get("cases")
    if not isinstance(oracle_cases, dict):
        raise AcceptanceError("oracle 缺少 cases object")
    behavior_cases = oracle.get("behavior_cases", {})
    if not isinstance(behavior_cases, dict):
        raise AcceptanceError("oracle behavior_cases 必須是 JSON object")
    missing_oracles = sorted(
        {
            case["oracle_key"]
            for case in questions
            if case.get("oracle_key")
            and case["oracle_key"] not in oracle_cases
            and case["oracle_key"] not in behavior_cases
        }
    )
    if missing_oracles:
        raise AcceptanceError("oracle 缺少題目結果：" + ", ".join(missing_oracles))
    return questions, oracle


def _number(value: str, column: str, rally_id: str) -> Decimal:
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise AcceptanceError(
            f"rally_id={rally_id!r} 的 {column} 不是數值：{value!r}"
        ) from exc


def _zone(value: str) -> int | None:
    if value == "":
        return None
    try:
        return int(Decimal(value))
    except (InvalidOperation, ValueError):
        return None


def _zone_counts(rows: list[dict[str, str]], column: str) -> dict[str, Any]:
    valid: Counter[int] = Counter()
    undefined = 0
    missing = 0
    invalid = 0
    for row in rows:
        zone = _zone(row.get(column, ""))
        if zone is None:
            missing += 1
        elif 1 <= zone <= 32:
            valid[zone] += 1
        elif column == "landing_area" and zone == 33:
            undefined += 1
        else:
            invalid += 1
    return {
        "valid_zones": {str(zone): valid[zone] for zone in sorted(valid)},
        "undefined_landing_sentinel_33": undefined,
        "missing": missing,
        "invalid": invalid,
    }


def _coordinate_summary(pairs: list[tuple[float, float]]) -> dict[str, Any]:
    """以不依賴列順序的座標多重集合摘要核對圖表輸入資料。"""

    counts = Counter((0.0 if x == 0 else x, 0.0 if y == 0 else y) for x, y in pairs)
    digest = hashlib.sha256()
    for (x, y), count in sorted(counts.items()):
        digest.update(struct.pack("!ddI", x, y, count))
    return {
        "valid_coordinate_events": len(pairs),
        "distinct_locations": len(counts),
        "x_range": [
            min((x for x, _ in pairs), default=None),
            max((x for x, _ in pairs), default=None),
        ],
        "y_range": [
            min((y for _, y in pairs), default=None),
            max((y for _, y in pairs), default=None),
        ],
        "coordinate_multiset_sha256": digest.hexdigest(),
    }


def _location_xy_oracle(rows: list[dict[str, str]]) -> dict[str, Any]:
    pairs: list[tuple[float, float]] = []
    for row in rows:
        try:
            x = float(Decimal(row["player_location_x"]))
            y = float(Decimal(row["player_location_y"]))
        except (InvalidOperation, ValueError, OverflowError, TypeError):
            continue
        if math.isfinite(x) and math.isfinite(y):
            pairs.append((x, y))
    return {
        **_coordinate_summary(pairs),
        "excluded_unusable_coordinates": len(rows) - len(pairs),
        "source_fields": ["player_location_x", "player_location_y"],
    }


def _plotly_numeric_array(value: Any) -> list[float]:
    """讀取 Plotly JSON 的數值陣列或 float32/float64 typed array。"""

    if isinstance(value, list):
        numbers = value
    elif isinstance(value, dict) and value.get("dtype") in {"f4", "f8"}:
        code = "f" if value["dtype"] == "f4" else "d"
        width = struct.calcsize(code)
        encoded = value.get("bdata")
        if not isinstance(encoded, str):
            raise AcceptanceError("Q7 Plotly 座標缺少 bdata")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise AcceptanceError("Q7 Plotly bdata 無效") from exc
        if len(data) % width:
            raise AcceptanceError("Q7 Plotly bdata 長度與 dtype 不符")
        numbers = [item[0] for item in struct.iter_unpack("<" + code, data)]
    else:
        raise AcceptanceError("Q7 Plotly 座標格式無效")
    if any(
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not math.isfinite(item)
        for item in numbers
    ):
        raise AcceptanceError("Q7 Plotly 座標含非有限數值")
    return [float(item) for item in numbers]


def verify_q7_histogram2d(
    figure: dict[str, Any], oracle: dict[str, Any]
) -> dict[str, Any]:
    """比對實際熱區圖的全部原始站位點，而非只檢查圖表有顯示。"""

    traces = figure.get("data")
    if not isinstance(traces, list) or len(traces) != 1:
        raise AcceptanceError("Q7 應只有一個熱區 trace")
    trace = traces[0]
    if not isinstance(trace, dict) or trace.get("type") != "histogram2d":
        raise AcceptanceError("Q7 圖表不是 histogram2d 熱區圖")
    if trace.get("histfunc", "count") != "count":
        raise AcceptanceError("Q7 熱區圖不是以事件數計算")
    if (trace.get("nbinsx"), trace.get("nbinsy")) != (30, 30):
        raise AcceptanceError("Q7 熱區圖要求的網格數不是 30×30")
    x = _plotly_numeric_array(trace.get("x"))
    y = _plotly_numeric_array(trace.get("y"))
    if len(x) != len(y):
        raise AcceptanceError("Q7 熱區圖 X/Y 座標筆數不同")
    actual = _coordinate_summary(list(zip(x, y)))
    expected = oracle.get("player_location_xy")
    if not isinstance(expected, dict):
        raise AcceptanceError("Q7 oracle 缺少獨立站位座標摘要")
    for key in ("valid_coordinate_events", "coordinate_multiset_sha256"):
        if actual[key] != expected.get(key):
            raise AcceptanceError(f"Q7 圖表的 {key} 與固定 snapshot 不符")
    return actual


def _sorted_rallies(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["rally_id"]].append(row)
    for rally_id, events in grouped.items():
        events.sort(key=lambda row: _number(row["ball_round"], "ball_round", rally_id))
        rounds = [_number(row["ball_round"], "ball_round", rally_id) for row in events]
        if len(rounds) != len(set(rounds)):
            raise AcceptanceError(f"rally_id={rally_id!r} 有重複 ball_round")
    return grouped


def compute_snapshot_oracle(snapshot_path: Path) -> dict[str, Any]:
    """直接以標準函式庫解析原始 CSV，獨立計算 TASK-016 核心基準。"""

    try:
        snapshot_bytes = snapshot_path.read_bytes()
        with snapshot_path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            columns = frozenset(reader.fieldnames or ())
            missing_columns = REQUIRED_COLUMNS - columns
            if missing_columns:
                raise AcceptanceError(
                    "snapshot 缺欄位：" + ", ".join(sorted(missing_columns))
                )
            rows = list(reader)
    except AcceptanceError:
        raise
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        raise AcceptanceError(f"無法解析 snapshot：{snapshot_path}") from exc

    rallies = _sorted_rallies(rows)
    terminals: dict[str, dict[str, str]] = {}
    for rally_id, events in rallies.items():
        terminal = events[-1]
        winner = terminal.get("getpoint_player", "")
        if not winner:
            raise AcceptanceError(f"rally_id={rally_id!r} 缺少終局得分者")
        if any(event.get("getpoint_player", "") for event in events[:-1]):
            raise AcceptanceError(f"rally_id={rally_id!r} 非終局列含得分者")
        terminals[rally_id] = terminal

    chou_points = [row for row in terminals.values() if row["getpoint_player"] == CHOU]
    direct_wins = [row for row in chou_points if row["player"] == CHOU]

    def direct_score_rate(shot_type: str) -> dict[str, Any]:
        numerator = sum(row["type"] == shot_type for row in direct_wins)
        denominator = len(chou_points)
        active_denominator = len(direct_wins)
        return {
            "shot_type": shot_type,
            "numerator": numerator,
            "denominator": denominator,
            "percentage": round(numerator * 100 / denominator, 2)
            if denominator
            else None,
            "active_scoring_denominator": active_denominator,
            "active_scoring_percentage": round(numerator * 100 / active_denominator, 2)
            if active_denominator
            else None,
            "definition": (
                "「佔周天成總得分」分母包含對手失誤送分；另列「在周天成主動得分中比例」"
            ),
        }

    chou_smashes = [
        row for row in rows if row["player"] == CHOU and row["type"] == "殺球"
    ]
    chou_backcourt = [
        row
        for row in rows
        if row["player"] == CHOU and _zone(row["player_location_area"]) in {1, 2, 3, 4}
    ]
    chou_side_context = [
        row
        for row in rows
        if row["player"] == CHOU and _zone(row["opponent_location_area"]) in SIDE_ZONES
    ]

    long_rallies = {
        rally_id: events for rally_id, events in rallies.items() if len(events) >= 10
    }
    long_chou_wins = sum(
        terminals[rally_id]["getpoint_player"] == CHOU for rally_id in long_rallies
    )
    long_rally_count = len(long_rallies)

    eligible_last_two: list[dict[str, str]] = []
    for rally_id, events in rallies.items():
        if terminals[rally_id]["getpoint_player"] != CHOU:
            second_last = events[-2] if len(events) >= 2 else None
            if second_last is not None and second_last["player"] == CHOU:
                eligible_last_two.append(second_last)
    last_two_counts = Counter(row["type"] for row in eligible_last_two)

    backcourt_counts = Counter(row["type"] for row in chou_backcourt)
    side_landing_counts = Counter(
        str(_zone(row["landing_area"]))
        for row in chou_side_context
        if _zone(row["landing_area"]) is not None
    )
    available_players = sorted(
        {row["player"] for row in rows} | {row["opponent"] for row in rows},
        key=str.casefold,
    )
    blank_winner_rows = sum(row["getpoint_player"] == "" for row in rows)

    return {
        "snapshot": {
            "file": snapshot_path.name,
            "sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
            "row_count": len(rows),
            "rally_count": len(rallies),
            "blank_getpoint_player_rows": blank_winner_rows,
            "nonblank_getpoint_player_rows": len(rows) - blank_winner_rows,
            "json_null_getpoint_player_rows": 0,
        },
        "cases": {
            "Q1": direct_score_rate("殺球"),
            "Q3": {
                "sample_events": len(chou_smashes),
                "landing_area": _zone_counts(chou_smashes, "landing_area"),
            },
            "Q5": {
                "mapping_source": ".runtime/approved-data/metadata/court_place.txt",
                "ambiguity_note": (
                    "自然題可解讀為周天成贏下回合的終局落點，"
                    "或本人直接擊出得分的終局落點。"
                ),
                "accepted_interpretations": {
                    "zhou_winning_rallies_terminal_event": {
                        "sample_events": len(chou_points),
                        "landing_area": _zone_counts(chou_points, "landing_area"),
                        "definition": "周天成贏下的每個回合之終局事件落點",
                    },
                    "zhou_direct_winning_stroke": {
                        "sample_events": len(direct_wins),
                        "landing_area": _zone_counts(direct_wins, "landing_area"),
                        "definition": "僅計周天成本人擊出且得分的終局事件落點",
                    },
                },
            },
            "Q7": {
                "sample_events": len(chou_smashes),
                "player_location_area": _zone_counts(
                    chou_smashes, "player_location_area"
                ),
                "player_location_xy": _location_xy_oracle(chou_smashes),
            },
            "Q8": {
                "sample_events": len(chou_backcourt),
                "top_three": [
                    {"shot_type": shot_type, "count": count}
                    for shot_type, count in backcourt_counts.most_common(3)
                ],
                "court_zones": [1, 2, 3, 4],
            },
            "Q25": {
                "sample_events": len(chou_side_context),
                "mapping_source": (
                    ".runtime/approved-data/metadata/court_place.txt, "
                    "Spatial Relationships matrix: Col A and Col D"
                ),
                "left_side_zones": sorted(LEFT_SIDE_ZONES),
                "right_side_zones": sorted(RIGHT_SIDE_ZONES),
                "side_zones": sorted(SIDE_ZONES),
                "landing_area_counts": {
                    zone: side_landing_counts[zone]
                    for zone in sorted(side_landing_counts, key=int)
                    if 1 <= int(zone) <= 32
                },
                "undefined_landing_sentinel_33": sum(
                    _zone(row["landing_area"]) == 33 for row in chou_side_context
                ),
            },
            "Q32": {
                "long_rallies_at_least_10_events": long_rally_count,
                "zhou_wins": long_chou_wins,
                "numerator": long_chou_wins,
                "denominator": long_rally_count,
                "percentage": round(long_chou_wins * 100 / long_rally_count, 2)
                if long_rally_count
                else None,
                "definition": "以 rally_id 分組；至少 10 拍，終局得分者為周天成",
            },
            "Q60": {
                "eligible_losing_rallies": len(eligible_last_two),
                "top_shot_type": last_two_counts.most_common(1)[0][0]
                if last_two_counts
                else None,
                "top_count": last_two_counts.most_common(1)[0][1]
                if last_two_counts
                else 0,
                "percentage": round(
                    last_two_counts.most_common(1)[0][1] * 100 / len(eligible_last_two),
                    2,
                )
                if eligible_last_two
                else None,
                "counts": dict(sorted(last_two_counts.items())),
                "definition": "周天成失分回合的整體倒數第二拍，僅納入該拍由周天成擊出的回合",
            },
            "FOLLOWUP": {
                shot_type: direct_score_rate(shot_type)
                for shot_type in ("殺球", "切球", "平球", "挑球")
            },
            "NO_DATA": {
                "searched_player": "不存在球員測試",
                "matching_rows": sum(
                    row["player"] == "不存在球員測試"
                    or row["opponent"] == "不存在球員測試"
                    for row in rows
                ),
                "available_players": available_players,
                "expected_behavior": "明確回覆沒有資料，不推測數字",
            },
        },
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _verify_oracle(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    expected_snapshot = expected.get("snapshot")
    if not isinstance(expected_snapshot, dict):
        raise AcceptanceError("oracle 缺少 snapshot provenance")
    for field in ("file", "sha256", "row_count", "rally_count"):
        if actual["snapshot"].get(field) != expected_snapshot.get(field):
            raise AcceptanceError(
                f"snapshot {field} 與固定 oracle 不符；請勿直接重用舊預期值"
            )
    for case_id, oracle_result in expected["cases"].items():
        if case_id not in actual["cases"]:
            continue
        if _canonical_json(actual["cases"][case_id]) != _canonical_json(oracle_result):
            raise AcceptanceError(f"{case_id} oracle 與目前 snapshot 計算結果不符")


def _compare_response(response: str, oracle_result: dict[str, Any]) -> str:
    """對可抽取的數值/球種作保守比較，其餘保留人工複核。"""

    required_text: list[str] = []
    top_type = oracle_result.get("top_shot_type")
    if isinstance(top_type, str):
        required_text.append(top_type)
    top_entries = oracle_result.get("top_three")
    if isinstance(top_entries, list):
        required_text.extend(
            item["shot_type"] for item in top_entries if item.get("shot_type")
        )
    if any(text.casefold() not in response.casefold() for text in required_text):
        return "mismatch"

    percentage = oracle_result.get("percentage")
    if isinstance(percentage, (float, int)):
        numerator = oracle_result.get("numerator", oracle_result.get("top_count"))
        denominator = oracle_result.get(
            "denominator", oracle_result.get("eligible_losing_rallies")
        )
        if isinstance(numerator, int) and isinstance(denominator, int):
            fraction = re.compile(rf"\b{numerator}\s*[/／]\s*{denominator}\b")
            if fraction.search(response) is None:
                return "mismatch"
        found = [float(value) for value in PERCENT_PATTERN.findall(response)]
        if not any(abs(value - float(percentage)) <= 0.05 for value in found):
            return "mismatch"
        return "pass"
    return "manual_review"


def build_report(
    *,
    selected_ids: list[str] | None = None,
    snapshot_path: Path = DEFAULT_SNAPSHOT,
    question_file: Path = QUESTION_FILE,
    oracle_file: Path = ORACLE_FILE,
    records_file: Path | None = None,
) -> dict[str, Any]:
    questions, expected = validate_fixtures(question_file, oracle_file)
    by_id = {case["id"]: case for case in questions}
    selected = list(by_id) if selected_ids is None else selected_ids
    unknown_ids = sorted(set(selected) - set(by_id))
    if unknown_ids:
        raise AcceptanceError("未知題目 ID：" + ", ".join(unknown_ids))

    actual = compute_snapshot_oracle(snapshot_path)
    _verify_oracle(actual, expected)
    saved_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if records_file is not None:
        record_payload = _load_json(records_file)
        runs = record_payload.get("runs") if isinstance(record_payload, dict) else None
        if not isinstance(runs, list):
            raise AcceptanceError('live 紀錄需使用 {"runs": [...]} 格式')
        for run in runs:
            if run.get("case_id") not in by_id:
                raise AcceptanceError(f"live 紀錄含未知 case_id：{run.get('case_id')}")
            saved_records[run["case_id"]].append(run)

    report_cases = []
    for case_id in selected:
        case = by_id[case_id]
        oracle_key = case.get("oracle_key")
        oracle_result = None
        if oracle_key:
            oracle_result = actual["cases"].get(oracle_key)
            if oracle_result is None:
                oracle_result = expected.get("behavior_cases", {}).get(oracle_key)
        if oracle_result is None:
            oracle_result = case.get("expected")
        live_runs = []
        for run in saved_records.get(case_id, []):
            tool_calls = run.get("tool_calls", [])
            response = run.get("assistant_response", "")
            if not isinstance(tool_calls, list) or not isinstance(response, str):
                raise AcceptanceError(f"{case_id} live 紀錄欄位型別錯誤")
            live_runs.append(
                {
                    "label": run.get("label"),
                    "tool_call_count": len(tool_calls),
                    "tool_calls": tool_calls,
                    "assistant_response": response,
                    "oracle_comparison": _compare_response(response, oracle_result)
                    if isinstance(oracle_result, dict)
                    else "manual_review",
                    "manual_assessment": run.get("manual_assessment"),
                    "notes": run.get("notes"),
                }
            )
        report_cases.append(
            {
                "id": case_id,
                "source": case.get("source"),
                "categories": case.get("categories", []),
                "turns": case.get("turns", []),
                "setup": case.get("setup"),
                "acceptance_criteria": case.get("acceptance_criteria"),
                "oracle": oracle_result,
                "live_runs": live_runs,
            }
        )
    return {
        "snapshot": actual["snapshot"],
        "oracle_verified": True,
        "selected_count": len(report_cases),
        "cases": report_cases,
        "comparison_note": (
            "比例結果比對分子/分母、百分比及必要球種；圖表、澄清與錯誤處理"
            "仍需人工檢視附件/對話。工具呼叫數由每筆 live 紀錄的 tool_calls 計算。"
        ),
    }


def _template(ids: list[str]) -> dict[str, Any]:
    return {
        "runs": [
            {
                "case_id": case_id,
                "label": "填入日期/模型/新聊天識別資訊（勿放憑證）",
                "tool_calls": [],
                "assistant_response": "",
                "manual_assessment": None,
                "notes": "圖表可記錄可見性與檔名；不要把回答當作獨立 oracle。",
            }
            for case_id in ids
        ]
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="列出題目 ID 與分類")
    parser.add_argument("--ids", nargs="+", help="只輸出指定題目，例如 Q32 Q60")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--records", type=Path, help="匯入人工 live 紀錄 JSON")
    parser.add_argument(
        "--record-template", action="store_true", help="輸出所選題目的 live 紀錄範本"
    )
    parser.add_argument(
        "--verify-fixtures",
        action="store_true",
        help="檢查題集/預期值結構，不需 snapshot",
    )
    args = parser.parse_args(argv)

    try:
        questions, _ = validate_fixtures()
        by_id = {case["id"]: case for case in questions}
        ids = list(by_id) if args.ids is None else args.ids
        unknown_ids = sorted(set(ids) - set(by_id))
        if unknown_ids:
            raise AcceptanceError("未知題目 ID：" + ", ".join(unknown_ids))
        if args.list:
            payload = [
                {"id": case["id"], "categories": case.get("categories", [])}
                for case in questions
            ]
        elif args.verify_fixtures:
            payload = {"fixture_status": "valid", "question_count": len(questions)}
        elif args.record_template:
            payload = _template(ids)
        else:
            payload = build_report(
                selected_ids=ids,
                snapshot_path=args.snapshot,
                records_file=args.records,
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except AcceptanceError as exc:
        print(f"acceptance error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
