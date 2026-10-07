"""以 Docker 執行單機 MVP 自訂分析的簡化安全邊界。"""

from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..data.errors import DataError
from ..query import MAX_PAGE_LIMIT, BadmintonQueryService

DEFAULT_SANDBOX_IMAGE = (
    "ghcr.io/douobb/badminton-ai-sandbox@sha256:"
    "78c162aa9c4952347c9d7fc0fb1a948649fdb9d2c7c176085c92cfc3fe2cf678"
)
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MEMORY_BYTES = 512 * 1024 * 1024
DEFAULT_CPU_LIMIT = 1.0
DEFAULT_PIDS_LIMIT = 64
DEFAULT_TMPFS_BYTES = 64 * 1024 * 1024
DEFAULT_CODE_MAX_BYTES = 64 * 1024
DEFAULT_MAX_OUTPUT_FILES = 16
DEFAULT_MAX_OUTPUT_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_OUTPUT_TOTAL_BYTES = 25 * 1024 * 1024
DEFAULT_OUTPUT_EXTENSIONS = (".csv", ".json", ".jsonl", ".png")
MAX_STDOUT_PREVIEW_BYTES = 4 * 1024
STDOUT_PREVIEW_FILE = ".badminton-stdout-preview"
_JOB_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_RENDER_FILE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:-]{0,127}$")
_CONTAINER_PREFIX_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,23}$")
_ARTIFACT_TYPES = {
    ".csv": ("table", "text/csv"),
    ".json": ("json", "application/json"),
    ".jsonl": ("json", "application/jsonl"),
    ".png": ("chart", "image/png"),
}
_ANALYSIS_CODE_EXIT = 86
_ANALYSIS_ERROR_FILE = "/sandbox/output/_badminton_analysis_error.json"
_CODE_ERROR_MESSAGES = {
    "json_serialization": (
        "圖表 JSON 無法序列化；請使用 json.loads(fig.to_json()) 產生 figure 物件"
    ),
    "assertion_failed": "Python 程式的條件檢查未通過；請核對範圍、排除條件與資料定義",
    "syntax_error": "Python 程式語法錯誤；請修正程式後重試",
    "name_error": "Python 程式使用未定義名稱；請核對變數與 import 後重試",
    "missing_module": "Python 程式匯入的模組未安裝於 sandbox 映像；請改用已安裝套件或標準函式庫",
    "missing_file": "繪圖程式引用的保存檔名不存在；請核對工具列出的實際檔案名稱",
    "missing_key": "Python 程式引用的鍵或欄位不在目前物件中；請檢查出錯物件欄位（聚合後可能與原始 df 不同）",
    "missing_attribute": "Python 程式使用不存在的屬性；請核對屬性名稱與物件型別後重試",
    "invalid_value": "Python 程式的值轉換或操作失敗；請核對欄位資料型別與輸入格式",
    "type_mismatch": "Python 程式收到不相容的值型別；請核對運算兩側的型別",
    "python_exception": "Python 分析程式執行失敗；請檢查程式與輸出契約後重試",
}
_LEGACY_CODE_ERROR_HINTS = {"分析程式有未定義變數": "name_error"}
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_EXCEPTION_TYPES = frozenset(
    {
        "AttributeError",
        "AssertionError",
        "FileNotFoundError",
        "KeyError",
        "ModuleNotFoundError",
        "NameError",
        "SyntaxError",
        "TypeError",
        "ValueError",
    }
)


def _analysis_static_references(code: str) -> tuple[set[str], set[str], set[str]]:
    """取得最小 AST 對照集合；不檢查或限制分析程式的執行內容。"""

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set(), set(), set()
    imports: set[str] = set()
    columns: set[str] = set()
    attributes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
        elif isinstance(node, ast.Attribute):
            if _SAFE_IDENTIFIER.fullmatch(node.attr):
                attributes.add(node.attr)
        elif isinstance(node, ast.Subscript):
            base = node.value
            dataframe = isinstance(base, ast.Name) and base.id == "df"
            indexer = (
                isinstance(base, ast.Attribute)
                and isinstance(base.value, ast.Name)
                and base.value.id == "df"
                and base.attr in {"loc", "iloc"}
            )
            if dataframe or indexer:
                columns.update(
                    item.value
                    for item in ast.walk(node.slice)
                    if isinstance(item, ast.Constant)
                    and isinstance(item.value, str)
                    and len(item.value) <= 128
                )
    return imports, columns, attributes


def _analysis_static_references_on_line(
    code: str, line: int | None
) -> tuple[set[str], set[str], set[str]]:
    """只取得錯誤行明寫的鍵、屬性與短檔名，不使用動態錯誤本文。"""

    if not isinstance(line, int) or line < 1:
        return set(), set(), set()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set(), set(), set()
    keys: set[str] = set()
    attributes: set[str] = set()
    filenames: set[str] = set()
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) != line:
            continue
        if isinstance(node, ast.Attribute) and _SAFE_IDENTIFIER.fullmatch(node.attr):
            attributes.add(node.attr)
        elif isinstance(node, ast.Subscript):
            keys.update(
                item.value
                for item in ast.walk(node.slice)
                if isinstance(item, ast.Constant)
                and isinstance(item.value, str)
                and len(item.value) <= 128
            )
        elif isinstance(node, ast.Call):
            function = node.func
            function_name = (
                function.attr if isinstance(function, ast.Attribute) else None
            )
            owner = function.value if isinstance(function, ast.Attribute) else None
            owner_name = owner.id if isinstance(owner, ast.Name) else None
            if owner_name == "px" and function_name in {
                "bar",
                "line",
                "scatter",
                "heatmap",
                "histogram",
                "box",
                "density_heatmap",
            }:
                for keyword in node.keywords:
                    if keyword.arg not in {
                        "x",
                        "y",
                        "color",
                        "facet_row",
                        "facet_col",
                        "hover_name",
                        "animation_frame",
                        "names",
                        "values",
                        "path",
                        "lat",
                        "lon",
                    }:
                        continue
                    value = keyword.value
                    if (
                        isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                        and _SAFE_IDENTIFIER.fullmatch(value.value)
                    ):
                        keys.add(value.value)
            if function_name in {"groupby", "sort_values"}:
                by_value = next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "by"),
                    node.args[0] if node.args else None,
                )
                if isinstance(by_value, ast.Constant) and isinstance(
                    by_value.value, str
                ):
                    candidates = [by_value.value]
                elif isinstance(by_value, (ast.List, ast.Tuple)) and all(
                    isinstance(item, ast.Constant) and isinstance(item.value, str)
                    for item in by_value.elts
                ):
                    candidates = [item.value for item in by_value.elts]
                else:
                    candidates = []
                keys.update(
                    candidate
                    for candidate in candidates
                    if _SAFE_IDENTIFIER.fullmatch(candidate)
                )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            candidate = Path(node.value).name
            if (
                candidate == node.value
                and _SAFE_FILENAME.fullmatch(candidate)
                and Path(candidate).suffix.casefold() in {".csv", ".json", ".jsonl"}
            ):
                filenames.add(candidate)
    return keys, attributes, filenames


def _analysis_static_loaded_names_on_line(code: str, line: int | None) -> set[str]:
    """只找錯誤行直接讀取的名稱，供 host 再驗證固定診斷。"""

    if not isinstance(line, int) or line < 1:
        return set()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.lineno == line
        and _SAFE_IDENTIFIER.fullmatch(node.id)
    }


def _analysis_line_explicitly_raises_keyerror(code: str, line: int | None) -> bool:
    if not isinstance(line, int) or line < 1:
        return False
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.Raise)
        and node.lineno == line
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "KeyError"
        for node in ast.walk(tree)
    )


