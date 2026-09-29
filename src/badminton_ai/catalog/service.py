"""BadmintonAI v2 的唯讀資料目錄與覆蓋摘要服務。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..data.errors import DataError
from ..query import (
    MAX_PAGE_LIMIT,
    BadmintonQueryService,
    EventFilter,
    MatchSummary,
)

_DECIMAL_INTEGER_PATTERN = re.compile(r"[0-9]+(?:\.0+)?\Z")


class CatalogError(DataError):
    """資料目錄或覆蓋摘要的領域錯誤。"""


@dataclass(frozen=True)
class DatasetSummary:
    """單一 snapshot 的資料來源、範圍與穩定 identity。"""

    source: str
    row_count: int
    column_count: int
    match_count: int
    set_count: int
    rally_count: int
    player_count: int
    snapshot_id: str
    snapshot_version: str | None


@dataclass(frozen=True)
class ColumnSummary:
    """單一欄位的 metadata 與觀測覆蓋摘要。"""

    name: str
    description: str | None
    data_type: str | None
    role: str | None
    unit: str | None
    null_count: int
    blank_count: int
    json_null_count: int
    distinct_count: int


@dataclass(frozen=True)
class PlayerCoverageSummary:
    """canonical 球員在資料中的場次、回合、事件及勝負回合覆蓋量。"""

    player: str
    match_count: int
    rally_count: int
    won_rallies: int
    lost_rallies: int
    event_count: int
    swing_event_count: int


@dataclass(frozen=True)
class _EventScan:
    """query service 分頁掃描的不可變內部結果。"""

    rows: tuple[Mapping[str, Any], ...]
    columns: tuple[str, ...]
    source: str


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        parsed = None
    elif isinstance(value, int):
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
        raise CatalogError(f"資料欄位 {field} 不是有效正整數")
    return parsed


def _rally_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    return (_text(row["match_id"]), _text(row["rally_id"]))


def _player_rally_identity(row: Mapping[str, Any]) -> tuple[str, int, str]:
    return (
        _text(row["match_id"]),
        _positive_integer(row["set"], "set"),
        _text(row["rally_id"]),
    )


def _rally_location(row: Mapping[str, Any]) -> tuple[int, int]:
    return (
        _positive_integer(row["set"], "set"),
        _positive_integer(row["rally"], "rally"),
    )


def _is_null(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _is_blank(value: Any) -> bool:
    return isinstance(value, str) and not value.strip()


def _canonicalize_value(
    value: Any,
    *,
    path: str,
    active_containers: set[int] | None = None,
) -> Any:
    """將 JSON-compatible 值轉成可穩定序列化的內部值。

    mapping 僅接受文字 key；list 與 tuple 皆轉成 JSON 陣列；set、bytes、
    自訂物件與非有限浮點數明確拒絕，避免以 ``repr`` 產生不穩定 identity。
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CatalogError(f"{path} 含非有限浮點數，無法建立穩定摘要")
        return value

    if active_containers is None:
        active_containers = set()
    if isinstance(value, Mapping):
        container_id = id(value)
        if container_id in active_containers:
            raise CatalogError(f"{path} 含循環 mapping，無法建立穩定摘要")
        active_containers.add(container_id)
        try:
            normalized: dict[str, Any] = {}
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise CatalogError(
                        f"{path} 的 mapping key 必須是文字，收到 {type(key).__name__}"
                    )
                normalized[key] = _canonicalize_value(
                    nested,
                    path=f"{path}[{key!r}]",
                    active_containers=active_containers,
                )
            return normalized
        finally:
            active_containers.remove(container_id)

    if isinstance(value, (list, tuple)):
        container_id = id(value)
        if container_id in active_containers:
            raise CatalogError(f"{path} 含循環陣列，無法建立穩定摘要")
        active_containers.add(container_id)
        try:
            return [
                _canonicalize_value(
                    nested,
                    path=f"{path}[{index}]",
                    active_containers=active_containers,
                )
                for index, nested in enumerate(value)
            ]
        finally:
            active_containers.remove(container_id)

    raise CatalogError(
        f"{path} 含不支援的值類型 {type(value).__name__}，"
        "只能使用 JSON-compatible scalar/container"
    )


