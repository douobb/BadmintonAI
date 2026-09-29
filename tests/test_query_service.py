"""結構化唯讀 query service 的行為與不可變性測試。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from badminton_ai.data import (
    APPROVED_SHOT_TYPES,
    MVP_REQUIRED_COLUMNS,
    AmbiguousPlayerAliasError,
    DatasetSnapshot,
    MetadataSnapshot,
    SchemaValidationError,
    UnknownPlayerAliasError,
)
from badminton_ai.query import (
    MAX_PAGE_LIMIT,
    BadmintonQueryService,
    EventFilter,
    MatchSummary,
    QueryValidationError,
    UnknownMatchError,
)


def _row(
    match_id: str,
    set_number: int,
    rally: int,
    rally_id: str,
    ball_round: int,
    player: str,
    opponent: str,
    shot_type: str,
    getpoint_player: str = "",
) -> dict[str, Any]:
    return dict(
        zip(
            MVP_REQUIRED_COLUMNS,
            [
                match_id,
                str(set_number),
                str(rally),
                rally_id,
                str(ball_round),
                player,
                opponent,
                shot_type,
                getpoint_player,
                "得分" if getpoint_player else "",
                "",
                "1",
                "2",
            ],
        )
    )


def _snapshot() -> DatasetSnapshot:
    rows = [
        _row("M2", 2, 1, "R2A", 1, "Carol", "Dave", "發短球"),
        _row("M2", 2, 1, "R2A", 2, "Dave", "Carol", "殺球", "Dave"),
        _row("M1", 1, 2, "R1B", 1, "Alice", "Bob", "發短球"),
        _row("M1", 1, 2, "R1B", 2, "Bob", "Alice", "殺球", "Alice"),
        _row("M1", 1, 1, "R1A", 1, "Alice", "Bob", "發短球"),
        _row("M1", 1, 1, "R1A", 2, "Bob", "Alice", "殺球", "Bob"),
    ]
    return DatasetSnapshot(
        columns=MVP_REQUIRED_COLUMNS,
        rows=tuple(rows),
        source=Path("fixture.csv"),
    )


def _metadata(tmp_path: Path) -> MetadataSnapshot:
    actors = {
        "Alice": ("Alice", "A"),
        "Bob": ("Bob", "B"),
        "Carol": ("Carol", "C"),
        "Dave": ("Dave", "D"),
    }
    alias_index = {
        alias.casefold(): canonical
        for canonical, aliases in actors.items()
        for alias in aliases
    }
    return MetadataSnapshot(
        actor_aliases=actors,
        column_definitions=(),
        event_semantic_registry={"fields": ()},
        court_place="fixture court",
        source_dir=tmp_path,
        alias_index=alias_index,
        shot_types=frozenset(APPROVED_SHOT_TYPES),
    )


def test_service_requires_canonical_schema_validation() -> None:
    rows = [dict(row) for row in _snapshot().rows]
    rows[0]["set"] = "4"
    invalid = DatasetSnapshot(
        columns=MVP_REQUIRED_COLUMNS,
        rows=tuple(rows),
        source=Path("invalid.csv"),
    )

    with pytest.raises(SchemaValidationError, match="set"):
        BadmintonQueryService(invalid)


def test_query_events_filters_with_and_or_and_preserves_source_order() -> None:
    service = BadmintonQueryService(_snapshot())
    filters = EventFilter(
        match_ids=("M1", "M2"),
        set_numbers=(1,),
        players=("Alice", "Bob"),
        shot_types="殺球",
        ball_round_min=2,
        ball_round_max=2,
    )

    result = service.query_events(filters, limit=10)

    assert result.total_count == 2
    assert result.offset == 0
    assert result.limit == 10
    assert [row["rally_id"] for row in result.rows] == ["R1B", "R1A"]
    assert tuple(result.rows[0]) == MVP_REQUIRED_COLUMNS
    assert result.effective_filters.players == ("Alice", "Bob")


def test_query_events_accepts_rally_id_and_opponent_filters() -> None:
    service = BadmintonQueryService(_snapshot())

    result = service.query_events(
        EventFilter(rally_ids="R1A", opponents="Alice"),
    )

    assert result.total_count == 1
    assert {row["rally_id"] for row in result.rows} == {"R1A"}


def test_alias_normalization_with_metadata_and_without_metadata() -> None:
    snapshot = _snapshot()
    service = BadmintonQueryService(snapshot)
    metadata_service = BadmintonQueryService(snapshot, _metadata(Path("meta")))

    exact_casefold = service.query_events(EventFilter(players="alice"))
    normalized_alias = metadata_service.query_events(EventFilter(players="a"))

    assert exact_casefold.total_count == 2
    assert normalized_alias.total_count == 2
    assert normalized_alias.effective_filters.players == ("Alice",)
    with pytest.raises(UnknownPlayerAliasError, match="未知球員 alias"):
        metadata_service.query_events(EventFilter(players="unknown"))
    with pytest.raises(UnknownPlayerAliasError, match="未知球員 alias"):
        service.query_events(EventFilter(players="unknown"))


def test_known_aliases_are_canonicalized_in_query_rows_and_summaries(
    tmp_path: Path,
) -> None:
    rows = [dict(row) for row in _snapshot().rows]
    for row in rows:
        if row["match_id"] == "M2":
            row["player"] = {"Carol": "C", "Dave": "D"}[row["player"]]
            row["opponent"] = {"Carol": "C", "Dave": "D"}[row["opponent"]]
            if row["getpoint_player"]:
                row["getpoint_player"] = "D"
    aliased_snapshot = DatasetSnapshot(
        columns=MVP_REQUIRED_COLUMNS,
        rows=tuple(rows),
        source=Path("aliases.csv"),
    )
    service = BadmintonQueryService(aliased_snapshot, _metadata(tmp_path))

    page = service.query_events(EventFilter(match_ids="M2"))

    assert page.rows[0]["player"] == "Carol"
    assert page.rows[0]["opponent"] == "Dave"
    assert page.rows[1]["getpoint_player"] == "Dave"
    assert service.get_match("M2").players == ("Carol", "Dave")
    assert service.list_players(EventFilter(match_ids="M2")) == ("Carol", "Dave")


def test_unknown_snapshot_player_alias_fails_during_service_construction(
    tmp_path: Path,
) -> None:
    rows = [dict(row) for row in _snapshot().rows]
    rows[0]["player"] = "Unknown"
    aliased_snapshot = DatasetSnapshot(
        columns=MVP_REQUIRED_COLUMNS,
        rows=tuple(rows),
        source=Path("unknown-alias.csv"),
    )

    with pytest.raises(UnknownPlayerAliasError, match="未知球員 alias"):
        BadmintonQueryService(aliased_snapshot, _metadata(tmp_path))


def test_casefold_collision_without_metadata_is_explicit() -> None:
    rows = list(_snapshot().rows)
    rows.extend(
        [
            _row("M3", 1, 1, "R3", 1, "ALICE", "Bob", "發短球"),
            _row("M3", 1, 1, "R3", 2, "Bob", "ALICE", "殺球", "ALICE"),
        ]
    )
    service = BadmintonQueryService(
        DatasetSnapshot(
            columns=MVP_REQUIRED_COLUMNS,
            rows=tuple(rows),
            source=Path("casefold.csv"),
        )
    )

    with pytest.raises(AmbiguousPlayerAliasError, match="多個 canonical"):
        service.query_events(EventFilter(players="alice"))


@pytest.mark.parametrize(
    "factory",
    [
        lambda: EventFilter(set_numbers=(0,)),
        lambda: EventFilter(set_numbers=(4,)),
        lambda: EventFilter(ball_round_min=0),
        lambda: EventFilter(ball_round_min=3, ball_round_max=2),
    ],
)
def test_filter_values_are_validated(factory: Any) -> None:
    with pytest.raises(QueryValidationError):
        factory()


def test_query_events_validates_shot_type_and_pagination() -> None:
    service = BadmintonQueryService(_snapshot())

    with pytest.raises(QueryValidationError, match="shot_types"):
        service.query_events(EventFilter(shot_types="未知球種"))
    with pytest.raises(QueryValidationError, match="limit"):
        service.query_events(limit=0)
    with pytest.raises(QueryValidationError, match="limit"):
        service.query_events(limit=MAX_PAGE_LIMIT + 1)
    with pytest.raises(QueryValidationError, match="offset"):
        service.query_events(offset=-1)
    with pytest.raises(QueryValidationError, match="整數"):
        service.query_events(limit=True)


def test_pagination_empty_result_and_effective_source() -> None:
    service = BadmintonQueryService(_snapshot())

    page = service.query_events(limit=2, offset=1)
    empty = service.query_events(EventFilter(match_ids="missing"))

    assert page.total_count == 6
    assert len(page.rows) == 2
    assert page.source == Path("fixture.csv").resolve()
    assert empty.total_count == 0
    assert empty.rows == ()


def test_match_summaries_players_and_unknown_match_are_deterministic() -> None:
    service = BadmintonQueryService(_snapshot())

    assert service.list_matches() == (
        MatchSummary("M1", ("Alice", "Bob"), (1,), 2, 4),
        MatchSummary("M2", ("Carol", "Dave"), (2,), 1, 2),
    )
    assert service.get_match("M1") == MatchSummary(
        "M1",
        ("Alice", "Bob"),
        (1,),
        2,
        4,
    )
    assert service.list_matches(EventFilter(match_ids="missing")) == ()
    assert service.list_players() == ("Alice", "Bob", "Carol", "Dave")
    assert service.list_players(EventFilter(match_ids="M1")) == ("Alice", "Bob")
    with pytest.raises(UnknownMatchError, match="未知 match_id"):
        service.get_match("missing")


def test_query_results_are_immutable_and_snapshot_is_unchanged() -> None:
    snapshot = _snapshot()
    original_rows = snapshot.rows
    service = BadmintonQueryService(snapshot)
    page = service.query_events(limit=1)

    with pytest.raises(TypeError):
        page.rows[0]["match_id"] = "changed"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        page.limit = 2  # type: ignore[misc]

    assert service.snapshot is snapshot
    assert service.snapshot.rows == original_rows
