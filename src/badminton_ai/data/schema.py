"""依 v2 DATA_CONTRACT 驗證逐拍資料的結構與終局語意。"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .constants import (
    APPROVED_SHOT_TYPES,
    COURT_ZONE_CODES,
    MVP_REQUIRED_COLUMNS,
    UNDEFINED_LANDING_AREA_CODE,
)
from .errors import SchemaValidationError
from .models import DatasetSnapshot

_DECIMAL_INTEGER_PATTERN = re.compile(r"[0-9]+(?:\.0+)?\Z")


@dataclass(frozen=True)
class _ParsedEvent:
    """供 validator 內部使用的型別化事件。"""

    row_number: int
    match_id: str
    set_number: int
    rally: int
    rally_id: str
    ball_round: int
    player: str
    opponent: str
    shot_type: str
    getpoint_player: str | None
    win_reason: str | None
    lose_reason: str | None
    rally_key: tuple[str, int, int]


def validate_snapshot(
    snapshot: DatasetSnapshot,
    *,
    approved_shot_types: Iterable[str] = APPROVED_SHOT_TYPES,
) -> DatasetSnapshot:
    """驗證 snapshot，成功時回傳原 snapshot，失敗時拋出領域例外。

    驗證包含必要欄位、事件鍵、回合順序、球員與球種、場區代碼，以及每
    個 rally 恰有一筆位於最大 ``ball_round`` 的終局 winner。
    """

    missing_columns = tuple(
        column for column in MVP_REQUIRED_COLUMNS if column not in snapshot.columns
    )
    if missing_columns:
        raise SchemaValidationError(f"資料缺少必要欄位：{', '.join(missing_columns)}")

    accepted_shot_types = frozenset(approved_shot_types)
    events_by_rally: dict[tuple[str, int, int], list[_ParsedEvent]] = {}
    seen_event_keys: dict[tuple[str, int, int, int], int] = {}
    seen_rally_ids: dict[str, tuple[tuple[str, int, int], int]] = {}

    for row_number, row in enumerate(snapshot.rows, start=1):
        event = _parse_event(
            row_number=row_number,
            row=row,
            accepted_shot_types=accepted_shot_types,
        )
        previous_rally = seen_rally_ids.get(event.rally_id)
        if previous_rally is not None and previous_rally[0] != event.rally_key:
            previous_key, previous_row = previous_rally
            raise SchemaValidationError(
                f"rally_id {event.rally_id!r} 不得指向多個回合："
                f"第 {previous_row} 列 rally {previous_key} 與"
                f"第 {row_number} 列 rally {event.rally_key}",
                row_number=row_number,
                rally_key=event.rally_key,
            )
        seen_rally_ids.setdefault(event.rally_id, (event.rally_key, row_number))
        event_key = (
            event.match_id,
            event.set_number,
            event.rally,
            event.ball_round,
        )
        previous_row = seen_event_keys.get(event_key)
        if previous_row is not None:
            raise _row_error(
                event,
                "事件鍵 (match_id, set, rally, ball_round) 重複，"
                f"首次出現在第 {previous_row} 列",
            )
        seen_event_keys[event_key] = row_number
        events_by_rally.setdefault(event.rally_key, []).append(event)

    for rally_key, events in events_by_rally.items():
        _validate_rally(rally_key, events)

    return snapshot


def _parse_event(
    *,
    row_number: int,
    row: Mapping[str, Any],
    accepted_shot_types: frozenset[str],
) -> _ParsedEvent:
    missing_columns = tuple(
        column for column in MVP_REQUIRED_COLUMNS if column not in row
    )
    if missing_columns:
        raise SchemaValidationError(
            f"資料列 {row_number} 缺少必要欄位：{', '.join(missing_columns)}",
            row_number=row_number,
        )

    match_id = _required_text(row["match_id"], row_number, "match_id")
    set_number = _parse_positive_int(row["set"], row_number, "set")
    if set_number not in {1, 2, 3}:
        raise SchemaValidationError(
            f"資料列 {row_number} 的 set 必須是 1、2 或 3",
            row_number=row_number,
        )
    rally = _parse_positive_int(row["rally"], row_number, "rally")
    rally_id = _required_text(row["rally_id"], row_number, "rally_id")
    ball_round = _parse_positive_int(row["ball_round"], row_number, "ball_round")
    player = _required_text(row["player"], row_number, "player")
    opponent = _required_text(row["opponent"], row_number, "opponent")
    if player.casefold() == opponent.casefold():
        raise SchemaValidationError(
            f"資料列 {row_number} 的 player 與 opponent 不得相同",
            row_number=row_number,
        )

    shot_type = _required_text(row["type"], row_number, "type")
    if shot_type not in accepted_shot_types:
        raise SchemaValidationError(
            f"資料列 {row_number} 的 type 不是核准球種：{shot_type!r}",
            row_number=row_number,
        )

    _parse_landing_area(row["landing_area"], row_number)
    _parse_zone(
        row["player_location_area"],
        row_number,
        "player_location_area",
    )

    getpoint_player = _optional_text(row["getpoint_player"])
    win_reason = _optional_text(row["win_reason"])
    lose_reason = _optional_text(row["lose_reason"])
    rally_key = (match_id, set_number, rally)
    return _ParsedEvent(
        row_number=row_number,
        match_id=match_id,
        set_number=set_number,
        rally=rally,
        rally_id=rally_id,
        ball_round=ball_round,
        player=player,
        opponent=opponent,
        shot_type=shot_type,
        getpoint_player=getpoint_player,
        win_reason=win_reason,
        lose_reason=lose_reason,
        rally_key=rally_key,
    )


def _validate_rally(
    rally_key: tuple[str, int, int],
    events: list[_ParsedEvent],
) -> None:
    first_event = events[0]
    expected_rounds = list(range(1, len(events) + 1))
    actual_rounds = [event.ball_round for event in events]
    if actual_rounds != expected_rounds:
        raise _rally_error(
            rally_key,
            first_event.row_number,
            "同一 rally 的 ball_round 必須依輸入順序從 1 連續遞增",
        )

    rally_ids = {event.rally_id for event in events}
    if len(rally_ids) != 1:
        raise _rally_error(
            rally_key,
            first_event.row_number,
            "同一 rally 的 rally_id 必須一致",
        )

    terminal_event = events[-1]
    winner_events = [event for event in events if event.getpoint_player is not None]
    if len(winner_events) != 1:
        raise _rally_error(
            rally_key,
            first_event.row_number,
            "每個 rally 必須恰有一筆 getpoint_player",
        )

    winner_event = winner_events[0]
    if winner_event.ball_round != terminal_event.ball_round:
        raise _row_error(
            winner_event,
            "getpoint_player 只能出現在最大 ball_round 的終局事件",
        )

    participant_names = {event.player.casefold() for event in events}
    participant_names.update(event.opponent.casefold() for event in events)
    assert winner_event.getpoint_player is not None
    if winner_event.getpoint_player.casefold() not in participant_names:
        raise _row_error(
            winner_event,
            "getpoint_player 必須是該 rally 的參賽者",
        )

    for event in events[:-1]:
        if event.win_reason is not None or event.lose_reason is not None:
            raise _row_error(
                event,
                "非終局事件不得有 win_reason 或 lose_reason",
            )


def _required_text(value: Any, row_number: int, field: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise SchemaValidationError(
            f"資料列 {row_number} 的 {field} 不得為空",
            row_number=row_number,
        )
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_positive_int(value: Any, row_number: int, field: str) -> int:
    if isinstance(value, bool):
        parsed = None
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        parsed = int(value) if math.isfinite(value) and value.is_integer() else None
    elif isinstance(value, str):
        text = value.strip()
        if _DECIMAL_INTEGER_PATTERN.fullmatch(text) is not None:
            parsed = int(text.split(".", maxsplit=1)[0])
        else:
            parsed = None
    else:
        parsed = None

    if parsed is None or parsed < 1:
        raise SchemaValidationError(
            f"資料列 {row_number} 的 {field} 必須是正整數",
            row_number=row_number,
        )
    return parsed


def _parse_zone(value: Any, row_number: int, field: str) -> int:
    parsed = _parse_positive_int(value, row_number, field)
    if parsed not in COURT_ZONE_CODES:
        raise SchemaValidationError(
            f"資料列 {row_number} 的 {field} 必須是 1 到 32 的場區代碼",
            row_number=row_number,
        )
    return parsed


def _parse_landing_area(value: Any, row_number: int) -> int:
    """解析落點；33 僅代表未定義落點，不是正式球場網格。"""

    parsed = _parse_positive_int(value, row_number, "landing_area")
    if parsed not in COURT_ZONE_CODES and parsed != UNDEFINED_LANDING_AREA_CODE:
        raise SchemaValidationError(
            f"資料列 {row_number} 的 landing_area 必須是 1 到 32 的場區代碼，"
            f"或 {UNDEFINED_LANDING_AREA_CODE} 的未定義落點 sentinel",
            row_number=row_number,
        )
    return parsed


def _row_error(event: _ParsedEvent, message: str) -> SchemaValidationError:
    return SchemaValidationError(
        f"資料列 {event.row_number}、rally {event.rally_key}：{message}",
        row_number=event.row_number,
        rally_key=event.rally_key,
    )


def _rally_error(
    rally_key: tuple[str, int, int],
    row_number: int,
    message: str,
) -> SchemaValidationError:
    return SchemaValidationError(
        f"資料列 {row_number}、rally {rally_key}：{message}",
        row_number=row_number,
        rally_key=rally_key,
    )