def _analysis_line_has_dynamic_attribute_error(code: str, line: int | None) -> bool:
    if not isinstance(line, int) or line < 1:
        return False
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.lineno != line:
            continue
        if (
            isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name)
            and node.exc.func.id == "AttributeError"
        ):
            return True
    return any(
        isinstance(node, ast.Call)
        and node.lineno == line
        and isinstance(node.func, ast.Name)
        and node.func.id in {"getattr", "setattr"}
        and len(node.args) >= 2
        and not (
            isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        )
        for node in ast.walk(tree)
    )


def _analysis_line_uses_integer_conversion(code: str, line: int | None) -> bool:
    """只確認錯誤行是否呼叫內建 int，不讀取轉換值。"""

    if not isinstance(line, int) or line < 1:
        return False
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.Call)
        and node.lineno == line
        and isinstance(node.func, ast.Name)
        and node.func.id == "int"
        for node in ast.walk(tree)
    )


def _validate_code_diagnostic(
    diagnostic: Mapping[str, Any],
    *,
    hint: str,
    code: str,
    columns: Sequence[str],
    mode: str = "analysis",
) -> dict[str, Any]:
    """只接受可由原始 code 靜態引用證實的短診斷欄位。"""

    imports, _, _ = _analysis_static_references(code)
    validated: dict[str, Any] = {}
    line = diagnostic.get("line")
    source_lines = code.splitlines()
    if (
        isinstance(line, int)
        and not isinstance(line, bool)
        and 1 <= line <= len(source_lines)
    ):
        validated["line"] = line
        line_keys, line_attributes, line_filenames = (
            _analysis_static_references_on_line(code, line)
        )
        line_names = _analysis_static_loaded_names_on_line(code, line)
        offset = diagnostic.get("offset")
        if (
            hint == "syntax_error"
            and isinstance(offset, int)
            and not isinstance(offset, bool)
            and 1 <= offset <= len(source_lines[line - 1]) + 1
        ):
            validated["offset"] = offset
    else:
        line_keys, line_attributes, line_filenames = set(), set(), set()
        line_names = set()
    exception_type = diagnostic.get("exception_type")
    if exception_type in _SAFE_EXCEPTION_TYPES:
        validated["exception_type"] = exception_type
    if hint == "missing_module":
        module = diagnostic.get("module")
        if (
            isinstance(module, str)
            and len(module) <= 80
            and all(_SAFE_IDENTIFIER.fullmatch(part) for part in module.split("."))
            and any(item == module or item.startswith(module + ".") for item in imports)
        ):
            validated["module"] = module
    elif hint == "name_error":
        if (
            mode == "analysis"
            and diagnostic.get("name") == "results_dir"
            and "results_dir" in line_names
        ):
            validated["name"] = "results_dir"
            validated["stateless_results_dir"] = True
    elif hint == "missing_key":
        key = diagnostic.get("key")
        accepted_keys: set[str] = set()
        if (
            isinstance(key, str)
            and _SAFE_IDENTIFIER.fullmatch(key)
            and key in line_keys
            and not _analysis_line_explicitly_raises_keyerror(code, line)
        ):
            validated["key"] = key
            accepted_keys.add(key)
        keys = diagnostic.get("keys")
        if isinstance(keys, list) and not _analysis_line_explicitly_raises_keyerror(
            code, line
        ):
            accepted = sorted(
                {
                    candidate
                    for candidate in keys[:4]
                    if isinstance(candidate, str)
                    and _SAFE_IDENTIFIER.fullmatch(candidate)
                    and candidate in line_keys
                }
            )
            if accepted:
                if len(accepted) == 1 and "key" not in validated:
                    validated["key"] = accepted[0]
                elif len(accepted) > 1:
                    validated["keys"] = accepted
                accepted_keys.update(accepted)
        if diagnostic.get("schema_column") is True and any(
            candidate in columns for candidate in accepted_keys
        ):
            validated["schema_column"] = True
    elif hint == "missing_attribute":
        attribute = diagnostic.get("attribute")
        if (
            isinstance(attribute, str)
            and _SAFE_IDENTIFIER.fullmatch(attribute)
            and attribute in line_attributes
            and not _analysis_line_has_dynamic_attribute_error(code, line)
        ):
            validated["attribute"] = attribute
            if diagnostic.get("schema_column") is True and attribute in columns:
                validated["schema_column"] = True
    elif hint in {"invalid_value", "type_mismatch"}:
        key = diagnostic.get("key")
        if (
            isinstance(key, str)
            and _SAFE_IDENTIFIER.fullmatch(key)
            and key in line_keys
        ):
            validated["key"] = key
        fields = diagnostic.get("fields")
        if isinstance(fields, list):
            accepted = sorted(
                {
                    field
                    for field in fields[:4]
                    if isinstance(field, str)
                    and _SAFE_IDENTIFIER.fullmatch(field)
                    and field in line_keys
                }
            )
            if accepted:
                validated["fields"] = accepted
    elif hint == "missing_file":
        filename = diagnostic.get("filename")
        if (
            isinstance(filename, str)
            and _SAFE_FILENAME.fullmatch(filename)
            and Path(filename).suffix.casefold() in {".csv", ".json", ".jsonl"}
            and filename in line_filenames
        ):
            validated["filename"] = filename
    elif hint == "assertion_failed":
        if diagnostic.get("exception_type") == "AssertionError":
            validated["exception_type"] = "AssertionError"
    if hint == "invalid_value" and diagnostic.get("integer_conversion") is True:
        if _analysis_line_uses_integer_conversion(code, line):
            validated["integer_conversion"] = True
    return validated


class SandboxError(DataError):
    """sandbox 的領域錯誤基底類別。"""


class SandboxPolicyError(SandboxError):
    """sandbox policy、job 或輸入程式不符合契約。"""


class SandboxUnavailableError(SandboxError):
    """Docker daemon 或指定 runtime image 無法使用。"""


class SandboxMaterializationError(SandboxError):
    """完整授權 snapshot 無法安全物化成 sandbox input。"""


class SandboxExecutionError(SandboxError):
    """sandbox 容器執行失敗。"""


class SandboxCodeError(SandboxExecutionError):
    """模型提供的 Python 程式在 sandbox 中發生可修正的例外。"""

    def __init__(
        self,
        message: str | None = None,
        *,
        hint: str = "python_exception",
        diagnostic: Mapping[str, Any] | None = None,
        _host_validated: bool = False,
    ) -> None:
        if hint not in _CODE_ERROR_MESSAGES:
            hint = "python_exception"
        # 僅接受已知固定訊息作為相容輸入；絕不保留任意例外本文。
        if message in _CODE_ERROR_MESSAGES.values():
            hint = next(
                key for key, value in _CODE_ERROR_MESSAGES.items() if value == message
            )
        elif message in _LEGACY_CODE_ERROR_HINTS:
            hint = _LEGACY_CODE_ERROR_HINTS[message]
        self.hint = hint
        self.diagnostic = dict(diagnostic or {}) if _host_validated else {}
        self.diagnostic_is_host_validated = _host_validated
        super().__init__(_CODE_ERROR_MESSAGES[hint])


