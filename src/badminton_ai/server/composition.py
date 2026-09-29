"""組裝唯讀核心服務的最小 composition root。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..catalog import BadmintonCatalogService
from ..data import (
    DataError,
    DataSourceError,
    load_csv,
    load_metadata,
    load_sqlite,
    validate_registry_covers_columns,
)
from ..data.models import DatasetSnapshot, MetadataSnapshot
from ..query import BadmintonQueryService
from ..sandbox import DockerSandboxRunner
from ..settings import AppSettings, load_settings

ENV_DATA_FILE = "BADMINTON_AI_DATA_FILE"
ENV_METADATA_DIR = "BADMINTON_AI_METADATA_DIR"
ENV_SQLITE_TABLE = "BADMINTON_AI_SQLITE_TABLE"
DEFAULT_SQLITE_TABLE = "match_data"
SUPPORTED_SQLITE_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3"})


class CompositionError(DataError):
    """服務組裝設定或核准資料來源不完整。"""


@dataclass(frozen=True, slots=True)
class ToolServices:
    """API 使用的核心服務集合；所有服務都限制在唯讀資料邊界。"""

    query: BadmintonQueryService
    catalog: BadmintonCatalogService
    sandbox: DockerSandboxRunner


def build_services(
    settings: AppSettings | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    data_file: str | Path | None = None,
    metadata_dir: str | Path | None = None,
    sqlite_table: str | None = None,
    sandbox: DockerSandboxRunner | None = None,
) -> ToolServices:
    """從設定與核准來源建立 query、catalog、sandbox 服務。

    ``data_file`` 與 ``metadata_dir`` 若為相對路徑，皆只能相對於
    ``source_data_dir``；這可避免 API 啟動器意外讀取來源目錄外的檔案。
    測試可直接傳入參數，正式啟動則使用對應環境變數。
    """

    values = os.environ if environ is None else environ
    app_settings = settings or load_settings(values)
    raw_data_file = data_file
    if raw_data_file is None:
        raw_data_file = values.get(ENV_DATA_FILE)
    if raw_data_file is None:
        raise CompositionError(
            f"必須設定 {ENV_DATA_FILE} 指向 source data 目錄內的 CSV 或 SQLite 檔案"
        )
    resolved_data_file = _resolve_inside_source(
        raw_data_file,
        source_dir=app_settings.source_data_dir,
        setting_name=ENV_DATA_FILE,
    )

    resolved_metadata_dir = _resolve_metadata_dir(
        metadata_dir,
        values=values,
        source_dir=app_settings.source_data_dir,
    )
    snapshot = _load_snapshot(
        resolved_data_file,
        sqlite_table=sqlite_table or values.get(ENV_SQLITE_TABLE, DEFAULT_SQLITE_TABLE),
    )
    metadata = _load_optional_metadata(resolved_metadata_dir)
    if metadata is not None:
        validate_registry_covers_columns(snapshot, metadata)

    query = BadmintonQueryService(snapshot, metadata)
    return ToolServices(
        query=query,
        catalog=BadmintonCatalogService(query),
        sandbox=sandbox or DockerSandboxRunner(),
    )


def _resolve_inside_source(
    value: str | Path,
    *,
    source_dir: Path,
    setting_name: str,
) -> Path:
    try:
        source_root = Path(source_dir).expanduser().resolve(strict=False)
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = source_root / candidate
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(source_root)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise CompositionError(
            f"{setting_name} 必須位於唯讀 source data 目錄內"
        ) from exc
    return resolved


def _resolve_metadata_dir(
    explicit: str | Path | None,
    *,
    values: Mapping[str, str],
    source_dir: Path,
) -> Path | None:
    raw_value = explicit
    if raw_value is None:
        raw_value = values.get(ENV_METADATA_DIR)
    if raw_value is None:
        default = Path(source_dir).expanduser().resolve(strict=False) / "metadata"
        return default.resolve(strict=False) if default.is_dir() else None
    return _resolve_inside_source(
        raw_value,
        source_dir=source_dir,
        setting_name=ENV_METADATA_DIR,
    )


def _load_snapshot(path: Path, *, sqlite_table: str) -> DatasetSnapshot:
    suffix = path.suffix.casefold()
    try:
        if suffix == ".csv":
            return load_csv(path)
        if suffix in SUPPORTED_SQLITE_SUFFIXES:
            return load_sqlite(path, sqlite_table)
    except DataError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise CompositionError("核准資料來源無法載入") from exc
    raise DataSourceError("資料來源副檔名必須是 CSV 或 SQLite")


def _load_optional_metadata(path: Path | None) -> MetadataSnapshot | None:
    if path is None:
        return None
    return load_metadata(path)


__all__ = [
    "DEFAULT_SQLITE_TABLE",
    "ENV_DATA_FILE",
    "ENV_METADATA_DIR",
    "ENV_SQLITE_TABLE",
    "CompositionError",
    "ToolServices",
    "build_services",
]
