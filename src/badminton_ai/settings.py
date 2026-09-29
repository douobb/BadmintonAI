"""BadmintonAI v2 的純 Python 設定層。

本模組只在呼叫 :func:`load_settings` 時讀取指定的環境變數，匯入模組
本身不會讀取設定、建立目錄或執行任何檔案系統寫入。資料目錄僅提供
唯讀語意的路徑；只有呼叫 :meth:`AppSettings.ensure_runtime_dirs` 才會
建立 runtime 目錄。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

DEFAULT_APP_ENVIRONMENT = "development"
DEFAULT_SOURCE_DATA_DIR = Path("data")
DEFAULT_RUNTIME_DIR = Path(".runtime")

ENV_APP_ENVIRONMENT = "BADMINTON_AI_APP_ENV"
ENV_SOURCE_DATA_DIR = "BADMINTON_AI_SOURCE_DATA_DIR"
ENV_RUNTIME_DIR = "BADMINTON_AI_RUNTIME_DIR"

VALID_APP_ENVIRONMENTS = frozenset({"development", "test", "staging", "production"})


class SettingsError(ValueError):
    """設定值缺失、格式錯誤或不在允許範圍時拋出的例外。"""


@dataclass(frozen=True, slots=True)
class AppSettings:
    """已驗證且正規化的應用程式設定。

    ``source_data_dir`` 表示管理者預先準備的資料來源位置，設定層不會
    嘗試建立或修改它。``runtime_dir`` 是應用程式可寫入的位置，但也必須
    透過明確呼叫 :meth:`ensure_runtime_dirs` 後才會建立。
    """

    app_environment: str
    source_data_dir: Path
    runtime_dir: Path

    @property
    def app_env(self) -> str:
        """提供較短的環境名稱別名，避免呼叫端重複轉換設定欄位。"""

        return self.app_environment

    def ensure_runtime_dirs(self) -> Path:
        """明確建立 runtime 目錄並回傳其正規化路徑。

        這是設定層唯一會建立目錄的操作；資料來源目錄永遠不會由此方法
        建立。方法可重複呼叫，適合由應用程式啟動流程顯式執行。
        """

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        return self.runtime_dir

    def ensure_runtime_dir(self) -> Path:
        """``ensure_runtime_dirs`` 的單數別名。"""

        return self.ensure_runtime_dirs()


def load_settings(
    environ: Mapping[str, str] | None = None,
    *,
    base_dir: Path | str | None = None,
) -> AppSettings:
    """從環境變數載入並驗證設定。

    Args:
        environ: 要讀取的環境變數 mapping；省略時使用 ``os.environ``。
        base_dir: 相對路徑的解析基準；省略時使用目前工作目錄。

    Returns:
        包含環境值與兩個正規化路徑的不可變設定物件。

    Raises:
        SettingsError: 環境值或路徑無法通過驗證時。

    ``environ`` 與 ``base_dir`` 參數讓測試與啟動器可以不修改全域程序狀態
    而載入設定，也避免本模組在 import 時產生任何副作用。
    """

    values = os.environ if environ is None else environ
    resolved_base_dir = _normalise_base_dir(base_dir)

    raw_environment = _read_value(
        values,
        ENV_APP_ENVIRONMENT,
        default=DEFAULT_APP_ENVIRONMENT,
    )
    app_environment = _normalise_environment(raw_environment)

    raw_source_dir = _read_value(
        values,
        ENV_SOURCE_DATA_DIR,
        default=str(DEFAULT_SOURCE_DATA_DIR),
    )
    raw_runtime_dir = _read_value(
        values,
        ENV_RUNTIME_DIR,
        default=str(DEFAULT_RUNTIME_DIR),
    )

    source_data_dir = _normalise_path(
        raw_source_dir,
        setting_name=ENV_SOURCE_DATA_DIR,
        base_dir=resolved_base_dir,
    )
    runtime_dir = _normalise_path(
        raw_runtime_dir,
        setting_name=ENV_RUNTIME_DIR,
        base_dir=resolved_base_dir,
    )
    _validate_directory_boundaries(source_data_dir, runtime_dir)

    return AppSettings(
        app_environment=app_environment,
        source_data_dir=source_data_dir,
        runtime_dir=runtime_dir,
    )


def _read_value(
    environ: Mapping[str, str],
    name: str,
    *,
    default: str,
) -> str:
    """讀取一個設定值，保留空字串讓驗證層明確拒絕它。"""

    try:
        value = environ[name]
    except KeyError:
        return default

    if not isinstance(value, str):
        raise SettingsError(f"環境變數 {name} 必須是文字值")
    return value


def _normalise_environment(value: str) -> str:
    """清理並驗證應用程式環境名稱。"""

    normalised = value.strip().lower()
    if normalised not in VALID_APP_ENVIRONMENTS:
        allowed = ", ".join(sorted(VALID_APP_ENVIRONMENTS))
        raise SettingsError(
            f"{ENV_APP_ENVIRONMENT} 必須是 {allowed} 其中之一，收到 {value!r}"
        )
    return normalised


def _normalise_base_dir(base_dir: Path | str | None) -> Path:
    """將相對路徑解析基準正規化，並將錯誤轉成設定例外。"""

    candidate = Path.cwd() if base_dir is None else Path(base_dir).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SettingsError("設定路徑的解析基準無法正規化") from exc


def _normalise_path(value: str, *, setting_name: str, base_dir: Path) -> Path:
    """依固定基準解析路徑，不建立路徑代表的目錄。"""

    cleaned = value.strip()
    if not cleaned:
        raise SettingsError(f"環境變數 {setting_name} 不可為空白")

    candidate = Path(cleaned).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate

    try:
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SettingsError(f"環境變數 {setting_name} 的路徑無法正規化") from exc


def _validate_directory_boundaries(
    source_data_dir: Path,
    runtime_dir: Path,
) -> None:
    """拒絕資料來源與 runtime 目錄相同或互相包含。"""

    if _is_same_or_descendant(source_data_dir, runtime_dir) or _is_same_or_descendant(
        runtime_dir, source_data_dir
    ):
        raise SettingsError(
            f"{ENV_SOURCE_DATA_DIR} 與 {ENV_RUNTIME_DIR} 不得相同或互為父子目錄"
        )


def _is_same_or_descendant(candidate: Path, parent: Path) -> bool:
    """以 Path 語意判斷包含關係，避免字串前綴造成誤判。"""

    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True