def sandbox_code_error_message(exc: SandboxCodeError) -> str:
    """建立只含固定提示與 host 已驗證識別資訊的工具錯誤本文。"""

    hint = exc.hint if exc.hint in _CODE_ERROR_MESSAGES else "python_exception"
    message = _CODE_ERROR_MESSAGES[hint]
    diagnostic = exc.diagnostic if exc.diagnostic_is_host_validated else {}
    if diagnostic.get("schema_column") is True:
        message += "；請檢查出錯物件的欄位與型別；若經過合併，欄名可能帶有 _x／_y 後綴"
    if diagnostic.get("stateless_results_dir") is True:
        message += (
            "；runPythonAnalysis 每次從乾淨環境執行，沒有先前變數或 results_dir。"
            "既有保存結果只供 renderAnalysisChart 讀取；請勿為取回數字而重畫已發布圖表。"
            "需要新分析時請使用本次 df 並保存結果。"
        )
    if hint == "missing_module" and isinstance(diagnostic.get("module"), str):
        message += f"；來源程式匯入的未安裝模組：{diagnostic['module']}"
    elif hint == "missing_key":
        keys = diagnostic.get("keys")
        if isinstance(keys, list):
            safe_keys = [
                key
                for key in keys
                if isinstance(key, str) and _SAFE_IDENTIFIER.fullmatch(key)
            ][:4]
        else:
            key = diagnostic.get("key")
            safe_keys = (
                [key]
                if isinstance(key, str) and _SAFE_IDENTIFIER.fullmatch(key)
                else []
            )
        if safe_keys:
            message += f"；來源程式靜態引用鍵：{'、'.join(safe_keys)}"
    elif hint in {"invalid_value", "type_mismatch"} and isinstance(
        diagnostic.get("key"), str
    ):
        message += f"；來源程式靜態引用鍵：{diagnostic['key']}"
    if hint in {"invalid_value", "type_mismatch"} and isinstance(
        diagnostic.get("fields"), list
    ):
        fields = [
            field
            for field in diagnostic["fields"]
            if isinstance(field, str) and _SAFE_IDENTIFIER.fullmatch(field)
        ][:4]
        if fields:
            message += f"；來源程式此行靜態引用欄位：{'、'.join(fields)}"
    if hint == "invalid_value" and diagnostic.get("integer_conversion") is True:
        message += (
            "；若整數轉換遇到小數字串，先用 pd.to_numeric(errors='coerce')，"
            "檢查缺值與整數性後再轉型，不要直接取整"
        )
    elif hint == "missing_attribute" and isinstance(diagnostic.get("attribute"), str):
        message += f"；靜態屬性引用：{diagnostic['attribute']}"
    elif hint == "missing_file" and isinstance(diagnostic.get("filename"), str):
        message += f"；來源程式引用檔名：{diagnostic['filename']}"
    exception_type = diagnostic.get("exception_type")
    if exception_type in _SAFE_EXCEPTION_TYPES:
        message += f"；例外類型：{exception_type}"
    line = diagnostic.get("line")
    if isinstance(line, int) and not isinstance(line, bool) and line > 0:
        message += f"（分析程式第 {line} 行"
        offset = diagnostic.get("offset")
        if isinstance(offset, int) and not isinstance(offset, bool) and offset > 0:
            message += f"、第 {offset} 欄"
        message += "）"
    return message


class SandboxTimeoutError(SandboxError):
    """sandbox 容器超過明確的執行時間上限。"""


class SandboxArtifactError(SandboxError):
    """sandbox 輸出違反基本路徑、型別或大小契約。"""


class SandboxOutputError(SandboxArtifactError):
    """模型程式寫入的產物不符合輸出契約，可由程式碼修正。"""


class SandboxCleanupError(SandboxError):
    """sandbox 暫存資料無法清理。"""


@dataclass(frozen=True)
class SandboxPolicy:
    """單機 MVP 的 Docker 資源與 artifact policy。"""

    image: str = DEFAULT_SANDBOX_IMAGE
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    memory_bytes: int = DEFAULT_MEMORY_BYTES
    cpu_limit: float = DEFAULT_CPU_LIMIT
    pids_limit: int = DEFAULT_PIDS_LIMIT
    tmpfs_bytes: int = DEFAULT_TMPFS_BYTES
    code_max_bytes: int = DEFAULT_CODE_MAX_BYTES
    max_output_files: int = DEFAULT_MAX_OUTPUT_FILES
    max_output_file_bytes: int = DEFAULT_MAX_OUTPUT_FILE_BYTES
    max_output_total_bytes: int = DEFAULT_MAX_OUTPUT_TOTAL_BYTES
    allowed_output_extensions: tuple[str, ...] = DEFAULT_OUTPUT_EXTENSIONS
    container_name_prefix: str = "badminton-ai-sandbox"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.image, str)
            or _IMAGE_PATTERN.fullmatch(self.image) is None
            or self.image.rsplit("/", maxsplit=1)[-1].endswith(":latest")
            or "\\" in self.image
            or "//" in self.image
            or ":" not in self.image.rsplit("/", maxsplit=1)[-1]
            and "@" not in self.image
        ):
            raise SandboxPolicyError("sandbox image 必須是含固定 tag 或 digest 的名稱")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise SandboxPolicyError("sandbox timeout 必須是有限正數")
        if (
            not isinstance(self.cpu_limit, (int, float))
            or isinstance(self.cpu_limit, bool)
            or not math.isfinite(float(self.cpu_limit))
            or self.cpu_limit <= 0
        ):
            raise SandboxPolicyError("sandbox CPU 限制必須是有限正數")
        for field_name in (
            "memory_bytes",
            "pids_limit",
            "tmpfs_bytes",
            "code_max_bytes",
            "max_output_files",
            "max_output_file_bytes",
            "max_output_total_bytes",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise SandboxPolicyError(f"{field_name} 必須是正整數")
        if (
            not isinstance(self.container_name_prefix, str)
            or _CONTAINER_PREFIX_PATTERN.fullmatch(self.container_name_prefix) is None
        ):
            raise SandboxPolicyError("container name prefix 格式不合法")
        try:
            extensions = tuple(
                sorted(
                    {extension.lower() for extension in self.allowed_output_extensions}
                )
            )
        except (TypeError, AttributeError) as exc:
            raise SandboxPolicyError("allowed output extensions 格式不合法") from exc
        if not extensions or any(
            not extension.startswith(".")
            or "/" in extension
            or "\\" in extension
            or len(extension) == 1
            for extension in extensions
        ):
            raise SandboxPolicyError("allowed output extensions 格式不合法")
        object.__setattr__(self, "allowed_output_extensions", extensions)


@dataclass(frozen=True)
class SandboxJob:
    """一次單機分析工作。"""

    job_id: str
    code: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.job_id, str)
            or _JOB_ID_PATTERN.fullmatch(self.job_id) is None
        ):
            raise SandboxPolicyError("sandbox job id 格式不合法")
        if not isinstance(self.code, str) or "\x00" in self.code:
            raise SandboxPolicyError("sandbox code 必須是無 NUL 的文字")


@dataclass(frozen=True)
class MaterializationManifest:
    """寫入 sandbox input 的完整 snapshot manifest。"""

    format_version: int
    source: str
    columns: tuple[str, ...]
    row_count: int
    snapshot_id: str
    events_file: str = "events.jsonl"
    manifest_file: str = "manifest.json"
    metadata_available: bool = False
    metadata_file: str = "metadata.json"
    schema_available: bool = True
    schema_file: str = "schema.json"
    analysis_file: str = "analysis.py"
    input_files: tuple[str, ...] = (
        "events.jsonl",
        "metadata.json",
        "schema.json",
        "manifest.json",
        "analysis.py",
    )
    mode: str = "analysis"


@dataclass(frozen=True)
class MaterializedSnapshot:
    """一次 job input/output 目錄的 immutable 描述。"""

    job_id: str
    root_dir: str
    input_dir: str
    output_dir: str
    manifest: MaterializationManifest


@dataclass(frozen=True)
class SandboxArtifact:
    """已由 host 讀取並以 base64 保存的 output artifact。"""

    relative_path: str
    kind: str
    extension: str
    mime_type: str
    size_bytes: int
    content_base64: str


@dataclass(frozen=True)
class SandboxInputFile:
    """繪圖 job 可讀的保存資料檔；不包含原始 snapshot。"""

    relative_path: str
    content: bytes

    def __post_init__(self) -> None:
        _validate_render_relative_path(self.relative_path)
        if not isinstance(self.content, bytes):
            raise SandboxPolicyError("繪圖輸入檔案必須是位元組")


