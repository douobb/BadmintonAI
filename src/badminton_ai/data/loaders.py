"""CSV 與 SQLite 的唯讀 snapshot loader。"""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from .constants import MVP_REQUIRED_COLUMNS, STRICT_IDENTIFIER_PATTERN
from .errors import (
    DataFormatError,
    DataLoadError,
    DataSourceError,
    InvalidIdentifierError,
)
from .models import DatasetSnapshot


def load_csv(
    path: str | Path,
    *,
    required_columns: Iterable[str] = MVP_REQUIRED_COLUMNS,
) -> DatasetSnapshot:
    """以 UTF-8 唯讀載入 CSV，保留 header 與資料列欄位順序。"""

    source = _resolve_source(path, source_kind="CSV")
    try:
        with source.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream, strict=True)
            try:
                header = next(reader)
            except StopIteration as exc:
                raise DataFormatError("CSV 檔案不可為空") from exc

            columns = tuple(header)
            _validate_header(columns)
            _validate_required_columns(columns, required_columns)

            rows: list[dict[str, str]] = []
            for row_number, values in enumerate(reader, start=2):
                if not values:
                    continue
                if len(values) != len(columns):
                    raise DataFormatError(
                        f"CSV 第 {row_number} 列欄位數與 header 不一致",
                        row_number=row_number,
                    )
                rows.append(dict(zip(columns, values)))
    except DataFormatError:
        raise
    except UnicodeDecodeError as exc:
        raise DataFormatError("CSV 必須使用有效的 UTF-8 編碼") from exc
    except csv.Error as exc:
        raise DataFormatError("CSV 欄列格式無法解析") from exc
    except OSError as exc:
        raise DataLoadError("CSV 無法讀取") from exc

    return DatasetSnapshot(columns=columns, rows=tuple(rows), source=source)


def load_sqlite(
    path: str | Path,
    table: str,
    *,
    required_columns: Iterable[str] = MVP_REQUIRED_COLUMNS,
) -> DatasetSnapshot:
    """以 SQLite ``mode=ro`` 載入指定 table，不接受任意 SQL。"""

    source = _resolve_source(path, source_kind="SQLite")
    quoted_table = quote_identifier(table)
    uri = f"{source.as_uri()}?mode=ro"

    try:
        connection = sqlite3.connect(uri, uri=True)
    except (OSError, sqlite3.Error) as exc:
        raise DataSourceError("SQLite 資料庫無法以唯讀模式開啟") from exc

    try:
        try:
            cursor = connection.execute(f"SELECT * FROM {quoted_table}")
            columns = tuple(description[0] for description in cursor.description)
            _validate_header(columns)
            _validate_required_columns(columns, required_columns)
            rows = tuple(dict(zip(columns, values)) for values in cursor.fetchall())
        except DataFormatError:
            raise
        except sqlite3.Error as exc:
            raise DataLoadError("SQLite table 無法讀取") from exc
    finally:
        connection.close()

    return DatasetSnapshot(columns=columns, rows=rows, source=source)


def quote_identifier(identifier: str) -> str:
    """驗證並 quote SQLite table identifier。"""

    if not isinstance(identifier, str) or not STRICT_IDENTIFIER_PATTERN.fullmatch(
        identifier
    ):
        raise InvalidIdentifierError("SQLite table 名稱必須符合 ASCII identifier 規則")
    return f'"{identifier}"'


def _resolve_source(path: str | Path, *, source_kind: str) -> Path:
    """解析既有檔案路徑，不建立不存在的來源檔案。"""

    try:
        source = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise DataSourceError(f"{source_kind} 資料來源路徑無法正規化") from exc

    if not source.exists():
        raise DataSourceError(f"{source_kind} 資料來源不存在")
    if not source.is_file():
        raise DataSourceError(f"{source_kind} 資料來源不是檔案")
    return source


def _validate_header(columns: tuple[str, ...]) -> None:
    """檢查 header 非空、名稱非空且沒有重複。"""

    if not columns:
        raise DataFormatError("資料 header 不可為空")
    if any(not isinstance(column, str) or not column for column in columns):
        raise DataFormatError("資料 header 不可包含空白欄位名稱")
    if len(columns) != len(set(columns)):
        raise DataFormatError("資料 header 不可包含重複欄位")


def _validate_required_columns(
    columns: tuple[str, ...],
    required_columns: Iterable[str],
) -> None:
    """確認來源含有指定的必要欄位，並以固定順序回報缺漏。"""

    required = tuple(required_columns)
    missing = tuple(column for column in required if column not in columns)
    if missing:
        raise DataFormatError(f"資料缺少必要欄位：{', '.join(missing)}")
