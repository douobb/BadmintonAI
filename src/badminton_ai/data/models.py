"""資料層使用的不可變 snapshot 與 metadata 型別。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .constants import APPROVED_SHOT_TYPES
from .errors import DataFormatError, UnknownPlayerAliasError


@dataclass(frozen=True)
class DatasetSnapshot:
    """單一資料來源載入後的欄位順序、列資料與來源識別。"""

    columns: tuple[str, ...]
    rows: tuple[Mapping[str, Any], ...]
    source: Path

    def __post_init__(self) -> None:
        columns = tuple(self.columns)
        if len(columns) != len(set(columns)):
            raise DataFormatError("DatasetSnapshot 欄位不得重複")
        if any(not isinstance(column, str) or not column for column in columns):
            raise DataFormatError("DatasetSnapshot 欄位名稱必須是非空文字")

        immutable_rows = tuple(MappingProxyType(dict(row)) for row in self.rows)
        object.__setattr__(self, "columns", columns)
        object.__setattr__(self, "rows", immutable_rows)
        object.__setattr__(
            self,
            "source",
            Path(self.source).resolve(strict=False),
        )

    @property
    def row_count(self) -> int:
        """回傳資料列數。"""

        return len(self.rows)


@dataclass(frozen=True)
class MetadataSnapshot:
    """已驗證的 metadata 集合與別名查詢索引。"""

    actor_aliases: Mapping[str, tuple[str, ...]]
    column_definitions: tuple[Mapping[str, Any], ...]
    event_semantic_registry: Mapping[str, Any]
    court_place: str
    source_dir: Path
    alias_index: Mapping[str, str] = field(repr=False)
    shot_types: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "actor_aliases",
            MappingProxyType(dict(self.actor_aliases)),
        )
        object.__setattr__(
            self,
            "column_definitions",
            tuple(MappingProxyType(dict(item)) for item in self.column_definitions),
        )
        object.__setattr__(
            self,
            "event_semantic_registry",
            MappingProxyType(dict(self.event_semantic_registry)),
        )
        object.__setattr__(
            self,
            "source_dir",
            Path(self.source_dir).resolve(strict=False),
        )
        object.__setattr__(
            self,
            "alias_index",
            MappingProxyType(dict(self.alias_index)),
        )
        object.__setattr__(
            self,
            "shot_types",
            frozenset(self.shot_types),
        )

    @property
    def registry_columns(self) -> frozenset[str]:
        """回傳 semantic registry 已登錄的欄位集合。"""

        fields = self.event_semantic_registry["fields"]
        return frozenset(field["column"] for field in fields)

    def normalize_player(self, value: str) -> str:
        """將球員 alias 正規化為 canonical 名稱，未知時明確失敗。"""

        if not isinstance(value, str) or not value.strip():
            raise UnknownPlayerAliasError("球員 alias 不可為空白")
        key = value.strip().casefold()
        try:
            return self.alias_index[key]
        except KeyError as exc:
            raise UnknownPlayerAliasError(f"未知球員 alias：{value!r}") from exc

    resolve_player_alias = normalize_player

    @property
    def approved_shot_types(self) -> frozenset[str]:
        """回傳符合 v2 契約、排除 ``接不到`` 等淘汰類別的球種集合。"""

        return frozenset(self.shot_types & APPROVED_SHOT_TYPES)