@dataclass(frozen=True)
class SandboxResult:
    """成功 sandbox job 的 immutable 結果契約。"""

    job_id: str
    exit_code: int
    manifest: MaterializationManifest
    artifacts: tuple[SandboxArtifact, ...]
    stdout_preview: str | None = None
    stdout_preview_truncated: bool = False


def _canonicalize_json_value(
    value: Any,
    *,
    path: str,
    active_containers: set[int] | None = None,
) -> Any:
    """只接受可穩定 JSON 序列化的值，不使用 repr fallback。"""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SandboxMaterializationError(f"{path} 含非有限浮點數")
        return value
    if active_containers is None:
        active_containers = set()
    if isinstance(value, Mapping):
        value_id = id(value)
        if value_id in active_containers:
            raise SandboxMaterializationError(f"{path} 含循環 mapping")
        active_containers.add(value_id)
        try:
            normalized: dict[str, Any] = {}
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise SandboxMaterializationError(
                        f"{path} 的 mapping key 必須是文字"
                    )
                normalized[key] = _canonicalize_json_value(
                    nested,
                    path=f"{path}[{key!r}]",
                    active_containers=active_containers,
                )
            return normalized
        finally:
            active_containers.remove(value_id)
    if isinstance(value, (list, tuple)):
        value_id = id(value)
        if value_id in active_containers:
            raise SandboxMaterializationError(f"{path} 含循環陣列")
        active_containers.add(value_id)
        try:
            return [
                _canonicalize_json_value(
                    nested,
                    path=f"{path}[{index}]",
                    active_containers=active_containers,
                )
                for index, nested in enumerate(value)
            ]
        finally:
            active_containers.remove(value_id)
    raise SandboxMaterializationError(f"{path} 含不支援的值類型 {type(value).__name__}")


def _render_json(value: Any, *, path: str, sort_keys: bool) -> str:
    try:
        return json.dumps(
            _canonicalize_json_value(value, path=path),
            ensure_ascii=False,
            sort_keys=sort_keys,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SandboxMaterializationError(f"{path} 無法穩定序列化") from exc


def _snapshot_id(columns: tuple[str, ...], rows: tuple[Mapping[str, Any], ...]) -> str:
    payload = _render_json(
        {"columns": columns, "rows": rows},
        path="snapshot",
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _validate_job_id(job_id: str) -> None:
    if not isinstance(job_id, str) or _JOB_ID_PATTERN.fullmatch(job_id) is None:
        raise SandboxPolicyError("sandbox job id 格式不合法")


def _validate_render_relative_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or "\\" in value
        or ":" in value
        or "\x00" in value
    ):
        raise SandboxPolicyError("繪圖輸入路徑不合法")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or not 1 <= len(path.parts) <= 4
        or any(
            component in {".", ".."}
            or _RENDER_FILE_COMPONENT.fullmatch(component) is None
            for component in path.parts
        )
        or Path(value).suffix.casefold() not in {".csv", ".json", ".jsonl"}
    ):
        raise SandboxPolicyError("繪圖輸入路徑不合法")
    return value


def _safe_source_name(source: str) -> str:
    try:
        name = Path(source).name
    except (OSError, RuntimeError, TypeError, ValueError):
        name = ""
    return name if name not in {"", ".", ".."} else "dataset"


def _metadata_payload(query_service: BadmintonQueryService) -> dict[str, Any]:
    """建立不含 host 路徑的 JSON-compatible metadata input。"""

    metadata = query_service.metadata
    if metadata is None:
        return {
            "available": False,
            "reason": "query service 未提供已驗證 metadata",
        }
    return {
        "available": True,
        "actor_aliases": {
            canonical: list(aliases)
            for canonical, aliases in sorted(metadata.actor_aliases.items())
        },
        "column_definitions": [
            dict(column_definition) for column_definition in metadata.column_definitions
        ],
        "event_semantic_registry": metadata.event_semantic_registry,
        "court_place": metadata.court_place,
        "approved_shot_types": sorted(metadata.approved_shot_types),
    }


def _schema_payload(columns: tuple[str, ...]) -> dict[str, Any]:
    """建立分析程式可讀的 canonical event schema 描述。"""

    from ..data.constants import MVP_REQUIRED_COLUMNS

    return {
        "available": True,
        "format": "canonical-event-jsonl-v1",
        "columns": list(columns),
        "field_order": list(columns),
        "required_columns": list(MVP_REQUIRED_COLUMNS),
    }


