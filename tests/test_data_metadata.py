"""metadata loader、alias 與 semantic registry 測試。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from badminton_ai.data import (
    AmbiguousPlayerAliasError,
    DatasetSnapshot,
    MetadataLoadError,
    MetadataValidationError,
    UnknownPlayerAliasError,
    load_metadata,
    validate_registry_covers_columns,
)


def _registry_field(column: str) -> dict[str, str]:
    return {
        "column": column,
        "group": "game_structure",
        "role": "event_actor",
        "grain": "event",
        "perspective": "neutral",
        "null_policy": "required",
        "reference_frame": "sequence",
        "unit": "identifier",
    }


def _write_metadata(
    directory: Path,
    *,
    actors: dict[str, list[str]] | None = None,
    data_columns: list[dict[str, object]] | None = None,
    registry_fields: list[dict[str, str]] | None = None,
    court_place: str = "Zone 1 is back court.",
) -> None:
    directory.mkdir()
    if actors is None:
        actors = {"Alice": ["alice"], "Bob": ["bob"]}
    if data_columns is None:
        data_columns = [
            {"column": "match_id", "description": "比賽識別"},
        ]
    if registry_fields is None:
        registry_fields = [_registry_field("match_id")]

    (directory / "actor_aliases.json").write_text(
        json.dumps({"dataset": "fixture", "actors": actors}, ensure_ascii=False),
        encoding="utf-8",
    )
    (directory / "column_definition.json").write_text(
        json.dumps(
            {
                "metadata": {"description": "fixture"},
                "shot_types": {"1": "發短球", "2": "殺球", "11": "接不到"},
                "data_columns": data_columns,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (directory / "event_semantic_registry.json").write_text(
        json.dumps(
            {"name": "fixture_registry", "version": 1, "fields": registry_fields},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (directory / "court_place.txt").write_text(court_place, encoding="utf-8")


def test_load_metadata_validates_all_files_and_normalizes_alias(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "metadata"
    _write_metadata(directory)

    metadata = load_metadata(directory)

    assert metadata.normalize_player(" ALICE ") == "Alice"
    assert metadata.normalize_player("BOB") == "Bob"
    assert metadata.shot_types == {"發短球", "殺球", "接不到"}
    assert metadata.approved_shot_types == {"發短球", "殺球"}
    assert metadata.court_place.startswith("Zone")


def test_approved_scoring_metadata_documents_empty_string_placeholders() -> None:
    metadata_dir = (
        Path(__file__).resolve().parents[1] / ".runtime" / "approved-data" / "metadata"
    )
    metadata = load_metadata(metadata_dir)
    definitions = {item["column"]: item for item in metadata.column_definitions}
    column_file = json.loads(
        (metadata_dir / "column_definition.json").read_text(encoding="utf-8")
    )

    winner = definitions["getpoint_player"]
    assert '空字串 ""' in winner["description"]
    assert "不是 JSON null" in winner["description"]
    assert "尚未結束" in winner["description"]
    assert "不要依賴 isna() 或 dropna()" in winner["usage"]
    assert '空字串 ""' in definitions["win_reason"]["description"]
    assert '空字串 ""' in definitions["lose_reason"]["description"]
    success_rate = column_file["analysis_guidelines"]["shot_success_rate"]
    assert "僅計終局事件" in success_rate
    assert 'getpoint_player == "" 不代表成功' in success_rate
    assert "getpoint_player != opponent" in success_rate
    shot_analysis = column_file["analysis_guidelines"]["shot_analysis"]
    assert "player == getpoint_player == P 且 type == T" in shot_analysis["logic"]
    total_share = shot_analysis["share_of_total_points"]
    assert "分母為所有終局 getpoint_player == P 的回合" in total_share
    assert "包含對手失誤送分" in total_share
    assert "包含對手失誤送分" in total_share
    assert "本 snapshot" not in total_share
    active_share = shot_analysis["share_within_active_points"]
    assert "分母才限於終局 getpoint_player == P 且 player == P" in active_share
    assert "分母才限於終局 getpoint_player == P 且 player == P" in active_share
    assert "本 snapshot" not in active_share


def test_unknown_and_ambiguous_aliases_are_explicit_errors(tmp_path: Path) -> None:
    directory = tmp_path / "unknown"
    _write_metadata(directory)
    metadata = load_metadata(directory)
    with pytest.raises(UnknownPlayerAliasError, match="未知球員 alias"):
        metadata.normalize_player("Carol")

    conflict_directory = tmp_path / "conflict"
    _write_metadata(
        conflict_directory,
        actors={"Alice": ["shared"], "Bob": ["SHARED"]},
    )
    with pytest.raises(AmbiguousPlayerAliasError, match="同時指向"):
        load_metadata(conflict_directory)


def test_metadata_rejects_duplicate_columns_and_missing_semantic_attribute(
    tmp_path: Path,
) -> None:
    duplicate_directory = tmp_path / "duplicate"
    _write_metadata(
        duplicate_directory,
        data_columns=[
            {"column": "match_id", "description": "一"},
            {"column": "match_id", "description": "二"},
        ],
    )
    with pytest.raises(MetadataValidationError, match="欄位重複"):
        load_metadata(duplicate_directory)

    missing_attribute_directory = tmp_path / "missing-attribute"
    field = _registry_field("match_id")
    del field["unit"]
    _write_metadata(missing_attribute_directory, registry_fields=[field])
    with pytest.raises(MetadataValidationError, match="必要屬性"):
        load_metadata(missing_attribute_directory)


@pytest.mark.parametrize(
    ("enum_name", "invalid_value"),
    [
        ("role", "invalid_role"),
        ("grain", "invalid_grain"),
        ("perspective", "invalid_perspective"),
        ("null_policy", "invalid_null_policy"),
        ("reference_frame", "invalid_reference_frame"),
        ("unit", "invalid_unit"),
    ],
)
def test_metadata_rejects_invalid_registry_enum_values(
    tmp_path: Path,
    enum_name: str,
    invalid_value: str,
) -> None:
    directory = tmp_path / f"invalid-{enum_name}"
    field = _registry_field("match_id")
    field[enum_name] = invalid_value
    _write_metadata(directory, registry_fields=[field])

    with pytest.raises(
        MetadataValidationError,
        match=rf"fields\[1\].*{enum_name}.*enum",
    ):
        load_metadata(directory)


def test_metadata_rejects_empty_court_place_and_invalid_json(tmp_path: Path) -> None:
    empty_directory = tmp_path / "empty-court"
    _write_metadata(empty_directory, court_place="\n  ")
    with pytest.raises(MetadataValidationError, match="不可為空"):
        load_metadata(empty_directory)

    invalid_directory = tmp_path / "invalid-json"
    _write_metadata(invalid_directory)
    (invalid_directory / "actor_aliases.json").write_text("{", encoding="utf-8")
    with pytest.raises(MetadataLoadError, match="JSON"):
        load_metadata(invalid_directory)


def test_registry_cross_check_only_requires_registry_coverage(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "cross-check"
    _write_metadata(
        directory,
        data_columns=[
            {"column": "match_id", "description": "比賽識別"},
        ],
        registry_fields=[
            _registry_field("match_id"),
            _registry_field("extra_dataset_column"),
        ],
    )
    metadata = load_metadata(directory)
    snapshot = DatasetSnapshot(
        columns=("match_id", "extra_dataset_column"),
        rows=(),
        source=tmp_path / "fixture.csv",
    )

    assert validate_registry_covers_columns(snapshot, metadata) is snapshot

    missing_snapshot = DatasetSnapshot(
        columns=("match_id", "not_registered"),
        rows=(),
        source=tmp_path / "fixture.csv",
    )
    with pytest.raises(MetadataValidationError, match="not_registered"):
        validate_registry_covers_columns(missing_snapshot, metadata)
