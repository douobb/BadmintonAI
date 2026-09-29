"""品質 runner 的流程與 exit code 測試。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.quality import quality_commands, run_quality


def test_quality_commands_run_in_required_order() -> None:
    """品質命令依序檢查格式、lint、題庫 fixtures 及 pytest。"""

    commands = quality_commands(python_executable="python-test")

    assert commands[0] == (
        "python-test",
        "-m",
        "ruff",
        "format",
        "--check",
        "src",
        "tests",
        "scripts",
    )
    assert commands[1] == (
        "python-test",
        "-m",
        "ruff",
        "check",
        "src",
        "tests",
        "scripts",
    )
    assert commands[2] == (
        "python-test",
        "-m",
        "scripts.acceptance",
        "--verify-fixtures",
    )
    assert commands[3] == ("python-test", "-m", "pytest")


def test_run_quality_stops_at_first_failure_and_transfers_code(
    tmp_path: Path,
) -> None:
    """第一個失敗命令的 exit code 應原樣回傳，後續命令不得執行。"""

    commands = (("first",), ("second",), ("third",))
    calls: list[tuple[str, ...]] = []

    def fake_runner(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
    ) -> subprocess.CompletedProcess:
        calls.append(tuple(command))
        assert cwd == tmp_path
        assert check is False
        return subprocess.CompletedProcess(command, returncode=17)

    result = run_quality(commands=commands, runner=fake_runner, cwd=tmp_path)

    assert result == 17
    assert calls == [("first",)]


def test_run_quality_executes_all_commands_when_they_pass(
    tmp_path: Path,
) -> None:
    """所有命令成功時應完成整個品質流程並回傳零。"""

    commands = (("first",), ("second",), ("third",))
    calls: list[tuple[str, ...]] = []

    def fake_runner(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
    ) -> subprocess.CompletedProcess:
        calls.append(tuple(command))
        assert cwd == tmp_path
        assert check is False
        return subprocess.CompletedProcess(command, returncode=0)

    result = run_quality(commands=commands, runner=fake_runner, cwd=tmp_path)

    assert result == 0
    assert calls == [("first",), ("second",), ("third",)]
