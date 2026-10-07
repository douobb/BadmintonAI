"""在隔離 sandbox 內為原始分析程式載入固定資料與分析環境。"""

from __future__ import annotations

import ast
import contextlib
import json
import os
import re
import runpy
import sys
import traceback
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

_ANALYSIS_ERROR_FILE = "_badminton_analysis_error.json"
_ANALYSIS_CODE_EXIT = 86
_STDOUT_PREVIEW_FILE = ".badminton-stdout-preview"
_STDOUT_PREVIEW_LIMIT = 4 * 1024
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


class _BoundedStdoutPreview:
    """只保留最多 4 KiB UTF-8 stdout，避免探查輸出無界佔用記憶體。"""

    def __init__(self, limit: int = _STDOUT_PREVIEW_LIMIT) -> None:
        self.limit = limit
        self._buffer = bytearray()
        self.truncated = False
        self.buffer = self

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "strict"

    def write(self, value: str | bytes) -> int:
        if isinstance(value, str):
            result = len(value)
            for character in value:
                remaining = max(0, self.limit - len(self._buffer))
                if remaining == 0:
                    self.truncated = True
                    break
                encoded = character.encode("utf-8", errors="replace")
                if len(encoded) > remaining:
                    self.truncated = True
                    break
                self._buffer.extend(encoded)
            return result
        elif isinstance(value, bytes):
            data = value
            result = len(value)
        else:
            raise TypeError("stdout 僅接受文字或位元組")
        remaining = max(0, self.limit - len(self._buffer))
        if len(data) > remaining:
            self.truncated = True
        if remaining:
            self._buffer.extend(data[:remaining])
        return result

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def getvalue(self) -> bytes:
        return bytes(self._buffer)


def _player_resolver(metadata: dict, df: pd.DataFrame):
    """以已驗證 metadata 的別名映射解析球員，不改動原始事件值。"""

    aliases = {
        str(name).strip().casefold(): str(name)
        for name in df.get("player", pd.Series(dtype=object)).dropna().unique()
    }
    if metadata.get("available"):
        for canonical, names in metadata.get("actor_aliases", {}).items():
            for name in [canonical, *names]:
                aliases[str(name).strip().casefold()] = canonical

    def resolve_player(name: str) -> str:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("球員名稱不可為空白")
        try:
            return aliases[name.strip().casefold()]
        except KeyError as exc:
            raise ValueError("未知球員別名，請先確認對象") from exc

    return resolve_player


def _error_hint(exc: Exception) -> str:
    """只回傳固定診斷代碼，避免洩露模型程式或資料內容。"""

    if isinstance(exc, TypeError) and "not JSON serializable" in str(exc):
        return "json_serialization"
    if isinstance(exc, AssertionError):
        return "assertion_failed"
    if isinstance(exc, SyntaxError):
        return "syntax_error"
    if isinstance(exc, NameError):
        return "name_error"
    if isinstance(exc, ModuleNotFoundError):
        return "missing_module"
    if isinstance(exc, FileNotFoundError):
        return "missing_file"
    if isinstance(exc, KeyError):
        return "missing_key"
    if isinstance(exc, AttributeError):
        return "missing_attribute"
    if isinstance(exc, ValueError):
        return "invalid_value"
    if isinstance(exc, TypeError):
        return "type_mismatch"
    return "python_exception"


def _static_references(source: str) -> tuple[set[str], set[str], set[str]]:
    """擷取可核對的靜態 import、df 欄位字串與屬性名稱。"""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set(), set(), set()
    modules: set[str] = set()
    columns: set[str] = set()
    attributes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Attribute):
            if _SAFE_IDENTIFIER.fullmatch(node.attr):
                attributes.add(node.attr)
        elif isinstance(node, ast.Subscript):
            base = node.value
            is_dataframe_access = isinstance(base, ast.Name) and base.id == "df"
            is_dataframe_indexer = (
                isinstance(base, ast.Attribute)
                and isinstance(base.value, ast.Name)
                and base.value.id == "df"
                and base.attr in {"loc", "iloc"}
            )
            if is_dataframe_access or is_dataframe_indexer:
                columns.update(
                    item.value
                    for item in ast.walk(node.slice)
                    if isinstance(item, ast.Constant)
                    and isinstance(item.value, str)
                    and len(item.value) <= 128
                )
    return modules, columns, attributes


def _static_references_on_line(
    source: str, line: int | None
) -> tuple[set[str], set[str], set[str]]:
    """只取錯誤行明寫的鍵、屬性與短檔名，避免回報動態資料值。"""

    if not isinstance(line, int) or line < 1:
        return set(), set(), set()
    try:
        tree = ast.parse(source)
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


