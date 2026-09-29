"""資料目錄與描述性覆蓋摘要測試。"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from badminton_ai.catalog import BadmintonCatalogService, CatalogError
from badminton_ai.data import (
    APPROVED_SHOT_TYPES,
    MVP_REQUIRED_COLUMNS,
    DatasetSnapshot,
    MetadataSnapshot,
)
from badminton_ai.query import BadmintonQueryService, EventFilter, MatchSummary


def _row(
    match_id: str,
    set_number: int,
    rally: int,
    rally_id: str,
    ball_round: int,
    player: str,
    opponent: str,
    shot_type: str,
    winner: str,
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
                winner,
                "得分" if winner else "",
                "",
                "1",
                "2",
            ],
        )
    )


def _snapshot(*, aliases: bool = True) -> DatasetSnapshot:
    names = {
        "Alice": "A" if aliases else "Alice",
        "Bob": "B" if aliases else "Bob",
        "Carol": "C" if aliases else "Carol",
        "Dave": "D" if aliases else "Dave",
    }
    rows = [
        _row("M2", 1, 1, "R2", 1, names["Carol"], names["Dave"], "發短球", ""),
        _row("M2", 1, 1, "R2", 2, names["Dave"], names["Carol"], "殺球", names["Dave"]),
        _row("M1", 1, 2, "R1B", 1, names["Alice"], names["Bob"], "發短球", ""),
        _row("M1", 1, 2, "R1B", 2, names["Bob"], names["Alice"], "殺球", names["Bob"]),
        _row(
            "M1",
            2,
            1,
            "R1C",
            1,
            names["Alice"],
            names["Bob"],
            "網前球",
            names["Alice"],
        ),
    ]
    return DatasetSnapshot(
        columns=MVP_REQUIRED_COLUMNS,
        rows=tuple(rows),
        source=Path("catalog-fixture.csv"),
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
    definitions = (
        {
            "column": "match_id",
            "description": "比賽識別碼",
            "data_type": "text",
        },
        {
            "column": "type",
            "description": "球種",
            "data_type": "category",
        },
    )
    registry = {
        "fields": (
            {
                "column": "match_id",
                "role": "partition_key",
                "unit": "identifier",
            },
            {
                "column": "type",
                "role": "event_type",
                "unit": "category",
            },
        )
    }
    return MetadataSnapshot(
        actor_aliases=actors,
        column_definitions=definitions,
        event_semantic_registry=registry,
        court_place="fixture court",
        source_dir=tmp_path,
        alias_index=alias_index,
        shot_types=frozenset(APPROVED_SHOT_TYPES),
    )


def _service(tmp_path: Path, *, metadata: bool = True) -> BadmintonCatalogService:
    snapshot = _snapshot()
    metadata_snapshot = _metadata(tmp_path) if metadata else None
    query = BadmintonQueryService(snapshot, metadata_snapshot)
    return BadmintonCatalogService(query)


def test_dataset_summary_is_stable_and_json_compatible(tmp_path: Path) -> None:
    catalog = _service(tmp_path)

    result = catalog.dataset_summary()

    assert result.source.endswith("catalog-fixture.csv")
    assert result.row_count == 5
    assert result.column_count == len(MVP_REQUIRED_COLUMNS)
    assert result.match_count == 2
    assert result.set_count == 3
    assert result.rally_count == 3
    assert result.player_count == 4
    assert result.snapshot_id.startswith("sha256:")
    assert result.snapshot_version is None
    json.dumps(asdict(result), ensure_ascii=False)

    other_snapshot = DatasetSnapshot(
        columns=_snapshot().columns,
        rows=_snapshot().rows,
        source=Path("other-location.csv"),
    )
    other = BadmintonCatalogService(
        BadmintonQueryService(other_snapshot, _metadata(tmp_path))
    ).dataset_summary()
    assert other.snapshot_id == result.snapshot_id


def test_column_summary_uses_only_metadata_and_counts_null_distinct(
    tmp_path: Path,
) -> None:
    catalog = _service(tmp_path)

    columns = catalog.describe_columns()
    by_name = {item.name: item for item in columns}

    assert tuple(item.name for item in columns) == MVP_REQUIRED_COLUMNS
    assert by_name["match_id"].description == "比賽識別碼"
    assert by_name["match_id"].data_type == "text"
    assert by_name["match_id"].role == "partition_key"
    assert by_name["match_id"].unit == "identifier"
    assert by_name["getpoint_player"].null_count == 2
    assert by_name["getpoint_player"].blank_count == 2
    assert by_name["getpoint_player"].json_null_count == 0
    assert by_name["type"].distinct_count == 3
    json.dumps([asdict(item) for item in columns], ensure_ascii=False)

    filtered = catalog.describe_columns(EventFilter(match_ids="M1"))
    filtered_by_name = {item.name: item for item in filtered}
    assert filtered_by_name["match_id"].distinct_count == 1
    assert filtered_by_name["getpoint_player"].null_count == 1
    assert filtered_by_name["getpoint_player"].blank_count == 1


def test_column_summary_distinguishes_json_null_from_blank_string(
    tmp_path: Path,
) -> None:
    rows = [dict(row) for row in _snapshot().rows[:2]]
    rows[0]["lose_reason"] = None
    rows[1]["lose_reason"] = ""
    snapshot = DatasetSnapshot(
        columns=MVP_REQUIRED_COLUMNS,
        rows=tuple(rows),
        source=Path("null-and-blank-fixture.csv"),
    )
    summary = {
        item.name: item
        for item in BadmintonCatalogService(
            BadmintonQueryService(snapshot, _metadata(tmp_path))
        ).describe_columns()
    }["lose_reason"]

    assert summary.null_count == 2
    assert summary.blank_count == 1
    assert summary.json_null_count == 1
    assert summary.distinct_count == 0


def test_without_metadata_does_not_guess_column_semantics() -> None:
    catalog = BadmintonCatalogService(
        BadmintonQueryService(_snapshot(aliases=False)),
    )

    summary = catalog.describe_columns()[0]

    assert summary.description is None
    assert summary.data_type is None
    assert summary.role is None
    assert summary.unit is None


def test_catalog_metadata_is_sourced_from_query_service(
    tmp_path: Path,
) -> None:
    metadata = _metadata(tmp_path)
    query = BadmintonQueryService(_snapshot(), metadata)
    catalog = BadmintonCatalogService(query)

    assert catalog.describe_columns()[0].description == "比賽識別碼"
    assert query.metadata is metadata
    with pytest.raises(TypeError):
        BadmintonCatalogService(query, metadata)  # type: ignore[call-arg]


def test_player_and_match_summaries_are_canonical_and_descriptive(
    tmp_path: Path,
) -> None:
    catalog = _service(tmp_path)

    players = catalog.describe_players()
    assert [item.player for item in players] == ["Alice", "Bob", "Carol", "Dave"]
    assert [
        (
            item.player,
            item.match_count,
            item.rally_count,
            item.won_rallies,
            item.lost_rallies,
            item.event_count,
            item.swing_event_count,
        )
        for item in players
    ] == [
        ("Alice", 1, 2, 1, 1, 3, 2),
        ("Bob", 1, 2, 1, 1, 3, 1),
        ("Carol", 1, 1, 0, 1, 2, 1),
        ("Dave", 1, 1, 1, 0, 2, 1),
    ]

    filtered_players = catalog.describe_players(
        EventFilter(match_ids="M1", players="A"),
    )
    assert [(item.player, item.event_count) for item in filtered_players] == [
        ("Alice", 2),
        ("Bob", 2),
    ]
    assert [
        (item.player, item.won_rallies, item.lost_rallies) for item in filtered_players
    ] == [
        ("Alice", 1, 1),
        ("Bob", 1, 1),
    ]

    assert catalog.describe_matches() == (
        MatchSummary("M1", ("Alice", "Bob"), (1, 2), 2, 3),
        MatchSummary("M2", ("Carol", "Dave"), (1,), 1, 2),
    )
    json.dumps([asdict(item) for item in players], ensure_ascii=False)


def test_player_outcomes_use_full_rally_and_terminal_winner_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    catalog = _service(tmp_path)
    incomplete = _row("M1", 2, 1, "R1", 2, "Bob", "Alice", "殺球", "")
    incomplete["getpoint_player"] = None
    rows = (
        _row("M1", 1, 1, "R1", 1, "Alice", "Bob", "發短球", ""),
        _row("M1", 1, 1, "R1", 2, "Bob", "Alice", "殺球", "Bob"),
        _row("M1", 2, 1, "R1", 1, "Alice", "Bob", "發短球", ""),
        incomplete,
    )
    monkeypatch.setattr(
        catalog,
        "_scan",
        lambda _filters=None: SimpleNamespace(rows=rows),
    )

    players = {item.player: item for item in catalog.describe_players()}

    assert (
        players["Alice"].rally_count,
        players["Alice"].won_rallies,
        players["Alice"].lost_rallies,
    ) == (
        2,
        0,
        1,
    )
    assert (
        players["Bob"].rally_count,
        players["Bob"].won_rallies,
        players["Bob"].lost_rallies,
    ) == (
        2,
        1,
        0,
    )


def test_catalog_scans_all_pages_and_empty_snapshot_is_explicit() -> None:
    rows = tuple(
        _row("M1", 1, index, f"R{index}", 1, "Alice", "Bob", "發短球", "Alice")
        for index in range(1, 502)
    )
    large_snapshot = DatasetSnapshot(
        columns=MVP_REQUIRED_COLUMNS,
        rows=rows,
        source=Path("large.csv"),
    )
    large_catalog = BadmintonCatalogService(BadmintonQueryService(large_snapshot))

    summary = large_catalog.dataset_summary()

    assert (summary.row_count, summary.rally_count) == (501, 501)
    assert large_catalog.describe_players()[0].event_count == 501

    empty_snapshot = DatasetSnapshot(
        columns=MVP_REQUIRED_COLUMNS,
        rows=(),
        source=Path("empty.csv"),
    )
    empty_catalog = BadmintonCatalogService(BadmintonQueryService(empty_snapshot))
    empty = empty_catalog.dataset_summary()

    assert (empty.row_count, empty.match_count, empty.rally_count) == (0, 0, 0)
    assert empty_catalog.describe_players() == ()
    assert all(item.null_count == 0 for item in empty_catalog.describe_columns())


def test_catalog_outputs_are_immutable() -> None:
    result = BadmintonCatalogService(
        BadmintonQueryService(_snapshot(aliases=False)),
    ).dataset_summary()

    with pytest.raises(FrozenInstanceError):
        result.row_count = 99  # type: ignore[misc]


@pytest.mark.parametrize(
    "unsupported_value",
    [object(), float("nan"), float("inf"), float("-inf"), {1: "非文字 key"}],
)
def test_catalog_rejects_unstable_extra_values(unsupported_value: Any) -> None:
    columns = MVP_REQUIRED_COLUMNS + ("extra",)
    row = dict(_snapshot(aliases=False).rows[4])
    row["extra"] = unsupported_value
    snapshot = DatasetSnapshot(
        columns=columns,
        rows=(row,),
        source=Path("unstable-value.csv"),
    )

    catalog = BadmintonCatalogService(BadmintonQueryService(snapshot))

    with pytest.raises(CatalogError, match="穩定|不支援|mapping key"):
        catalog.dataset_summary()
    with pytest.raises(CatalogError, match="穩定|不支援|mapping key"):
        catalog.describe_columns()


def test_catalog_identity_canonicalizes_mapping_order() -> None:
    columns = MVP_REQUIRED_COLUMNS + ("extra",)
    first_row = dict(_snapshot(aliases=False).rows[4])
    first_row["extra"] = {"b": 2, "a": 1}
    second_row = dict(first_row)
    second_row["extra"] = {"a": 1, "b": 2}
    first = DatasetSnapshot(
        columns=columns,
        rows=(first_row,),
        source=Path("mapping-first.csv"),
    )
    second = DatasetSnapshot(
        columns=columns,
        rows=(second_row,),
        source=Path("mapping-second.csv"),
    )

    first_summary = BadmintonCatalogService(
        BadmintonQueryService(first),
    ).dataset_summary()
    second_summary = BadmintonCatalogService(
        BadmintonQueryService(second),
    ).dataset_summary()

    assert first_summary.snapshot_id == second_summary.snapshot_id
