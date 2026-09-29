"""CSV 與 SQLite loader 的唯讀與欄位結構測試。"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from badminton_ai.data import (
    MVP_REQUIRED_COLUMNS,
    DataFormatError,
    DataLoadError,
    DataSourceError,
    InvalidIdentifierError,
    load_csv,
    load_sqlite,
    validate_snapshot,
)


def _write_csv(path: Path, columns: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows(rows)


def _valid_csv_rows() -> list[list[str]]:
    return [
        ["M1", "1", "1", "R1", "1", "Alice", "Bob", "發短球", "", "", "", "1", "2"],
        [
            "M1",
            "1",
            "1",
            "R1",
            "2",
            "Bob",
            "Alice",
            "殺球",
            "Alice",
            "得分",
            "",
            "2",
            "3",
        ],
    ]


def test_load_csv_preserves_header_order_and_values(tmp_path: Path) -> None:
    path = tmp_path / "events.csv"
    columns = list(MVP_REQUIRED_COLUMNS)
    _write_csv(path, columns, _valid_csv_rows())

    snapshot = load_csv(path)

    assert snapshot.columns == tuple(columns)
    assert snapshot.row_count == 2
    assert list(snapshot.rows[0]) == columns
    assert snapshot.rows[1]["getpoint_player"] == "Alice"
    assert snapshot.source == path.resolve()


def test_load_csv_accepts_decimal_integer_text_for_schema_validation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "decimal-integers.csv"
    rows = _valid_csv_rows()
    integer_columns = {
        "set",
        "rally",
        "ball_round",
        "landing_area",
        "player_location_area",
    }
    integer_indexes = [
        index
        for index, column in enumerate(MVP_REQUIRED_COLUMNS)
        if column in integer_columns
    ]
    for row in rows:
        for index in integer_indexes:
            row[index] = f"{row[index]}.0"
    _write_csv(path, list(MVP_REQUIRED_COLUMNS), rows)

    snapshot = load_csv(path)

    assert validate_snapshot(snapshot) is snapshot


def test_load_csv_rejects_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")

    with pytest.raises(DataFormatError, match="不可為空"):
        load_csv(path)


def test_load_csv_rejects_duplicate_header(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-header.csv"
    columns = list(MVP_REQUIRED_COLUMNS)
    columns[-1] = "match_id"
    _write_csv(path, columns, [])

    with pytest.raises(DataFormatError, match="重複欄位"):
        load_csv(path)


def test_load_csv_rejects_missing_required_column(tmp_path: Path) -> None:
    path = tmp_path / "missing-column.csv"
    _write_csv(path, ["match_id"], [["M1"]])

    with pytest.raises(DataFormatError, match="缺少必要欄位"):
        load_csv(path)


def test_load_csv_rejects_invalid_encoding_and_row_width(tmp_path: Path) -> None:
    invalid_encoding = tmp_path / "invalid-encoding.csv"
    invalid_encoding.write_bytes(b"match_id\n\xff")
    with pytest.raises(DataFormatError, match="UTF-8"):
        load_csv(invalid_encoding, required_columns=("match_id",))

    malformed = tmp_path / "wrong-width.csv"
    _write_csv(malformed, ["match_id", "set"], [["M1"]])
    with pytest.raises(DataFormatError, match="欄位數"):
        load_csv(malformed, required_columns=("match_id", "set"))


def test_load_csv_missing_source_does_not_create_file(tmp_path: Path) -> None:
    path = tmp_path / "not-created.csv"

    with pytest.raises(DataSourceError, match="不存在"):
        load_csv(path)

    assert not path.exists()


def _create_sqlite_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    columns_sql = ", ".join(f'"{column}" TEXT' for column in MVP_REQUIRED_COLUMNS)
    connection.execute(f'CREATE TABLE "events" ({columns_sql})')
    placeholders = ", ".join("?" for _ in MVP_REQUIRED_COLUMNS)
    connection.executemany(
        f'INSERT INTO "events" VALUES ({placeholders})',
        _valid_csv_rows(),
    )
    connection.commit()
    connection.close()


def test_load_sqlite_uses_read_only_snapshot_and_preserves_columns(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.db"
    _create_sqlite_database(path)

    snapshot = load_sqlite(path, "events")

    assert snapshot.columns == MVP_REQUIRED_COLUMNS
    assert snapshot.row_count == 2
    assert snapshot.rows[1]["getpoint_player"] == "Alice"


def test_load_sqlite_accepts_real_integral_values_for_schema_validation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "real-integers.db"
    integer_columns = {
        "set",
        "rally",
        "ball_round",
        "landing_area",
        "player_location_area",
    }
    column_definitions = ", ".join(
        f'"{column}" {"REAL" if column in integer_columns else "TEXT"}'
        for column in MVP_REQUIRED_COLUMNS
    )
    rows = _valid_csv_rows()
    integer_indexes = [
        index
        for index, column in enumerate(MVP_REQUIRED_COLUMNS)
        if column in integer_columns
    ]
    numeric_rows = []
    for row in rows:
        numeric_row = list(row)
        for index in integer_indexes:
            numeric_row[index] = float(numeric_row[index])
        numeric_rows.append(numeric_row)

    connection = sqlite3.connect(path)
    connection.execute(f'CREATE TABLE "events" ({column_definitions})')
    placeholders = ", ".join("?" for _ in MVP_REQUIRED_COLUMNS)
    connection.executemany(
        f'INSERT INTO "events" VALUES ({placeholders})',
        numeric_rows,
    )
    connection.commit()
    connection.close()

    snapshot = load_sqlite(path, "events")

    assert isinstance(snapshot.rows[0]["landing_area"], float)
    assert validate_snapshot(snapshot) is snapshot


def test_load_sqlite_rejects_unsafe_identifier_before_query(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    _create_sqlite_database(path)

    with pytest.raises(InvalidIdentifierError, match="identifier"):
        load_sqlite(path, "events; DROP TABLE events")


def test_load_sqlite_missing_source_does_not_create_database(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"

    with pytest.raises(DataSourceError, match="不存在"):
        load_sqlite(path, "events")

    assert not path.exists()


def test_load_sqlite_missing_table_is_a_domain_error(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    connection = sqlite3.connect(path)
    connection.close()

    with pytest.raises(DataLoadError, match="table"):
        load_sqlite(path, "events")
