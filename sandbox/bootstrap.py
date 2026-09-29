"""在隔離 sandbox 內為原始分析程式載入固定資料與分析環境。"""

from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

_ANALYSIS_ERROR_FILE = "_badminton_analysis_error.json"
_ANALYSIS_CODE_EXIT = 86


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
    if isinstance(exc, SyntaxError):
        return "syntax_error"
    if isinstance(exc, NameError):
        return "name_error"
    if isinstance(exc, ModuleNotFoundError):
        return "missing_module"
    if isinstance(exc, KeyError):
        return "missing_key"
    if isinstance(exc, AttributeError):
        return "missing_attribute"
    return "python_exception"


def main() -> None:
    """保留 JSONL 原始事件、欄序與值，並執行原始 analysis.py。"""

    manifest = json.loads(
        Path(os.environ["BADMINTON_MANIFEST_FILE"]).read_text(encoding="utf-8")
    )
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
            column: pd.Series([event[column] for event in events], dtype=object)
            for column in columns
        },
        columns=columns,
    )
    metadata = (
        json.loads(
            Path(os.environ["BADMINTON_METADATA_FILE"]).read_text(encoding="utf-8")
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

    try:
        runpy.run_path(
            sys.argv[1],
            init_globals={
                "pd": pd,
                "np": np,
                "plt": plt,
                "json": json,
                "os": os,
                "Path": Path,
                "df": df,
                "resolve_player": resolve_player,
            },
            run_name="__main__",
        )
    except Exception as exc:
        # 不輸出原始 traceback／訊息；其中可能含有資料值、機密或主機路徑。
        output = Path(os.environ["BADMINTON_OUTPUT_DIR"])
        output.mkdir(parents=True, exist_ok=True)
        (output / _ANALYSIS_ERROR_FILE).write_text(
            json.dumps({"version": 1, "hint": _error_hint(exc)}),
            encoding="utf-8",
        )
        raise SystemExit(_ANALYSIS_CODE_EXIT) from None


if __name__ == "__main__":
    main()