def _static_loaded_names_on_line(source: str, line: int | None) -> set[str]:
    """只找錯誤行直接讀取的名稱，避免回報動態資料值。"""

    if not isinstance(line, int) or line < 1:
        return set()
    try:
        tree = ast.parse(source)
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


def _line_uses_integer_conversion(source: str, line: int | None) -> bool:
    """只辨識錯誤行是否明確呼叫內建 int，不讀取轉換值。"""

    if not isinstance(line, int) or line < 1:
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.Call)
        and node.lineno == line
        and isinstance(node.func, ast.Name)
        and node.func.id == "int"
        for node in ast.walk(tree)
    )


def _line_explicitly_raises_keyerror(source: str, line: int | None) -> bool:
    if not isinstance(line, int) or line < 1:
        return False
    try:
        tree = ast.parse(source)
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


def _line_has_dynamic_attribute_error(source: str, line: int | None) -> bool:
    if not isinstance(line, int) or line < 1:
        return False
    try:
        tree = ast.parse(source)
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


def _error_diagnostic(
    exc: Exception,
    *,
    source: str,
    source_path: str,
    columns: list[str],
    mode: str = "analysis",
) -> dict[str, object]:
    """只保存固定提示及可由來源程式靜態引用交叉核對的識別資訊。"""

    imports, _, _ = _static_references(source)
    diagnostic: dict[str, object] = {}
    line = exc.lineno if isinstance(exc, SyntaxError) else None
    offset = exc.offset if isinstance(exc, SyntaxError) else None
    if line is None:
        for frame in traceback.extract_tb(exc.__traceback__):
            if frame.filename == source_path:
                line = frame.lineno
    if isinstance(line, int) and not isinstance(line, bool) and line > 0:
        diagnostic["line"] = line
    if isinstance(offset, int) and not isinstance(offset, bool) and offset > 0:
        diagnostic["offset"] = offset
    exception_type = type(exc).__name__
    if exception_type in _SAFE_EXCEPTION_TYPES:
        diagnostic["exception_type"] = exception_type

    line_keys, line_attributes, line_filenames = _static_references_on_line(
        source, line
    )
    line_names = _static_loaded_names_on_line(source, line)

    if isinstance(exc, ModuleNotFoundError):
        name = getattr(exc, "name", None)
        if (
            isinstance(name, str)
            and len(name) <= 80
            and all(_SAFE_IDENTIFIER.fullmatch(part) for part in name.split("."))
            and any(module == name or module.startswith(name + ".") for module in imports)
        ):
            diagnostic["module"] = name
    elif (
        isinstance(exc, NameError)
        and mode == "analysis"
        and getattr(exc, "name", None) == "results_dir"
        and "results_dir" in line_names
    ):
        diagnostic["name"] = "results_dir"
    elif isinstance(exc, KeyError) and exc.args:
        raw_key = exc.args[0]
        keys: set[str] = set()
        if (
            isinstance(raw_key, str)
            and _SAFE_IDENTIFIER.fullmatch(raw_key)
            and raw_key in line_keys
        ):
            keys.add(raw_key)
        elif isinstance(raw_key, str):
            # Pandas 對多欄分組／排序可能在錯誤字串中列出多個欄名；不回傳原始本文。
            keys = {
                candidate
                for candidate in line_keys
                if re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(candidate)}(?![A-Za-z0-9_])",
                    raw_key,
                )
            }
        if not _line_explicitly_raises_keyerror(source, line):
            safe_keys = sorted(
                key for key in keys if _SAFE_IDENTIFIER.fullmatch(key)
            )[:4]
            if len(safe_keys) == 1:
                diagnostic["key"] = safe_keys[0]
            elif safe_keys:
                diagnostic["keys"] = safe_keys
            if any(key in columns for key in safe_keys):
                diagnostic["schema_column"] = True
    elif isinstance(exc, AttributeError):
        attribute = getattr(exc, "name", None)
        if (
            isinstance(attribute, str)
            and _SAFE_IDENTIFIER.fullmatch(attribute)
            and attribute in line_attributes
            and not _line_has_dynamic_attribute_error(source, line)
        ):
            diagnostic["attribute"] = attribute
            if attribute in columns:
                diagnostic["schema_column"] = True
    elif isinstance(exc, FileNotFoundError):
        filename = getattr(exc, "filename", None)
        basename = (
            Path(filename).name
            if isinstance(filename, str) and len(filename) <= 512
            else None
        )
        if isinstance(basename, str) and basename in line_filenames:
            diagnostic["filename"] = basename
    elif isinstance(exc, AssertionError):
        # 不讀取 AssertionError.args，因為其中可能含資料值或私密內容。
        pass
    elif isinstance(exc, (ValueError, TypeError)) and len(line_keys) == 1:
        diagnostic["fields"] = sorted(line_keys)[:4]
        if isinstance(exc, ValueError) and _line_uses_integer_conversion(source, line):
            diagnostic["integer_conversion"] = True
    elif isinstance(exc, (ValueError, TypeError)) and line_keys:
        diagnostic["fields"] = sorted(line_keys)[:4]
        if isinstance(exc, ValueError) and _line_uses_integer_conversion(source, line):
            diagnostic["integer_conversion"] = True
    elif isinstance(exc, ValueError) and _line_uses_integer_conversion(source, line):
        diagnostic["integer_conversion"] = True
    return diagnostic