def _stable_json(value: Any, *, path: str) -> str:
    canonical = _canonicalize_value(value, path=path)
    try:
        return json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CatalogError(f"{path} 無法穩定序列化") from exc


def _distinct_token(value: Any) -> tuple[str, str]:
    return type(value).__name__, _stable_json(value, path="欄位值")


def _snapshot_identity(
    columns: tuple[str, ...], rows: tuple[Mapping[str, Any], ...]
) -> str:
    payload = _stable_json(
        {"columns": columns, "rows": rows},
        path="snapshot",
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class BadmintonCatalogService:
    """透過 query service 提供資料目錄與描述性覆蓋摘要。"""

    def __init__(
        self,
        query_service: BadmintonQueryService,
    ) -> None:
        if not isinstance(query_service, BadmintonQueryService):
            raise CatalogError("catalog service 必須接收 BadmintonQueryService")
        self._query_service = query_service
        self._metadata = query_service.metadata

    def dataset_summary(self) -> DatasetSummary:
        """回傳完整 snapshot 的來源、欄列數與資料覆蓋摘要。"""

        scan = self._scan(None)
        matches: set[str] = set()
        sets: set[tuple[str, int]] = set()
        rallies: set[tuple[str, str]] = set()
        players: set[str] = set()
        locations: dict[tuple[str, str], tuple[int, int]] = {}

        for row in scan.rows:
            match_id = _text(row["match_id"])
            identity = _rally_identity(row)
            location = _rally_location(row)
            previous_location = locations.get(identity)
            if previous_location is not None and previous_location != location:
                raise CatalogError(
                    f"rally {identity} 對應多個 set/rally："
                    f"{previous_location} 與 {location}"
                )
            locations[identity] = location
            matches.add(match_id)
            sets.add((match_id, location[0]))
            rallies.add(identity)
            players.add(_text(row["player"]))
            players.add(_text(row["opponent"]))

        return DatasetSummary(
            source=scan.source,
            row_count=len(scan.rows),
            column_count=len(scan.columns),
            match_count=len(matches),
            set_count=len(sets),
            rally_count=len(rallies),
            player_count=len(players),
            snapshot_id=_snapshot_identity(scan.columns, scan.rows),
            snapshot_version=None,
        )

    def describe_columns(
        self,
        filters: EventFilter | None = None,
    ) -> tuple[ColumnSummary, ...]:
        """依欄位順序描述 metadata 與 filtered event 的 null/distinct 覆蓋。"""

        scan = self._scan(filters)
        definitions, registry = self._metadata_indexes()
        summaries: list[ColumnSummary] = []
        for column in scan.columns:
            values = [row[column] for row in scan.rows]
            non_null_tokens = {
                _distinct_token(value) for value in values if not _is_null(value)
            }
            definition = definitions.get(column, {})
            semantic = registry.get(column, {})
            summaries.append(
                ColumnSummary(
                    name=column,
                    description=_optional_text(definition.get("description")),
                    data_type=_metadata_text(
                        definition,
                        ("data_type", "type", "dtype"),
                    ),
                    role=_optional_text(semantic.get("role")),
                    unit=_optional_text(semantic.get("unit")),
                    null_count=sum(1 for value in values if _is_null(value)),
                    blank_count=sum(1 for value in values if _is_blank(value)),
                    json_null_count=sum(1 for value in values if value is None),
                    distinct_count=len(non_null_tokens),
                )
            )
        return tuple(summaries)

    list_columns = describe_columns

    def describe_players(
        self,
        filters: EventFilter | None = None,
    ) -> tuple[PlayerCoverageSummary, ...]:
        """描述球員事件覆蓋及依完整終局欄計得的勝負回合數。"""

        scan = self._scan(filters)
        matches: dict[str, set[str]] = {}
        rallies: dict[str, set[tuple[str, int, str]]] = {}
        event_counts: dict[str, int] = {}
        swing_counts: dict[str, int] = {}
        selected_rallies: set[tuple[str, int, str]] = set()
        for row in scan.rows:
            match_id = _text(row["match_id"])
            identity = _player_rally_identity(row)
            selected_rallies.add(identity)
            player = _text(row["player"])
            opponent = _text(row["opponent"])
            for name in {player, opponent}:
                matches.setdefault(name, set()).add(match_id)
                rallies.setdefault(name, set()).add(identity)
                event_counts[name] = event_counts.get(name, 0) + 1
            swing_counts[player] = swing_counts.get(player, 0) + 1

        outcome_rows = scan.rows if filters is None else self._scan(None).rows
        rallies_by_identity: dict[tuple[str, int, str], dict[str, Any]] = {}
        for row in outcome_rows:
            identity = _player_rally_identity(row)
            if identity not in selected_rallies:
                continue
            rally = rallies_by_identity.setdefault(
                identity,
                {"participants": set(), "terminal_round": 0, "winners": set()},
            )
            rally["participants"].update((_text(row["player"]), _text(row["opponent"])))
            ball_round = _positive_integer(row["ball_round"], "ball_round")
            winner = _text(row.get("getpoint_player"))
            if ball_round > rally["terminal_round"]:
                rally["terminal_round"] = ball_round
                rally["winners"] = {winner} if winner else set()
            elif ball_round == rally["terminal_round"] and winner:
                rally["winners"].add(winner)

        won_counts: dict[str, int] = {}
        lost_counts: dict[str, int] = {}
        for rally in rallies_by_identity.values():
            participants = rally["participants"]
            winners = rally["winners"]
            if len(winners) != 1:
                continue
            winner = next(iter(winners))
            if winner not in participants:
                continue
            won_counts[winner] = won_counts.get(winner, 0) + 1
            for participant in participants - {winner}:
                lost_counts[participant] = lost_counts.get(participant, 0) + 1

        names = sorted(matches, key=lambda item: (item.casefold(), item))
        return tuple(
            PlayerCoverageSummary(
                player=name,
                match_count=len(matches[name]),
                rally_count=len(rallies[name]),
                won_rallies=won_counts.get(name, 0),
                lost_rallies=lost_counts.get(name, 0),
                event_count=event_counts[name],
                swing_event_count=swing_counts.get(name, 0),
            )
            for name in names
        )

    list_players = describe_players

    def describe_matches(
        self,
        filters: EventFilter | None = None,
    ) -> tuple[MatchSummary, ...]:
        """回傳 query service 的 deterministic 比賽資料覆蓋摘要。"""

        return self._query_service.list_matches(filters)

    list_matches = describe_matches

    def _metadata_indexes(
        self,
    ) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
        if self._metadata is None:
            return {}, {}
        definitions = {
            _text(item.get("column")): item
            for item in self._metadata.column_definitions
            if _text(item.get("column"))
        }
        registry = {
            _text(item.get("column")): item
            for item in self._metadata.event_semantic_registry.get("fields", ())
            if _text(item.get("column"))
        }
        return definitions, registry

    @staticmethod
    def _scan_pages(
        query_service: BadmintonQueryService,
        filters: EventFilter | None,
    ) -> _EventScan:
        rows: list[Mapping[str, Any]] = []
        columns: tuple[str, ...] = ()
        source = ""
        offset = 0
        while True:
            page = query_service.query_events(
                filters,
                limit=MAX_PAGE_LIMIT,
                offset=offset,
            )
            if not columns:
                columns = page.columns
                source = str(page.source)
            rows.extend(page.rows)
            if not page.rows or len(rows) >= page.total_count:
                return _EventScan(
                    rows=tuple(rows),
                    columns=columns,
                    source=source,
                )
            offset += len(page.rows)

    def _scan(self, filters: EventFilter | None) -> _EventScan:
        return self._scan_pages(self._query_service, filters)


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _metadata_text(item: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _optional_text(item.get(key))
        if value is not None:
            return value
    return None


__all__ = [
    "BadmintonCatalogService",
    "CatalogError",
    "ColumnSummary",
    "DatasetSummary",
    "PlayerCoverageSummary",
]