class SnapshotMaterializer:
    """透過 query public API 將完整 canonical snapshot 寫入 job input/output。"""

    def __init__(self, temp_dir: str | Path | None = None) -> None:
        if temp_dir is None:
            self._temp_dir: Path | None = None
        else:
            try:
                resolved = Path(temp_dir).expanduser().resolve(strict=False)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise SandboxPolicyError("sandbox temp 目錄無法正規化") from exc
            if not resolved.exists() or not resolved.is_dir():
                raise SandboxPolicyError("sandbox temp 目錄不存在或不是目錄")
            self._temp_dir = resolved
        self._owned_roots: set[Path] = set()

    def materialize(
        self,
        query_service: BadmintonQueryService,
        *,
        job_id: str | None = None,
    ) -> MaterializedSnapshot:
        """完整讀完所有 query 分頁並寫入 input 與空 output 目錄。"""

        if not isinstance(query_service, BadmintonQueryService):
            raise SandboxMaterializationError(
                "snapshot materializer 必須接收 BadmintonQueryService"
            )
        if job_id is None:
            job_id = uuid.uuid4().hex
        _validate_job_id(job_id)
        try:
            root = Path(
                tempfile.mkdtemp(
                    prefix=f"badminton-ai-sandbox-{job_id}-",
                    dir=str(self._temp_dir) if self._temp_dir is not None else None,
                )
            ).resolve()
        except (OSError, RuntimeError) as exc:
            raise SandboxMaterializationError("sandbox job 暫存目錄無法建立") from exc
        self._owned_roots.add(root)
        try:
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            output_dir.mkdir()
            columns, source, rows = self._read_all_pages(query_service)
            events_file = input_dir / "events.jsonl"
            with events_file.open("w", encoding="utf-8", newline="\n") as handle:
                for index, row in enumerate(rows, start=1):
                    record = {
                        column: _canonicalize_json_value(
                            row[column],
                            path=f"event[{index}].{column}",
                        )
                        for column in columns
                    }
                    handle.write(
                        _render_json(
                            record,
                            path=f"event[{index}]",
                            sort_keys=False,
                        )
                        + "\n"
                    )
            metadata_file = input_dir / "metadata.json"
            metadata_file.write_text(
                _render_json(
                    _metadata_payload(query_service),
                    path="metadata",
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            schema_file = input_dir / "schema.json"
            schema_file.write_text(
                _render_json(_schema_payload(columns), path="schema", sort_keys=True),
                encoding="utf-8",
            )
            manifest = MaterializationManifest(
                format_version=1,
                source=_safe_source_name(source),
                columns=columns,
                row_count=len(rows),
                snapshot_id=_snapshot_id(columns, rows),
                metadata_available=query_service.metadata is not None,
            )
            (input_dir / manifest.manifest_file).write_text(
                _render_json(asdict(manifest), path="manifest", sort_keys=True),
                encoding="utf-8",
            )
            return MaterializedSnapshot(
                job_id=job_id,
                root_dir=str(root),
                input_dir=str(input_dir),
                output_dir=str(output_dir),
                manifest=manifest,
            )
        except DataError:
            self.cleanup(
                MaterializedSnapshot(
                    job_id=job_id,
                    root_dir=str(root),
                    input_dir=str(root / "input"),
                    output_dir=str(root / "output"),
                    manifest=MaterializationManifest(1, "", (), 0, ""),
                )
            )
            raise
        except (KeyError, OSError, TypeError, ValueError) as exc:
            self.cleanup(
                MaterializedSnapshot(
                    job_id=job_id,
                    root_dir=str(root),
                    input_dir=str(root / "input"),
                    output_dir=str(root / "output"),
                    manifest=MaterializationManifest(1, "", (), 0, ""),
                )
            )
            raise SandboxMaterializationError("snapshot input 無法安全物化") from exc

    def materialize_render(
        self,
        files: Sequence[SandboxInputFile],
        *,
        snapshot_id: str,
        job_id: str | None = None,
    ) -> MaterializedSnapshot:
        """只將指定的已保存 JSON/CSV 檔案物化為 render 唯讀輸入。"""

        if (
            not isinstance(snapshot_id, str)
            or not snapshot_id
            or len(snapshot_id) > 128
        ):
            raise SandboxMaterializationError("保存結果 snapshot 識別資訊無效")
        if not isinstance(files, Sequence) or not files:
            raise SandboxMaterializationError("繪圖至少需要一個已保存資料檔")
        if job_id is None:
            job_id = uuid.uuid4().hex
        _validate_job_id(job_id)
        seen: set[str] = set()
        for item in files:
            if not isinstance(item, SandboxInputFile):
                raise SandboxMaterializationError("繪圖輸入檔案格式無效")
            _validate_render_relative_path(item.relative_path)
            if item.relative_path in seen:
                raise SandboxMaterializationError("繪圖輸入檔案重複")
            seen.add(item.relative_path)
        try:
            root = Path(
                tempfile.mkdtemp(
                    prefix=f"badminton-ai-sandbox-{job_id}-",
                    dir=str(self._temp_dir) if self._temp_dir is not None else None,
                )
            ).resolve()
        except (OSError, RuntimeError) as exc:
            raise SandboxMaterializationError("sandbox job 暫存目錄無法建立") from exc
        self._owned_roots.add(root)
        input_dir = root / "input"
        output_dir = root / "output"
        try:
            input_dir.mkdir()
            output_dir.mkdir()
            results_dir = input_dir / "results"
            results_dir.mkdir()
            names: list[str] = []
            for item in files:
                target = results_dir.joinpath(*PurePosixPath(item.relative_path).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(item.content)
                names.append(f"results/{item.relative_path}")
            manifest = MaterializationManifest(
                format_version=1,
                source="saved-analysis",
                columns=(),
                row_count=0,
                snapshot_id=snapshot_id,
                metadata_available=False,
                schema_available=False,
                input_files=("results/", "manifest.json", "analysis.py", *names),
                mode="render",
            )
            (input_dir / manifest.manifest_file).write_text(
                _render_json(asdict(manifest), path="manifest", sort_keys=True),
                encoding="utf-8",
            )
            return MaterializedSnapshot(
                job_id=job_id,
                root_dir=str(root),
                input_dir=str(input_dir),
                output_dir=str(output_dir),
                manifest=manifest,
            )
        except DataError:
            self.cleanup(
                MaterializedSnapshot(
                    job_id=job_id,
                    root_dir=str(root),
                    input_dir=str(input_dir),
                    output_dir=str(output_dir),
                    manifest=MaterializationManifest(1, "", (), 0, ""),
                )
            )
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.cleanup(
                MaterializedSnapshot(
                    job_id=job_id,
                    root_dir=str(root),
                    input_dir=str(input_dir),
                    output_dir=str(output_dir),
                    manifest=MaterializationManifest(1, "", (), 0, ""),
                )
            )
            raise SandboxMaterializationError("保存結果無法安全物化") from exc

    def cleanup(self, materialized: MaterializedSnapshot) -> None:
        """只清理此 materializer 建立的精確 job root。"""

        try:
            root = Path(materialized.root_dir).resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise SandboxCleanupError("sandbox job root 無法正規化") from exc
        if root not in self._owned_roots:
            raise SandboxCleanupError("sandbox job root 不屬於目前 materializer")
        try:
            if root.exists():
                self._restore_cleanup_permissions(root)
                shutil.rmtree(root)
        except OSError as exc:
            raise SandboxCleanupError("sandbox job 暫存資料無法清理") from exc
        finally:
            self._owned_roots.discard(root)

    @staticmethod
    def _restore_cleanup_permissions(root: Path) -> None:
        """移除 mount 的 non-root 權限設定，確保跨平台可清理。"""

        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in directories:
                path = current_path / name
                if not path.is_symlink():
                    os.chmod(path, 0o700)
            for name in files:
                path = current_path / name
                if not path.is_symlink():
                    os.chmod(path, 0o600)
        os.chmod(root, 0o700)

    @staticmethod
    def _read_all_pages(
        query_service: BadmintonQueryService,
    ) -> tuple[tuple[str, ...], str, tuple[Mapping[str, Any], ...]]:
        rows: list[Mapping[str, Any]] = []
        columns: tuple[str, ...] | None = None
        source: str | None = None
        expected_total: int | None = None
        offset = 0
        while True:
            page = query_service.query_events(limit=MAX_PAGE_LIMIT, offset=offset)
            page_columns = tuple(page.columns)
            page_source = str(page.source)
            if columns is None:
                columns = page_columns
                source = page_source
                expected_total = page.total_count
            elif page_columns != columns or page_source != source:
                raise SandboxMaterializationError("query 分頁 metadata 不一致")
            if expected_total != page.total_count:
                raise SandboxMaterializationError("query 分頁 total count 不一致")
            rows.extend(page.rows)
            if not page.rows:
                if expected_total != len(rows):
                    raise SandboxMaterializationError("query 分頁在完整資料前停止")
                break
            if expected_total is None or len(rows) >= expected_total:
                if expected_total != len(rows):
                    raise SandboxMaterializationError("query 分頁超過宣告列數")
                break
            offset += len(page.rows)
        return columns or (), source or "", tuple(rows)


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


class DockerSandboxRunner:
    """以 argv-only Docker CLI 啟動單機、禁網路分析工作。"""

    def __init__(
        self,
        policy: SandboxPolicy | None = None,
        *,
        docker_executable: str = "docker",
        materializer: SnapshotMaterializer | None = None,
        process_runner: ProcessRunner = subprocess.run,
    ) -> None:
        self.policy = policy or SandboxPolicy()
        if (
            not isinstance(docker_executable, str)
            or not docker_executable.strip()
            or "\x00" in docker_executable
            or docker_executable.startswith("-")
        ):
            raise SandboxPolicyError("docker executable 不合法")
        self.docker_executable = docker_executable
        self._materializer = materializer or SnapshotMaterializer()
        self._process_runner = process_runner

    def run(self, query_service: BadmintonQueryService, code: str) -> SandboxResult:
        """驗證 runtime 後執行一次完整 snapshot sandbox job。"""

        job = SandboxJob(job_id=uuid.uuid4().hex, code=code)
        return self.run_job(query_service, job)

    def run_job(
        self,
        query_service: BadmintonQueryService,
        job: SandboxJob,
    ) -> SandboxResult:
        if not isinstance(query_service, BadmintonQueryService):
            raise SandboxPolicyError("sandbox runner 必須接收 BadmintonQueryService")
        if not isinstance(job, SandboxJob):
            raise SandboxPolicyError("sandbox runner 必須接收 SandboxJob")
        if len(job.code.encode("utf-8")) > self.policy.code_max_bytes:
            raise SandboxPolicyError("sandbox code 超過大小上限")
        self._check_runtime()
        materialized = self._materializer.materialize(
            query_service,
            job_id=job.job_id,
        )
        return self._execute_materialized(
            materialized, job_id=job.job_id, code=job.code
        )

    def run_render(
        self,
        files: Sequence[SandboxInputFile],
        *,
        snapshot_id: str,
        code: str,
    ) -> SandboxResult:
        """只讀已保存分析檔執行繪圖，不接收或查詢原始資料服務。"""

        if not isinstance(code, str) or "\x00" in code:
            raise SandboxPolicyError("sandbox code 必須是無 NUL 的文字")
        if len(code.encode("utf-8")) > self.policy.code_max_bytes:
            raise SandboxPolicyError("sandbox code 超過大小上限")
        if not isinstance(files, Sequence) or not files:
            raise SandboxPolicyError("繪圖至少需要一個已保存資料檔")
        if len(files) > self.policy.max_output_files:
            raise SandboxPolicyError("繪圖輸入檔案數量超過上限")
        total_bytes = 0
        for item in files:
            if not isinstance(item, SandboxInputFile):
                raise SandboxPolicyError("繪圖輸入檔案格式無效")
            if len(item.content) > self.policy.max_output_file_bytes:
                raise SandboxPolicyError("繪圖輸入檔案超過大小上限")
            total_bytes += len(item.content)
        if total_bytes > self.policy.max_output_total_bytes:
            raise SandboxPolicyError("繪圖輸入總大小超過上限")
        self._check_runtime()
        job_id = uuid.uuid4().hex
        materialized = self._materializer.materialize_render(
            files,
            snapshot_id=snapshot_id,
            job_id=job_id,
        )
        return self._execute_materialized(materialized, job_id=job_id, code=code)

    def _execute_materialized(
        self,
        materialized: MaterializedSnapshot,
        *,
        job_id: str,
        code: str,
    ) -> SandboxResult:
        cleanup_allowed = True
        container_created = False
        container_removed = False
        container_name = self._container_name(uuid.uuid4().hex)
        try:
            self._validate_materialized_paths(materialized)
            code_path = (
                Path(materialized.input_dir) / materialized.manifest.analysis_file
            )
            try:
                code_path.write_bytes(code.encode("utf-8"))
                self._prepare_mounts(materialized)
            except OSError as exc:
                raise SandboxMaterializationError(
                    "sandbox input/output 無法安全設定"
                ) from exc
            command = self._docker_run_command(materialized, container_name)
            try:
                self._create_container(command)
                container_created = True
                self._start_container(container_name)
                self._copy_input(container_name, Path(materialized.input_dir))
                self._make_input_readonly(container_name)
                completed = self._run_container(
                    container_name,
                    timeout=float(self.policy.timeout_seconds),
                )
                if completed.returncode == _ANALYSIS_CODE_EXIT:
                    raise self._code_error(
                        container_name,
                        code=code,
                        columns=materialized.manifest.columns,
                        mode=materialized.manifest.mode,
                    )
                if completed.returncode != 0:
                    raise SandboxExecutionError("sandbox 容器執行失敗")
                self._copy_output(container_name, Path(materialized.output_dir))
            except subprocess.TimeoutExpired as exc:
                try:
                    self._terminate_container(container_name)
                    container_removed = True
                except SandboxExecutionError:
                    cleanup_allowed = False
                    raise
                raise SandboxTimeoutError("sandbox 執行逾時") from exc
            output_dir = Path(materialized.output_dir)
            artifacts = self._collect_artifacts(output_dir)
            stdout_preview, stdout_preview_truncated = self._read_stdout_preview(
                output_dir
            )
            self._remove_container(container_name)
            container_removed = True
            return SandboxResult(
                job_id=job_id,
                exit_code=completed.returncode,
                manifest=materialized.manifest,
                artifacts=artifacts,
                stdout_preview=stdout_preview,
                stdout_preview_truncated=stdout_preview_truncated,
            )
        finally:
            if container_created and not container_removed and cleanup_allowed:
                try:
                    self._remove_container(container_name)
                    container_removed = True
                except SandboxExecutionError:
                    cleanup_allowed = False
                    raise
            if materialized is not None and cleanup_allowed:
                self._materializer.cleanup(materialized)

    def _run_container(
        self,
        container_name: str,
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        """以 argv list 執行 container 內的分析程式。"""

        return self._process_runner(
            [
                self.docker_executable,
                "exec",
                "--user",
                "1000:1000",
                container_name,
                "python",
                "/sandbox/bootstrap.py",
                "/sandbox/input/analysis.py",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
            timeout=timeout,
        )

    def _code_error(
        self,
        container_name: str,
        *,
        code: str,
        columns: Sequence[str],
        mode: str = "analysis",
    ) -> SandboxCodeError:
        """擷取短診斷後由 host 對照原始程式與 schema 再驗證。"""

        reader = (
            "from pathlib import Path; "
            f"p=Path({_ANALYSIS_ERROR_FILE!r}); "
            "print(p.read_bytes()[:512].decode('utf-8') if p.is_file() else '')"
        )
        try:
            completed = self._process_runner(
                [
                    self.docker_executable,
                    "exec",
                    "--user",
                    "1000:1000",
                    container_name,
                    "python",
                    "-c",
                    reader,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                text=True,
                timeout=5.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return SandboxCodeError(_CODE_ERROR_MESSAGES["python_exception"])
        hint = "python_exception"
        diagnostic: dict[str, Any] = {}
        if completed.returncode == 0 and isinstance(completed.stdout, str):
            try:
                payload = json.loads(completed.stdout)
                if not isinstance(payload, dict):
                    raise ValueError("invalid diagnostic envelope")
                if (
                    payload.get("version") in {1, 2}
                    and payload.get("hint") in _CODE_ERROR_MESSAGES
                ):
                    hint = payload["hint"]
                    raw_diagnostic = payload.get("diagnostic")
                    if payload.get("version") == 2 and isinstance(raw_diagnostic, dict):
                        diagnostic = _validate_code_diagnostic(
                            raw_diagnostic,
                            hint=hint,
                            code=code,
                            columns=columns,
                            mode=mode,
                        )
            except (TypeError, ValueError, AttributeError):
                pass
        return SandboxCodeError(
            hint=hint,
            diagnostic=diagnostic,
            _host_validated=True,
        )

    def _create_container(self, command: Sequence[str]) -> None:
        completed = self._call(command, timeout=10.0)
        if completed.returncode != 0:
            raise SandboxExecutionError("sandbox container 建立失敗")

    def _start_container(self, container_name: str) -> None:
        completed = self._call(
            [self.docker_executable, "start", container_name],
            timeout=10.0,
        )
        if completed.returncode != 0:
            raise SandboxExecutionError("sandbox container 啟動失敗")

    def _copy_input(self, container_name: str, input_dir: Path) -> None:
        archive = self._input_archive(input_dir)
        completed = self._call_with_input(
            [
                self.docker_executable,
                "exec",
                "-i",
                "--user",
                "0:0",
                container_name,
                "tar",
                "-x",
                "-f",
                "-",
                "-C",
                "/sandbox/input",
            ],
            archive,
            timeout=10.0,
        )
        if completed.returncode != 0:
            raise SandboxExecutionError("sandbox input 傳輸失敗")

    def _make_input_readonly(self, container_name: str) -> None:
        completed = self._call(
            [
                self.docker_executable,
                "exec",
                "--user",
                "0:0",
                container_name,
                "chmod",
                "-R",
                "a-w",
                "/sandbox/input",
            ],
            timeout=5.0,
        )
        if completed.returncode != 0:
            raise SandboxExecutionError("sandbox input 唯讀設定失敗")

    def _copy_output(self, container_name: str, output_dir: Path) -> None:
        completed = self._process_runner(
            [
                self.docker_executable,
                "exec",
                "--user",
                "1000:1000",
                container_name,
                "tar",
                "-c",
                "-C",
                "/sandbox/output",
                ".",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=False,
            timeout=10.0,
        )
        if completed.returncode != 0:
            raise SandboxArtifactError("sandbox output 無法收集")
        payload = completed.stdout
        if not isinstance(payload, bytes):
            raise SandboxArtifactError("sandbox output 傳輸格式錯誤")
        archive_overhead = max(1024, self.policy.max_output_files * 1024)
        if len(payload) > self.policy.max_output_total_bytes + archive_overhead:
            raise SandboxArtifactError("sandbox output 傳輸超過大小上限")
        self._extract_output_archive(payload, output_dir)

    def _extract_output_archive(self, payload: bytes, output_dir: Path) -> None:
        """安全解開 trusted container tar，之後仍由 host 重做 artifact 檢查。"""

        seen: set[str] = set()
        file_count = 0
        total_bytes = 0
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
                for member in archive.getmembers():
                    name = member.name
                    pure_name = PurePosixPath(name)
                    if (
                        not name
                        or "\\" in name
                        or pure_name.is_absolute()
                        or ".." in pure_name.parts
                        or name in seen
                    ):
                        raise SandboxArtifactError(
                            "sandbox artifact archive 路徑不合法"
                        )
                    seen.add(name)
                    candidate = (output_dir / Path(*pure_name.parts)).resolve(
                        strict=False
                    )
                    self._ensure_inside_artifact_root(output_dir, candidate)
                    if member.issym() or member.islnk():
                        raise SandboxArtifactError(
                            "sandbox artifact archive 不允許 symlink"
                        )
                    if member.isdir():
                        candidate.mkdir(parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        raise SandboxArtifactError(
                            "sandbox artifact archive 只允許一般檔案"
                        )
                    is_stdout_preview = (
                        pure_name.as_posix().removeprefix("./") == STDOUT_PREVIEW_FILE
                    )
                    if is_stdout_preview:
                        if member.size > MAX_STDOUT_PREVIEW_BYTES + 1:
                            raise SandboxArtifactError(
                                "sandbox stdout preview 超過大小上限"
                            )
                    else:
                        file_count += 1
                        if file_count > self.policy.max_output_files:
                            raise SandboxArtifactError("sandbox artifact 數量超過上限")
                        if member.size > self.policy.max_output_file_bytes:
                            raise SandboxArtifactError(
                                "sandbox 單一 artifact 超過大小上限"
                            )
                        total_bytes += member.size
                        if total_bytes > self.policy.max_output_total_bytes:
                            raise SandboxArtifactError(
                                "sandbox artifact 總大小超過上限"
                            )
                    candidate.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise SandboxArtifactError("sandbox artifact archive 無法讀取")
                    data = source.read(member.size + 1)
                    if len(data) != member.size:
                        raise SandboxArtifactError(
                            "sandbox artifact archive 內容大小不一致"
                        )
                    candidate.write_bytes(data)
        except SandboxArtifactError:
            raise
        except (OSError, tarfile.TarError, ValueError) as exc:
            raise SandboxArtifactError("sandbox output archive 無法解開") from exc

    @staticmethod
    def _ensure_inside_artifact_root(root: Path, candidate: Path) -> None:
        try:
            candidate.relative_to(root.resolve(strict=False))
        except (OSError, RuntimeError, ValueError) as exc:
            raise SandboxArtifactError(
                "sandbox artifact archive 路徑超出 output root"
            ) from exc

    @staticmethod
    def _input_archive(input_dir: Path) -> bytes:
        """將受信任的 materialized input 打包供 docker exec 解開。"""

        buffer = io.BytesIO()
        try:
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                for path in sorted(input_dir.iterdir(), key=lambda item: item.name):
                    if path.is_symlink() or not (path.is_file() or path.is_dir()):
                        raise SandboxMaterializationError("sandbox input 路徑無效")
                    if path.is_dir():
                        for current, directories, files in os.walk(
                            path,
                            followlinks=False,
                        ):
                            current_path = Path(current)
                            for name in (*directories, *files):
                                candidate = current_path / name
                                if candidate.is_symlink() or not (
                                    candidate.is_file() or candidate.is_dir()
                                ):
                                    raise SandboxMaterializationError(
                                        "sandbox input 不允許 symlink"
                                    )
                    archive.add(
                        path,
                        arcname=path.name,
                        recursive=path.is_dir(),
                    )
        except (OSError, tarfile.TarError) as exc:
            raise SandboxMaterializationError("sandbox input 無法打包") from exc
        return buffer.getvalue()

    def _call_with_input(
        self,
        command: Sequence[str],
        input_data: bytes,
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        return self._process_runner(
            list(command),
            input=input_data,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            text=False,
            timeout=timeout,
        )

    def _check_runtime(self) -> None:
        try:
            daemon = self._call(
                [self.docker_executable, "version", "--format={{.Server.Version}}"],
                timeout=5.0,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxUnavailableError("Docker daemon 無法使用") from exc
        if daemon.returncode != 0:
            raise SandboxUnavailableError("Docker daemon 無法使用")
        try:
            image = self._call(
                [self.docker_executable, "image", "inspect", self.policy.image],
                timeout=5.0,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxUnavailableError("sandbox image 無法使用") from exc
        if image.returncode != 0:
            raise SandboxUnavailableError("sandbox image 無法使用")

    def _call(
        self,
        command: Sequence[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        return self._process_runner(
            list(command),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
            timeout=timeout,
        )

    def _terminate_container(self, container_name: str) -> None:
        try:
            completed = self._call(
                [self.docker_executable, "rm", "-f", container_name],
                timeout=5.0,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxExecutionError("sandbox container 終止狀態無法確認") from exc
        if completed.returncode != 0:
            raise SandboxExecutionError("sandbox container 終止失敗")

    def _remove_container(self, container_name: str) -> None:
        try:
            completed = self._call(
                [self.docker_executable, "rm", "-f", "-v", container_name],
                timeout=5.0,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxExecutionError("sandbox container 清理狀態無法確認") from exc
        if completed.returncode != 0:
            raise SandboxExecutionError("sandbox container 清理失敗")

    def _container_name(self, nonce: str) -> str:
        name = f"{self.policy.container_name_prefix}-{nonce}"
        if len(name) > 63:
            raise SandboxPolicyError("container name 超過 Docker 限制")
        return name

    @staticmethod
    def _validate_materialized_paths(materialized: MaterializedSnapshot) -> None:
        try:
            root = Path(materialized.root_dir).resolve(strict=True)
            expected_paths = {
                "input": Path(materialized.input_dir),
                "output": Path(materialized.output_dir),
            }
            for label, path in expected_paths.items():
                if path.is_symlink() or path.resolve(strict=True) != root / label:
                    raise SandboxMaterializationError(
                        f"sandbox {label} mount 路徑不在 job root"
                    )
                if not path.is_dir():
                    raise SandboxMaterializationError(f"sandbox {label} mount 不是目錄")
        except SandboxError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise SandboxMaterializationError("sandbox mount 路徑無法驗證") from exc

    def _docker_run_command(
        self,
        materialized: MaterializedSnapshot,
        container_name: str,
    ) -> list[str]:
        """組裝不含 host bind path 的 Docker container 建立命令。

        Tool Server 可能位於另一個容器；將 host 暫存路徑交給 Docker daemon
        會使 daemon 看見不存在的路徑。因此 input/output 透過 container 內
        受限 tmpfs 與 ``docker exec`` tar stream 傳輸，避免 namespace
        路徑誤綁定。
        """

        policy = self.policy
        input_size = self._input_size(Path(materialized.input_dir))
        input_tmpfs_size = max(policy.tmpfs_bytes, input_size + 1024 * 1024)
        inode_limit = max(32, policy.max_output_files * 4)
        output_tmpfs_size = policy.max_output_total_bytes + MAX_STDOUT_PREVIEW_BYTES + 1
        max_file_size = max(
            policy.max_output_file_bytes,
            MAX_STDOUT_PREVIEW_BYTES + 1,
        )
        return [
            self.docker_executable,
            "create",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--workdir",
            "/sandbox/output",
            "--user",
            "1000:1000",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--init",
            "--memory",
            f"{policy.memory_bytes}b",
            "--memory-swap",
            f"{policy.memory_bytes}b",
            "--cpus",
            str(policy.cpu_limit),
            "--pids-limit",
            str(policy.pids_limit),
            "--ulimit",
            f"fsize={max_file_size}:{max_file_size}",
            "--tmpfs",
            (
                "/sandbox/input:rw,noexec,nosuid,nodev,"
                f"size={input_tmpfs_size},nr_inodes={inode_limit},"
                "uid=0,gid=0,mode=755"
            ),
            "--tmpfs",
            (
                "/sandbox/output:rw,nosuid,nodev,"
                f"size={output_tmpfs_size},nr_inodes={inode_limit},"
                "uid=1000,gid=1000,mode=700"
            ),
            "--tmpfs",
            (
                "/tmp:rw,noexec,nosuid,nodev,"
                f"size={policy.tmpfs_bytes},nr_inodes={inode_limit},"
                "uid=1000,gid=1000,mode=700"
            ),
            "--env",
            "HOME=/tmp",
            "--env",
            "MPLCONFIGDIR=/tmp/matplotlib",
            "--env",
            "MPLBACKEND=Agg",
            "--env",
            "BADMINTON_INPUT_DIR=/sandbox/input",
            "--env",
            "BADMINTON_EVENTS_FILE=/sandbox/input/events.jsonl",
            "--env",
            "BADMINTON_MANIFEST_FILE=/sandbox/input/manifest.json",
            "--env",
            "BADMINTON_METADATA_FILE=/sandbox/input/metadata.json",
            "--env",
            "BADMINTON_SCHEMA_FILE=/sandbox/input/schema.json",
            "--env",
            "BADMINTON_OUTPUT_DIR=/sandbox/output",
            policy.image,
            "tail",
            "-f",
            "/dev/null",
        ]

    @staticmethod
    def _input_size(input_dir: Path) -> int:
        total = 0
        try:
            for current, directories, files in os.walk(input_dir, followlinks=False):
                current_path = Path(current)
                for name in directories:
                    candidate = current_path / name
                    if candidate.is_symlink() or not candidate.is_dir():
                        raise SandboxMaterializationError("sandbox input 路徑無效")
                for name in files:
                    candidate = current_path / name
                    if candidate.is_symlink() or not candidate.is_file():
                        raise SandboxMaterializationError("sandbox input 路徑無效")
                    total += candidate.stat().st_size
        except (OSError, ValueError) as exc:
            raise SandboxMaterializationError("sandbox input 大小無法確認") from exc
        return total

    @staticmethod
    def _prepare_mounts(materialized: MaterializedSnapshot) -> None:
        """讓固定 non-root UID 可讀取 input 並寫入 output。"""

        try:
            os.chmod(materialized.root_dir, 0o755)
            os.chmod(materialized.input_dir, 0o555)
            os.chmod(materialized.output_dir, 0o777)
        except OSError as exc:
            raise SandboxMaterializationError("sandbox mount 權限無法安全設定") from exc

    def _collect_artifacts(self, output_dir: Path) -> tuple[SandboxArtifact, ...]:
        """從 host output 目錄做基本檔案、副檔名與大小檢查。"""

        try:
            root = output_dir.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SandboxArtifactError("sandbox output 目錄無法讀取") from exc
        file_paths: list[Path] = []
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in (*directories, *files):
                candidate = current_path / name
                if candidate.is_symlink():
                    raise SandboxOutputError("sandbox artifact 不允許 symlink")
                self._ensure_inside(root, candidate)
            file_paths.extend(
                current_path / name
                for name in files
                if (current_path / name).relative_to(root).as_posix()
                != STDOUT_PREVIEW_FILE
            )
        if not file_paths:
            preview, _ = self._read_stdout_preview(root)
            if preview is None:
                raise SandboxOutputError("sandbox 必須產生至少一個 artifact")
        if len(file_paths) > self.policy.max_output_files:
            raise SandboxOutputError("sandbox artifact 數量超過上限")

        total_bytes = 0
        artifacts: list[SandboxArtifact] = []
        for path in sorted(
            file_paths, key=lambda item: item.relative_to(root).as_posix()
        ):
            relative_path = path.relative_to(root).as_posix()
            extension = path.suffix.lower()
            if extension not in self.policy.allowed_output_extensions:
                raise SandboxOutputError("sandbox artifact 副檔名不被允許")
            try:
                stat = path.stat()
                if not path.is_file():
                    raise SandboxOutputError("sandbox artifact 必須是一般檔案")
                size_bytes = stat.st_size
                if size_bytes > self.policy.max_output_file_bytes:
                    raise SandboxOutputError("sandbox 單一 artifact 超過大小上限")
                total_bytes += size_bytes
                if total_bytes > self.policy.max_output_total_bytes:
                    raise SandboxOutputError("sandbox artifact 總大小超過上限")
                data = path.read_bytes()
            except OSError as exc:
                raise SandboxArtifactError("sandbox artifact 無法讀取") from exc
            if len(data) != size_bytes:
                raise SandboxArtifactError("sandbox artifact 在讀取期間變更")
            kind, mime_type = _ARTIFACT_TYPES.get(
                extension,
                ("file", "application/octet-stream"),
            )
            artifacts.append(
                SandboxArtifact(
                    relative_path=relative_path,
                    kind=kind,
                    extension=extension,
                    mime_type=mime_type,
                    size_bytes=size_bytes,
                    content_base64=base64.b64encode(data).decode("ascii"),
                )
            )
        return tuple(artifacts)

    @staticmethod
    def _read_stdout_preview(output_dir: Path) -> tuple[str | None, bool]:
        """讀取 bootstrap 固定上限的內部探查輸出；它不是可保存繪圖產物。"""

        path = output_dir / STDOUT_PREVIEW_FILE
        try:
            if not path.exists():
                return None, False
            if path.is_symlink() or not path.is_file():
                raise SandboxArtifactError("sandbox stdout preview 格式無效")
            stat = path.stat()
            if stat.st_size < 1 or stat.st_size > MAX_STDOUT_PREVIEW_BYTES + 1:
                raise SandboxArtifactError("sandbox stdout preview 超過大小上限")
            payload = path.read_bytes()
        except SandboxArtifactError:
            raise
        except OSError as exc:
            raise SandboxArtifactError("sandbox stdout preview 無法讀取") from exc
        if len(payload) != stat.st_size or payload[0] not in {0, 1}:
            raise SandboxArtifactError("sandbox stdout preview 格式無效")
        truncated = payload[0] == 1
        content = payload[1:]
        if len(content) > MAX_STDOUT_PREVIEW_BYTES:
            raise SandboxArtifactError("sandbox stdout preview 超過大小上限")
        preview = content.decode("utf-8", errors="ignore")
        if not preview.strip():
            return None, truncated
        return preview, truncated

    @staticmethod
    def _ensure_inside(root: Path, candidate: Path) -> None:
        try:
            candidate.resolve(strict=False).relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise SandboxOutputError("sandbox artifact 路徑超出 output root") from exc


__all__ = [
    "DEFAULT_SANDBOX_IMAGE",
    "DockerSandboxRunner",
    "MaterializationManifest",
    "MaterializedSnapshot",
    "SandboxArtifact",
    "SandboxArtifactError",
    "SandboxOutputError",
    "SandboxCleanupError",
    "SandboxCodeError",
    "SandboxError",
    "SandboxExecutionError",
    "SandboxJob",
    "SandboxMaterializationError",
    "SandboxPolicy",
    "SandboxPolicyError",
    "SandboxResult",
    "SandboxTimeoutError",
    "SandboxUnavailableError",
    "SnapshotMaterializer",
]
