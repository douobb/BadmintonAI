"""v2 metadata 檔案的唯讀載入與結構驗證。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .constants import (
    APPROVED_SHOT_TYPES,
    METADATA_FILENAMES,
    REGISTRY_ENUMS,
    REGISTRY_FIELD_REQUIRED_KEYS,
)
from .errors import (
    AmbiguousPlayerAliasError,
    MetadataLoadError,
    MetadataValidationError,
)
from .models import DatasetSnapshot, MetadataSnapshot


def load_metadata(directory: str | Path) -> MetadataSnapshot:
    """載入並驗證四份 v2 metadata，不建立或修改任何檔案。"""

    metadata_dir = _resolve_metadata_dir(directory)
    actor_root = _load_json(metadata_dir / METADATA_FILENAMES[0])
    column_root = _load_json(metadata_dir / METADATA_FILENAMES[1])
    registry_root = _load_json(metadata_dir / METADATA_FILENAMES[2])
    court_place = _load_court_place(metadata_dir / METADATA_FILENAMES[3])

    actor_aliases, alias_index = _validate_actor_aliases(actor_root)
    column_definitions, shot_types = _validate_column_definitions(column_root)
    registry = _validate_registry(registry_root)

    return MetadataSnapshot(
        actor_aliases=actor_aliases,
        column_definitions=column_definitions,
        event_semantic_registry=registry,
        court_place=court_place,
        source_dir=metadata_dir,
        alias_index=alias_index,
        shot_types=shot_types or APPROVED_SHOT_TYPES,
    )


def validate_registry_covers_columns(
    snapshot: DatasetSnapshot,
    metadata: MetadataSnapshot,
) -> DatasetSnapshot:
    """確認 semantic registry 涵蓋 snapshot 欄位，不要求 column definitions 全覆蓋。"""

    registered = metadata.registry_columns
    missing = tuple(column for column in snapshot.columns if column not in registered)
    if missing:
        raise MetadataValidationError(
            f"semantic registry 缺少 dataset 欄位：{', '.join(missing)}"
        )
    return snapshot


def _resolve_metadata_dir(directory: str | Path) -> Path:
    try:
        metadata_dir = Path(directory).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise MetadataLoadError("metadata 目錄路徑無法正規化") from exc
    if not metadata_dir.exists():
        raise MetadataLoadError("metadata 目錄不存在")
    if not metadata_dir.is_dir():
        raise MetadataLoadError("metadata 路徑不是目錄")
    return metadata_dir


def _load_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise MetadataLoadError(f"metadata 檔案不存在：{path.name}") from exc
    except UnicodeDecodeError as exc:
        raise MetadataLoadError(f"metadata 檔案不是有效 UTF-8：{path.name}") from exc
    except OSError as exc:
        raise MetadataLoadError(f"metadata 檔案無法讀取：{path.name}") from exc

    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise MetadataLoadError(f"metadata JSON 無法解析：{path.name}") from exc


def _load_court_place(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise MetadataLoadError("court_place.txt 不存在") from exc
    except UnicodeDecodeError as exc:
        raise MetadataLoadError("court_place.txt 不是有效 UTF-8") from exc
    except OSError as exc:
        raise MetadataLoadError("court_place.txt 無法讀取") from exc

    if not text.strip():
        raise MetadataValidationError("court_place.txt 不可為空")
    return text


def _validate_actor_aliases(
    root: Any,
) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    if not isinstance(root, dict) or not isinstance(root.get("actors"), dict):
        raise MetadataValidationError("actor_aliases.json 必須包含 actors object")

    actors: dict[str, tuple[str, ...]] = {}
    alias_index: dict[str, str] = {}
    for canonical, aliases in root["actors"].items():
        canonical_name = _required_text(canonical, "actor canonical 名稱")
        if not isinstance(aliases, list):
            raise MetadataValidationError(
                f"actor {canonical_name!r} 的 aliases 必須是陣列"
            )
        normalized_aliases: list[str] = []
        for alias in [canonical_name, *aliases]:
            alias_name = _required_text(alias, "actor alias")
            normalized_aliases.append(alias_name)
            key = alias_name.casefold()
            previous = alias_index.get(key)
            if previous is not None and previous != canonical_name:
                raise AmbiguousPlayerAliasError(
                    f"球員 alias {alias_name!r} 同時指向 {previous!r} 與 "
                    f"{canonical_name!r}"
                )
            alias_index[key] = canonical_name
        actors[canonical_name] = tuple(dict.fromkeys(normalized_aliases))
    return actors, alias_index


def _validate_column_definitions(
    root: Any,
) -> tuple[tuple[dict[str, Any], ...], frozenset[str]]:
    if not isinstance(root, dict) or not isinstance(root.get("data_columns"), list):
        raise MetadataValidationError(
            "column_definition.json 必須包含 data_columns 陣列"
        )

    definitions: list[dict[str, Any]] = []
    seen_columns: set[str] = set()
    for index, item in enumerate(root["data_columns"], start=1):
        if not isinstance(item, dict):
            raise MetadataValidationError(
                f"column_definition.data_columns[{index}] 必須是 object"
            )
        column = _required_text(item.get("column"), "column definition column")
        description = _required_text(
            item.get("description"),
            f"column definition {column} description",
        )
        if column in seen_columns:
            raise MetadataValidationError(f"column definition 欄位重複：{column}")
        seen_columns.add(column)
        normalized = dict(item)
        normalized["column"] = column
        normalized["description"] = description
        definitions.append(normalized)

    shot_types = _validate_shot_types(root.get("shot_types", {}))
    return tuple(definitions), shot_types


def _validate_shot_types(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, dict):
        raise MetadataValidationError("column_definition.shot_types 必須是 object")
    names: list[str] = []
    for code, name in value.items():
        _required_text(code, "shot type code")
        names.append(_required_text(name, "shot type name"))
    return frozenset(names)


def _validate_registry(root: Any) -> dict[str, Any]:
    if not isinstance(root, dict):
        raise MetadataValidationError(
            "event_semantic_registry.json 根結構必須是 object"
        )
    for key in ("name", "version", "fields"):
        if key not in root:
            raise MetadataValidationError(
                f"event_semantic_registry.json 缺少必要鍵：{key}"
            )
    _required_text(root["name"], "registry name")
    if (
        isinstance(root["version"], bool)
        or not isinstance(root["version"], int)
        or root["version"] < 1
    ):
        raise MetadataValidationError("registry version 必須是正整數")
    if not isinstance(root["fields"], list):
        raise MetadataValidationError("registry fields 必須是陣列")

    fields: list[dict[str, Any]] = []
    seen_columns: set[str] = set()
    for index, item in enumerate(root["fields"], start=1):
        if not isinstance(item, dict):
            raise MetadataValidationError(f"registry fields[{index}] 必須是 object")
        normalized = dict(item)
        for key in REGISTRY_FIELD_REQUIRED_KEYS:
            if key not in item:
                raise MetadataValidationError(
                    f"registry fields[{index}] 缺少必要屬性：{key}"
                )
            normalized_value = _required_text(
                item[key], f"registry fields[{index}] {key}"
            )
            allowed_values = REGISTRY_ENUMS.get(key)
            if allowed_values is not None and normalized_value not in allowed_values:
                raise MetadataValidationError(
                    f"registry fields[{index}] {key} 不在允許 enum："
                    f"{normalized_value!r}"
                )
            normalized[key] = normalized_value
        column = normalized["column"]
        if column in seen_columns:
            raise MetadataValidationError(f"registry 欄位重複：{column}")
        seen_columns.add(column)
        fields.append(normalized)

    normalized = dict(root)
    normalized["fields"] = tuple(fields)
    return normalized


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MetadataValidationError(f"{label} 必須是非空文字")
    return value.strip()


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON key 重複：{key}")
        result[key] = value
    return result