def main() -> None:
    """依 job mode 執行完整 snapshot 分析或保存產物繪圖。"""

    manifest = json.loads(
        Path(os.environ["BADMINTON_MANIFEST_FILE"]).read_text(encoding="utf-8")
    )
    mode = manifest.get("mode", "analysis")
    columns = manifest.get("columns", [])
    source = ""
    source_path = ""
    try:
        if mode == "analysis":
            columns = manifest["columns"]
            with Path(os.environ["BADMINTON_EVENTS_FILE"]).open(
                encoding="utf-8"
            ) as events_file:
                events = [json.loads(line) for line in events_file]
            if len(events) != manifest["row_count"]:
                raise ValueError("events.jsonl 列數與 manifest 不一致")
            if any(tuple(event) != tuple(columns) for event in events):
                raise ValueError("events.jsonl 欄位與 manifest 順序不一致")
            df = pd.DataFrame(
                {
                    column: pd.Series(
                        [event[column] for event in events],
                        dtype=object,
                    )
                    for column in columns
                },
                columns=columns,
            )
            metadata = (
                json.loads(
                    Path(os.environ["BADMINTON_METADATA_FILE"]).read_text(
                        encoding="utf-8"
                    )
                )
                if "BADMINTON_METADATA_FILE" in os.environ
                else {"available": False}
            )
            resolve_player = _player_resolver(metadata, df)
            plt.rcParams["font.family"] = "sans-serif"
            plt.rcParams["font.sans-serif"] = [
                "Noto Sans CJK TC",
                "Noto Sans CJK SC",
                "Noto Sans CJK JP",
                "Noto Sans CJK KR",
                "DejaVu Sans",
            ]
            plt.rcParams["axes.unicode_minus"] = False
            namespace = {
                "pd": pd,
                "np": np,
                "plt": plt,
                "json": json,
                "os": os,
                "Path": Path,
                "df": df,
                "resolve_player": resolve_player,
            }
        elif mode == "render":
            results_dir = Path("/sandbox/input/results")
            if not results_dir.is_dir():
                raise FileNotFoundError("保存結果目錄不存在")
            namespace = {
                "pd": pd,
                "np": np,
                "json": json,
                "os": os,
                "Path": Path,
                "results_dir": results_dir,
                "output_dir": Path(os.environ["BADMINTON_OUTPUT_DIR"]),
            }
        else:
            raise ValueError("sandbox job mode 無效")
        source_path = str(Path(sys.argv[1]))
        source = Path(source_path).read_text(encoding="utf-8")
        if mode == "analysis":
            stdout_preview = _BoundedStdoutPreview()
            with contextlib.redirect_stdout(stdout_preview):
                runpy.run_path(
                    source_path,
                    init_globals=namespace,
                    run_name="__main__",
                )
            stdout_data = stdout_preview.getvalue()
            if stdout_data:
                preview_path = Path(os.environ["BADMINTON_OUTPUT_DIR"]) / _STDOUT_PREVIEW_FILE
                preview_path.write_bytes(
                    bytes((int(stdout_preview.truncated),)) + stdout_data
                )
        else:
            runpy.run_path(
                source_path,
                init_globals=namespace,
                run_name="__main__",
            )
    except Exception as exc:
        # 不輸出原始 traceback／訊息；其中可能含有資料值、機密或主機路徑。
        output = Path(os.environ["BADMINTON_OUTPUT_DIR"])
        output.mkdir(parents=True, exist_ok=True)
        (output / _ANALYSIS_ERROR_FILE).write_text(
            json.dumps(
                {
                    "version": 2,
                    "hint": _error_hint(exc),
                    "diagnostic": _error_diagnostic(
                        exc,
                        source=source if "source" in locals() else "",
                        source_path=source_path if "source_path" in locals() else "",
                        columns=columns,
                        mode=mode,
                    ),
                }
            ),
            encoding="utf-8",
        )
        raise SystemExit(_ANALYSIS_CODE_EXIT) from None


if __name__ == "__main__":
    main()
