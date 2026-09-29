"""BadmintonAI v2 的結構化、唯讀逐拍查詢服務。"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ..data.constants import APPROVED_SHOT_TYPES
from ..data.errors import (
    AmbiguousPlayerAliasError,
    DataError,
    UnknownPlayerAliasError,
)
from ..data.models import DatasetSnapshot, MetadataSnapshot
from ..data.schema import validate_snapshot

DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 500
_DECIMAL_INTEGER_PATTERN = re.compile(r"[0-9]+(?:\.0+)?\Z")


class QueryError(DataError):
    """查詢服務的領域錯誤基底類別。"""


class QueryValidationError(QueryError):
    """查詢輸入不符合結構化服務契約。"""


class UnknownMatchError(QueryError):
    """要求的 match_id 不存在於目前 snapshot。"""


def _sequence_values(value: Any, field: str) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)):
        return (value,)
    if isinstance(value, Mapping):
        raise QueryValidationError(f"{field} 不接受 mapping")
    if not isinstance(value, Iterable):
        return (value,)
    try:
        return tuple(value)
    except TypeError as exc:
        raise QueryValidationError(f"{field} 必須是單值或可迭代值") from exc


def _normalize_text_values(value: Any, field: str) -> tuple[str, ...]:
    normalized: set[str] = set()
    for item in _sequence_values(value, field):
        if not isinstance(item, str) or not item.strip():
            raise QueryValidationError(f"{field} 必須是非空文字或文字集合")
        normalized.add(item.strip())
    return tuple(sorted(normalized, key=lambda item: (item.casefold(), item)))


def _normalize_integer_values(value: Any, field: str) -> tuple[int, ...]:
    normalized: set[int] = set()
    for item in _sequence_values(value, field):
        if isinstance(item, bool) or not isinstance(item, int):
            raise QueryValidationError(f"{field} 必須是整數或整數集合")
        if item < 1:
            raise QueryValidationError(f"{field} 必須是正整數")
        if field == "set_numbers" and item not in {1, 2, 3}:
            raise QueryValidationError("set_numbers 只允許 1、2 或 3")
        normalized.add(item)
    return tuple(sorted(normalized))


def _validate_optional_positive_integer(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise QueryValidationError(f"{field} 必須是正整數或 None")
    if value < 1:
        raise QueryValidationError(f"{field} 必須是正整數或 None")
    return value


@dataclass(frozen=True)
class EventFilter:
    """可組合的逐拍查詢條件；同欄多值 OR、不同欄位 AND。"""

    match_ids: tuple[str, ...] | str | None = None
    set_numbers: tuple[int, ...] | int | None = None
    rally_ids: tuple[str, ...] | str | None = None
    players: tuple[str, ...] | str | None = None
    opponents: tuple[str, ...] | str | None = None
    shot_types: tuple[str, ...] | str | None = None
    ball_round_min: int | None = None
    ball_round_max: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "match_ids",
            _normalize_text_values(self.match_ids, "match_ids"),
        )
        object.__setattr__(
            self,
            "set_numbers",
            _normalize_integer_values(self.set_numbers, "set_numbers"),
        )
        object.__setattr__(
            self,
            "rally_ids",
            _normalize_text_values(self.rally_ids, "rally_ids"),
        )
        object.__setattr__(
            self,
            "players",
            _normalize_text_values(self.players, "players"),
        )
        object.__setattr__(
            self,
            "opponents",
            _normalize_text_values(self.opponents, "opponents"),
        )
        object.__setattr__(
            self,
            "shot_types",
            _normalize_text_values(self.shot_types, "shot_types"),
        )

        minimum = _validate_optional_positive_integer(
            self.ball_round_min,
            "ball_round_min",
        )
        maximum = _validate_optional_positive_integer(
            self.ball_round_max,
            "ball_round_max",
        )
        if minimum is not None and maximum is not None and minimum > maximum:
            raise QueryValidationError("ball_round_min 不得大於 ball_round_max")
        object.__setattr__(self, "ball_round_min", minimum)
        object.__setattr__(self, "ball_round_max", maximum)

    @property
    def sets(self) -> tuple[int, ...]:
        """``set_numbers`` 的簡短唯讀別名。"""

        return self.set_numbers


@dataclass(frozen=True)
class QueryPage:
    """不可變分頁查詢結果與有效條件。"""

    total_count: int
    offset: int
    limit: int
    rows: tuple[Mapping[str, Any], ...]
    filters: EventFilter
    source: Path
    columns: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "rows",
            tuple(MappingProxyType(dict(row)) for row in self.rows),
        )
        object.__setattr__(self, "source", Path(self.source).resolve(strict=False))
        object.__setattr__(self, "columns", tuple(self.columns))

    @property
    def total(self) -> int:
        """``total_count`` 的簡短唯讀別名。"""

        return self.total_count

    @property
    def effective_filters(self) -> EventFilter:
        """回傳已套用 alias 與輸入正規化的條件。"""

        return self.filters

    @property
    def data_source(self) -> Path:
        """回傳來源 snapshot 的正規化路徑。"""

        return self.source


@dataclass(frozen=True)
class MatchSummary:
    """比賽的 deterministic 結構摘要。"""

    match_id: str
    players: tuple[str, ...]
    sets: tuple[int, ...]
    rally_count: int
    event_count: int

    @property
    def participants(self) -> tuple[str, ...]:
        """``players`` 的語意別名。"""

        return self.players


def _value_as_text(value: Any) -> str:
    return str(value).strip()


def _validated_integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise QueryValidationError(f"資料欄位 {field} 不是有效正整數")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        parsed = int(value) if math.isfinite(value) and value.is_integer() else None
    elif isinstance(value, str):
        text = value.strip()
        if _DECIMAL_INTEGER_PATTERN.fullmatch(text) is None:
            parsed = None
        else:
            parsed = int(text.split(".", maxsplit=1)[0])
    else:
        parsed = None
    if parsed is None or parsed < 1:
        raise QueryValidationError(f"資料欄位 {field} 不是有效正整數")
    return parsed


class BadmintonQueryService:
    """在已通過 canonical schema 的 snapshot 上提供唯讀結構化查詢。"""

    def __init__(
        self,
        snapshot: DatasetSnapshot,
        metadata: MetadataSnapshot | None = None,
    ) -> None:
        if not isinstance(snapshot, DatasetSnapshot):
            raise QueryValidationError("query service 必須接收 DatasetSnapshot")
        validated_snapshot = validate_snapshot(snapshot)
        if metadata is not None and not isinstance(metadata, MetadataSnapshot):
            raise QueryValidationError("metadata 必須是 MetadataSnapshot 或 None")

        self._snapshot = validated_snapshot
        self._metadata = metadata
        self._columns = tuple(validated_snapshot.columns)
        self._rows = tuple(validated_snapshot.rows)
        self._player_canonical_cache = self._build_player_canonical_cache()
        self._player_index = self._build_player_index()

    @property
    def snapshot(self) -> DatasetSnapshot:
        """回傳原始不可變 snapshot。"""

        return self._snapshot

    @property
    def metadata(self) -> MetadataSnapshot | None:
        """回傳可選的唯讀 metadata snapshot。"""

        return self._metadata

    def query_events(
        self,
        filters: EventFilter | None = None,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> QueryPage:
        """依結構化條件查詢事件並安全分頁。"""

        effective_filters = self._resolve_filter(filters)
        page_limit, page_offset = self._validate_pagination(limit, offset)
        matched = self._matching_rows(effective_filters)
        page_rows = matched[page_offset : page_offset + page_limit]
        return QueryPage(
            total_count=len(matched),
            offset=page_offset,
            limit=page_limit,
            rows=tuple(self._copy_row(row) for row in page_rows),
            filters=effective_filters,
            source=self._snapshot.source,
            columns=self._columns,
        )

    def list_matches(
        self, filters: EventFilter | None = None
    ) -> tuple[MatchSummary, ...]:
        """列出符合條件的比賽摘要，依 match_id deterministic 排序。"""

        effective_filters = self._resolve_filter(filters)
        return self._summarize_matches(self._matching_rows(effective_filters))

    def get_match(self, match_id: str) -> MatchSummary:
        """取得指定比賽摘要；未知 match_id 拋出穩定領域錯誤。"""

        if not isinstance(match_id, str) or not match_id.strip():
            raise QueryValidationError("match_id 必須是非空文字")
        target = match_id.strip()
        rows = tuple(
            row for row in self._rows if _value_as_text(row["match_id"]) == target
        )
        if not rows:
            raise UnknownMatchError(f"未知 match_id：{target!r}")
        return self._summarize_matches(rows)[0]

    def list_players(
        self,
        filters: EventFilter | None = None,
    ) -> tuple[str, ...]:
        """列出符合條件事件中的 canonical 球員名稱。"""

        effective_filters = self._resolve_filter(filters)
        players: set[str] = set()
        for row in self._matching_rows(effective_filters):
            players.add(self._canonical_player(row["player"]))
            players.add(self._canonical_player(row["opponent"]))
        return tuple(sorted(players, key=lambda item: (item.casefold(), item)))

    def _build_player_index(self) -> Mapping[str, tuple[str, ...]]:
        names: set[str] = set()
        for row in self._rows:
            names.add(_value_as_text(row["player"]))
            names.add(_value_as_text(row["opponent"]))
        grouped: dict[str, set[str]] = {}
        for name in names:
            grouped.setdefault(name.casefold(), set()).add(name)
        return MappingProxyType(
            {
                key: tuple(sorted(values, key=lambda item: (item.casefold(), item)))
                for key, values in grouped.items()
            }
        )

    def _build_player_canonical_cache(self) -> Mapping[str, str]:
        raw_names: set[str] = set()
        for row in self._rows:
            raw_names.add(_value_as_text(row["player"]))
            raw_names.add(_value_as_text(row["opponent"]))
            winner = _value_as_text(row["getpoint_player"])
            if winner:
                raw_names.add(winner)

        canonical_names: dict[str, str] = {}
        for raw_name in raw_names:
            if self._metadata is None:
                canonical_names[raw_name] = raw_name
            else:
                canonical_names[raw_name] = self._metadata.normalize_player(raw_name)
        return MappingProxyType(canonical_names)

    def _canonical_player(self, value: Any) -> str:
        text = _value_as_text(value)
        try:
            return self._player_canonical_cache[text]
        except KeyError as exc:
            raise UnknownPlayerAliasError(f"未知球員 alias：{text!r}") from exc

    def _resolve_filter(self, filters: EventFilter | None) -> EventFilter:
        if filters is None:
            filters = EventFilter()
        if not isinstance(filters, EventFilter):
            raise QueryValidationError("filters 必須是 EventFilter 或 None")
        return EventFilter(
            match_ids=filters.match_ids,
            set_numbers=filters.set_numbers,
            rally_ids=filters.rally_ids,
            players=self._resolve_players(filters.players),
            opponents=self._resolve_players(filters.opponents),
            shot_types=self._resolve_shot_types(filters.shot_types),
            ball_round_min=filters.ball_round_min,
            ball_round_max=filters.ball_round_max,
        )

    def _resolve_players(self, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            return ()
        if self._metadata is not None:
            return tuple(
                sorted(
                    {self._metadata.normalize_player(value) for value in values},
                    key=lambda item: (item.casefold(), item),
                )
            )

        resolved: set[str] = set()
        for value in values:
            candidates = self._player_index.get(value.casefold(), ())
            if not candidates:
                raise UnknownPlayerAliasError(f"未知球員 alias：{value!r}")
            if len(candidates) > 1:
                raise AmbiguousPlayerAliasError(
                    f"球員 alias {value!r} 對應多個 canonical 名稱："
                    f"{', '.join(candidates)}"
                )
            resolved.add(candidates[0])
        return tuple(sorted(resolved, key=lambda item: (item.casefold(), item)))

    def _resolve_shot_types(self, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            return ()
        approved = (
            self._metadata.approved_shot_types
            if self._metadata is not None
            else APPROVED_SHOT_TYPES
        )
        unknown = tuple(value for value in values if value not in approved)
        if unknown:
            raise QueryValidationError(f"不支援的 shot_types：{', '.join(unknown)}")
        return tuple(sorted(values))

    def _matching_rows(self, filters: EventFilter) -> tuple[Mapping[str, Any], ...]:
        return tuple(row for row in self._rows if self._matches(row, filters))

    def _matches(self, row: Mapping[str, Any], filters: EventFilter) -> bool:
        if (
            filters.match_ids
            and _value_as_text(row["match_id"]) not in filters.match_ids
        ):
            return False
        if (
            filters.set_numbers
            and _validated_integer(row["set"], "set") not in filters.set_numbers
        ):
            return False
        if (
            filters.rally_ids
            and _value_as_text(row["rally_id"]) not in filters.rally_ids
        ):
            return False
        if (
            filters.players
            and self._canonical_player(row["player"]) not in filters.players
        ):
            return False
        if (
            filters.opponents
            and self._canonical_player(row["opponent"]) not in filters.opponents
        ):
            return False
        if filters.shot_types and _value_as_text(row["type"]) not in filters.shot_types:
            return False
        ball_round = _validated_integer(row["ball_round"], "ball_round")
        if filters.ball_round_min is not None and ball_round < filters.ball_round_min:
            return False
        if filters.ball_round_max is not None and ball_round > filters.ball_round_max:
            return False
        return True

    def _copy_row(self, row: Mapping[str, Any]) -> Mapping[str, Any]:
        copied = {column: row[column] for column in self._columns}
        for field in ("player", "opponent", "getpoint_player"):
            if field in copied and _value_as_text(copied[field]):
                copied[field] = self._canonical_player(copied[field])
        return MappingProxyType(copied)

    def _summarize_matches(
        self,
        rows: tuple[Mapping[str, Any], ...],
    ) -> tuple[MatchSummary, ...]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            match_id = _value_as_text(row["match_id"])
            group = grouped.setdefault(
                match_id,
                {"players": set(), "sets": set(), "rallies": set(), "events": 0},
            )
            group["players"].add(self._canonical_player(row["player"]))
            group["players"].add(self._canonical_player(row["opponent"]))
            set_number = _validated_integer(row["set"], "set")
            rally = _validated_integer(row["rally"], "rally")
            group["sets"].add(set_number)
            group["rallies"].add((set_number, rally))
            group["events"] += 1

        summaries: list[MatchSummary] = []
        for match_id in sorted(grouped, key=lambda item: (item.casefold(), item)):
            group = grouped[match_id]
            summaries.append(
                MatchSummary(
                    match_id=match_id,
                    players=tuple(
                        sorted(
                            group["players"],
                            key=lambda item: (item.casefold(), item),
                        )
                    ),
                    sets=tuple(sorted(group["sets"])),
                    rally_count=len(group["rallies"]),
                    event_count=group["events"],
                )
            )
        return tuple(summaries)

    @staticmethod
    def _validate_pagination(limit: int, offset: int) -> tuple[int, int]:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise QueryValidationError("limit 必須是整數")
        if limit < 1 or limit > MAX_PAGE_LIMIT:
            raise QueryValidationError(f"limit 必須介於 1 與 {MAX_PAGE_LIMIT} 之間")
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise QueryValidationError("offset 必須是非負整數")
        if offset < 0:
            raise QueryValidationError("offset 必須是非負整數")
        return limit, offset


__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "BadmintonQueryService",
    "EventFilter",
    "MatchSummary",
    "QueryError",
    "QueryPage",
    "QueryValidationError",
    "UnknownMatchError",
]
