"""設定層的最小行為測試。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from badminton_ai.settings import (
    DEFAULT_APP_ENVIRONMENT,
    DEFAULT_RUNTIME_DIR,
    DEFAULT_SOURCE_DATA_DIR,
    ENV_APP_ENVIRONMENT,
    ENV_RUNTIME_DIR,
    ENV_SOURCE_DATA_DIR,
    AppSettings,
    SettingsError,
    load_settings,
)


def test_defaults_are_normalised_against_base_dir(tmp_path: Path) -> None:
    """未提供環境變數時應使用固定預設值，且不建立任何目錄。"""

    settings = load_settings(environ={}, base_dir=tmp_path)

    assert settings == AppSettings(
        app_environment=DEFAULT_APP_ENVIRONMENT,
        source_data_dir=(tmp_path / DEFAULT_SOURCE_DATA_DIR).resolve(),
        runtime_dir=(tmp_path / DEFAULT_RUNTIME_DIR).resolve(),
    )
    assert not settings.source_data_dir.exists()
    assert not settings.runtime_dir.exists()


def test_environment_overrides_are_trimmed_and_paths_are_resolved(
    tmp_path: Path,
) -> None:
    """環境變數應覆寫預設值，並解析相對、空白與 ``..`` 路徑。"""

    environ = {
        ENV_APP_ENVIRONMENT: "  PRODUCTION ",
        ENV_SOURCE_DATA_DIR: " source/../source-data ",
        ENV_RUNTIME_DIR: "runtime/../runtime-data",
    }

    settings = load_settings(environ=environ, base_dir=tmp_path)

    assert settings.app_environment == "production"
    assert settings.app_env == "production"
    assert settings.source_data_dir == (tmp_path / "source-data").resolve()
    assert settings.runtime_dir == (tmp_path / "runtime-data").resolve()


@pytest.mark.parametrize("value", ["invalid", "", "   ", "qa"])
def test_invalid_environment_is_rejected(value: str) -> None:
    """不在允許清單內的環境名稱不得靜默接受。"""

    with pytest.raises(SettingsError, match=ENV_APP_ENVIRONMENT):
        load_settings(environ={ENV_APP_ENVIRONMENT: value})


@pytest.mark.parametrize("name", [ENV_SOURCE_DATA_DIR, ENV_RUNTIME_DIR])
def test_empty_path_is_rejected(name: str) -> None:
    """資料與 runtime 路徑不可使用空值。"""

    with pytest.raises(SettingsError, match=name):
        load_settings(environ={name: "  "})


@pytest.mark.parametrize(
    ("source_dir", "runtime_dir"),
    [
        ("shared", "shared"),
        ("data", "data/runtime"),
        ("data/source", "data"),
    ],
)
def test_source_and_runtime_overlap_is_rejected(
    tmp_path: Path,
    source_dir: str,
    runtime_dir: str,
) -> None:
    """資料與 runtime 的相同或父子路徑不得通過設定載入。"""

    environ = {
        ENV_SOURCE_DATA_DIR: source_dir,
        ENV_RUNTIME_DIR: runtime_dir,
    }

    with pytest.raises(
        SettingsError,
        match=rf"{ENV_SOURCE_DATA_DIR}.*{ENV_RUNTIME_DIR}",
    ):
        load_settings(environ=environ, base_dir=tmp_path)


def test_sibling_source_and_runtime_directories_are_accepted(
    tmp_path: Path,
) -> None:
    """互不包含的 sibling 目錄可同時作為資料與 runtime 路徑。"""

    settings = load_settings(
        environ={
            ENV_SOURCE_DATA_DIR: "source-data",
            ENV_RUNTIME_DIR: "runtime-data",
        },
        base_dir=tmp_path,
    )

    assert settings.source_data_dir == (tmp_path / "source-data").resolve()
    assert settings.runtime_dir == (tmp_path / "runtime-data").resolve()


def test_ensure_runtime_dirs_is_explicit_and_idempotent(tmp_path: Path) -> None:
    """只有明確呼叫建立方法後，runtime 目錄才會出現。"""

    settings = load_settings(environ={}, base_dir=tmp_path)
    assert not settings.runtime_dir.exists()

    created = settings.ensure_runtime_dirs()
    assert created == settings.runtime_dir
    assert settings.runtime_dir.is_dir()

    assert settings.ensure_runtime_dir() == settings.runtime_dir


def test_import_has_no_filesystem_side_effect(tmp_path: Path) -> None:
    """只 import 套件不應建立預設 runtime 或資料目錄。"""

    source_dir = Path(__file__).resolve().parents[1] / "src"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_dir)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import badminton_ai\n"
                "from pathlib import Path\n"
                "assert not Path('.runtime').exists()\n"
                "assert not Path('data').exists()\n"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".runtime").exists()
    assert not (tmp_path / "data").exists()
