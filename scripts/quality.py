"""以單一跨平台命令執行專案 Python 品質檢查。"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUALITY_PATHS = ("src", "tests", "scripts")
Command = tuple[str, ...]
CommandRunner = Callable[..., subprocess.CompletedProcess]


def quality_commands(python_executable: str | None = None) -> tuple[Command, ...]:
    """依序建立格式、lint、TASK-016 fixtures 及 pytest 品質命令。"""

    interpreter = python_executable or sys.executable
    paths = QUALITY_PATHS
    return (
        (interpreter, "-m", "ruff", "format", "--check", *paths),
        (interpreter, "-m", "ruff", "check", *paths),
        (interpreter, "-m", "scripts.acceptance", "--verify-fixtures"),
        (interpreter, "-m", "pytest"),
    )


def run_quality(
    *,
    commands: Sequence[Sequence[str]] | None = None,
    runner: CommandRunner = subprocess.run,
    cwd: Path = PROJECT_ROOT,
) -> int:
    """依序執行品質命令，遇到第一個失敗即回傳其 exit code。"""

    selected_commands = quality_commands() if commands is None else commands
    for command in selected_commands:
        result = runner(list(command), cwd=cwd, check=False)
        if result.returncode != 0:
            return result.returncode
    return 0


def main() -> int:
    """執行品質閘門並回傳程序狀態碼。"""

    return run_quality()


if __name__ == "__main__":
    raise SystemExit(main())
