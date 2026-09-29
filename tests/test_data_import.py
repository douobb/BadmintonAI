"""確認資料層 import 不讀取資料或建立目錄。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_data_import_has_no_filesystem_side_effect(tmp_path: Path) -> None:
    source_dir = Path(__file__).resolve().parents[1] / "src"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_dir)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import badminton_ai.data\n"
                "from pathlib import Path\n"
                "assert not Path('data').exists()\n"
                "assert not Path('.runtime').exists()\n"
                "assert not Path('metadata').exists()\n"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
