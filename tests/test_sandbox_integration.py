"""單機 Docker sandbox 的少量真實 smoke 與基本邊界測試。"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

from badminton_ai.data import (
    APPROVED_SHOT_TYPES,
    MVP_REQUIRED_COLUMNS,
    DatasetSnapshot,
    MetadataSnapshot,
)
from badminton_ai.query import BadmintonQueryService
from badminton_ai.sandbox import (
    DEFAULT_SANDBOX_IMAGE,
    DockerSandboxRunner,
    SandboxArtifactError,
    SandboxCodeError,
    SandboxExecutionError,
    SandboxInputFile,
    SandboxOutputError,
    SandboxPolicy,
    SandboxTimeoutError,
    SnapshotMaterializer,
    sandbox_code_error_message,
)


def _test_sandbox_image() -> str:
    """只供 integration tests 覆寫成已建置的本機固定 tag。"""

    return os.environ.get("BADMINTON_AI_TEST_SANDBOX_IMAGE", DEFAULT_SANDBOX_IMAGE)


def _test_policy(**overrides: Any) -> SandboxPolicy:
    return SandboxPolicy(image=_test_sandbox_image(), **overrides)


def _runtime_available() -> bool:
    """確認 daemon 與 immutable image 可使用；不可用時由 fixture 略過。"""

    try:
        daemon = subprocess.run(
            ["docker", "version", "--format={{.Server.Version}}"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
        image = subprocess.run(
            ["docker", "image", "inspect", _test_sandbox_image()],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    return daemon.returncode == 0 and image.returncode == 0


@pytest.fixture(scope="module", autouse=True)
def _require_runtime() -> None:
    if not _runtime_available():
        pytest.skip("Docker daemon 或 immutable sandbox image 不可用")


def _row(rally: int, shot_type: str) -> dict[str, Any]:
    return dict(
        zip(
            MVP_REQUIRED_COLUMNS,
            [
                "00042",
                "1",
                str(rally),
                f"000{rally}",
                "1",
                "Alice",
                "Bob",
                shot_type,
                "Alice",
                "得分",
                None,
                "1",
                "2",
            ],
        )
    )


def _query(
    tmp_path: Path,
    rows: tuple[dict[str, Any], ...] | None = None,
) -> BadmintonQueryService:
    if rows is None:
        rows = tuple(
            _row(index, shot_type)
            for index, shot_type in enumerate(("發短球", "殺球", "平球"), start=1)
        )
    snapshot = DatasetSnapshot(
        columns=MVP_REQUIRED_COLUMNS,
        rows=rows,
        source=Path("integration.csv"),
    )
    actors = {"Alice": ("Alice", "A"), "Bob": ("Bob", "B")}
    alias_index = {
        alias.casefold(): canonical
        for canonical, aliases in actors.items()
        for alias in aliases
    }
    metadata = MetadataSnapshot(
        actor_aliases=actors,
        column_definitions=(),
        event_semantic_registry={"fields": ()},
        court_place="integration court",
        source_dir=tmp_path,
        alias_index=alias_index,
        shot_types=frozenset(APPROVED_SHOT_TYPES),
    )
    return BadmintonQueryService(snapshot, metadata)


def _run(
    tmp_path: Path,
    code: str,
    *,
    policy: SandboxPolicy | None = None,
    rows: tuple[dict[str, Any], ...] | None = None,
) -> Any:
    runner = DockerSandboxRunner(
        policy=policy or _test_policy(),
        materializer=SnapshotMaterializer(tmp_path),
    )
    return runner.run(_query(tmp_path, rows), code)


def _artifact_bytes(result: Any, name: str) -> bytes:
    artifact = next(item for item in result.artifacts if item.relative_path == name)
    return base64.b64decode(artifact.content_base64)


def test_real_smoke_json_and_csv(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        """
import csv, json, os
from pathlib import Path

events = [json.loads(line) for line in Path(os.environ['BADMINTON_EVENTS_FILE']).read_text().splitlines()]
output = Path(os.environ['BADMINTON_OUTPUT_DIR'])
output.joinpath('summary.json').write_text(json.dumps({'events': len(events)}))
with output.joinpath('summary.csv').open('w', newline='') as handle:
    writer = csv.writer(handle)
    writer.writerow(['events'])
    writer.writerow([len(events)])
""",
    )

    assert json.loads(_artifact_bytes(result, "summary.json")) == {"events": 3}
    assert _artifact_bytes(result, "summary.csv").splitlines() == [b"events", b"3"]
    assert not list(tmp_path.iterdir())


def test_real_analysis_artifacts_render_bar_and_heatmap_without_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = tuple(
        {**_row(index, shot_type), "landing_area": str(zone)}
        for index, (shot_type, zone) in enumerate(
            (("發短球", 1), ("殺球", 2), ("平球", 5), ("殺球", 24)),
            start=1,
        )
    )
    runner = DockerSandboxRunner(
        policy=_test_policy(timeout_seconds=60),
        materializer=SnapshotMaterializer(tmp_path),
    )
    query = _query(tmp_path, rows)
    analysis = runner.run(
        query,
        """
import json, os
from pathlib import Path

output = Path(os.environ['BADMINTON_OUTPUT_DIR'])
points = df[['type', 'landing_area']]
points.to_csv(output / 'points.csv', index=False)
(output / 'summary.json').write_text(
    json.dumps({'event_count': len(points)}), encoding='utf-8'
)
""".lstrip(),
    )
    assert json.loads(_artifact_bytes(analysis, "summary.json")) == {"event_count": 4}
    point_bytes = _artifact_bytes(analysis, "points.csv")
    assert point_bytes.decode("utf-8").splitlines()[0] == "type,landing_area"

    # Renderer 只能收到分析時明確保存的檔案；若意外重新查詢 snapshot，測試立即失敗。
    def reject_snapshot_read(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("render 不得重新查詢原始 snapshot")

    monkeypatch.setattr(query, "query_events", reject_snapshot_read)
    rendered = runner.run_render(
        (
            SandboxInputFile("points.csv", point_bytes),
            SandboxInputFile("summary.json", _artifact_bytes(analysis, "summary.json")),
        ),
        snapshot_id=analysis.manifest.snapshot_id,
        code="""
import json, os
from pathlib import Path
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd

assert 'df' not in globals()
assert 'events' not in globals()
assert 'resolve_player' not in globals()
if 'BADMINTON_EVENTS_FILE' in os.environ:
    assert not Path(os.environ['BADMINTON_EVENTS_FILE']).exists()

points = pd.read_csv(Path(results_dir) / 'points.csv')
bar_counts = points.groupby('type', sort=True).size()
bar = px.bar(x=bar_counts.index.tolist(), y=bar_counts.tolist())
zone_counts = points['landing_area'].astype(int).value_counts()
matrix = [
    [int(zone_counts.get(row * 4 + column + 1, 0)) for column in range(4)]
    for row in range(6)
]
heatmap = go.Figure(
    data=[go.Heatmap(z=matrix, x=[1, 2, 3, 4], y=[1, 2, 3, 4, 5, 6])]
)
charts = [
    {'title': '球路次數', 'figure': json.loads(bar.to_json())},
    {'title': '落點熱區', 'figure': json.loads(heatmap.to_json())},
]
Path(output_dir, 'plotly_charts.json').write_text(
    json.dumps(
        {'schema_version': 'badminton-plotly/v1', 'charts': charts},
        ensure_ascii=False,
        allow_nan=False,
    ),
    encoding='utf-8',
)
""".lstrip(),
    )

    payload = json.loads(_artifact_bytes(rendered, "plotly_charts.json"))
    assert payload["schema_version"] == "badminton-plotly/v1"
    assert [chart["title"] for chart in payload["charts"]] == ["球路次數", "落點熱區"]
    bar_chart, heatmap_chart = [chart["figure"] for chart in payload["charts"]]
    assert bar_chart["data"][0]["type"] == "bar"
    assert heatmap_chart["data"][0]["type"] == "heatmap"
    assert heatmap_chart["data"][0]["z"][0][:2] == [1, 1]
    assert not list(tmp_path.iterdir())


def test_real_missing_artifact_is_a_fixable_output_error(tmp_path: Path) -> None:
    with pytest.raises(SandboxOutputError, match="至少一個 artifact"):
        _run(tmp_path, "pass")
    assert not list(tmp_path.iterdir())


def test_real_smoke_matplotlib_png(tmp_path: Path) -> None:
    code = """
import os
from pathlib import Path
import matplotlib.font_manager as font_manager
import matplotlib.pyplot as plt

font_path = font_manager.findfont('Noto Sans CJK TC', fallback_to_default=False)
if 'NotoSansCJKTC' not in Path(font_path).name:
    raise RuntimeError('Noto Sans CJK TC font is not installed')
if plt.get_backend().lower() != 'agg':
    raise RuntimeError('Matplotlib backend must remain Agg')
plt.title('羽球分析')
plt.bar(['得分'], [3])
plt.savefig(Path(os.environ['BADMINTON_OUTPUT_DIR'], 'summary.png'))
""".lstrip()
    result = _run(
        tmp_path,
        code,
    )

    assert _artifact_bytes(result, "summary.png").startswith(b"\x89PNG\r\n\x1a\n")
    assert not list(tmp_path.iterdir())


def test_real_plotly_writes_multiple_native_figure_jsons(tmp_path: Path) -> None:
    code = """
import json
import os
from pathlib import Path
import plotly.express as px
import plotly.graph_objects as go

output = Path(os.environ['BADMINTON_OUTPUT_DIR'])
express_figure = px.bar(x=['發短球', '殺球'], y=[2, 1], title='每種球路')
object_figure = go.Figure(data=[go.Scatter(x=[1, 2], y=[3, 4], mode='lines+markers')])
object_figure.update_layout(title='每局事件')
charts = [
    {'title': '每種球路', 'figure': json.loads(express_figure.to_json())},
    {'title': '每局事件', 'figure': json.loads(object_figure.to_json())},
]
payload = {'schema_version': 'badminton-plotly/v1', 'charts': charts}
output.joinpath('plotly_charts.json').write_text(
    json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding='utf-8'
)
""".lstrip()
    result = _run(tmp_path, code)

    artifact = next(
        item for item in result.artifacts if item.relative_path == "plotly_charts.json"
    )
    payload = json.loads(_artifact_bytes(result, "plotly_charts.json"))
    assert artifact.kind == "json"
    assert [item["title"] for item in payload["charts"]] == ["每種球路", "每局事件"]
    assert all(item["figure"]["data"] for item in payload["charts"])
    assert not list(tmp_path.iterdir())


def test_real_plotly_pie_custom_data_is_column_oriented(tmp_path: Path) -> None:
    code = """
import json
import os
from pathlib import Path
import plotly.express as px

counts = df['type'].value_counts()
numerators = [int(value) for value in counts.tolist()]
denominator = int(sum(numerators))
pie = px.pie(
    names=counts.index.tolist(),
    values=numerators,
    custom_data=[numerators, [denominator] * len(numerators)],
)
pie.update_traces(
    textinfo='label+percent',
    hovertemplate=(
        '%{label}<br>次數=%{customdata[0]}'
        '<br>分母=%{customdata[1]}<extra></extra>'
    ),
)
payload = {'figure': json.loads(pie.to_json())}
Path(os.environ['BADMINTON_OUTPUT_DIR'], 'pie.json').write_text(
    json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding='utf-8'
)
""".lstrip()
    result = _run(tmp_path, code)
    payload = json.loads(_artifact_bytes(result, "pie.json"))
    trace = payload["figure"]["data"][0]

    custom_data = trace["customdata"]
    assert custom_data["shape"] == "3, 2"
    assert list(base64.b64decode(custom_data["bdata"])) == [1, 3, 1, 3, 1, 3]
    assert trace["hovertemplate"].endswith("<extra></extra>")
    assert not list(tmp_path.iterdir())


def test_real_plotly_pie_rejects_row_oriented_custom_data(tmp_path: Path) -> None:
    code = """
import plotly.express as px

counts = df['type'].value_counts()
denominator = int(counts.sum())
px.pie(
    names=counts.index.tolist(),
    values=counts.tolist(),
    custom_data=[[int(value), denominator] for value in counts],
)
""".lstrip()

    with pytest.raises(SandboxExecutionError):
        _run(tmp_path, code)
    assert not list(tmp_path.iterdir())


def test_real_bootstrap_preloads_unfiltered_canonical_dataframe(tmp_path: Path) -> None:
    rows = tuple(
        _row(index, shot_type)
        for index, shot_type in zip((3, 1, 2), ("平球", "發短球", "殺球"))
    )
    rows[1]["lose_reason"] = ""
    rows[2]["lose_reason"] = "球落界外"
    result = _run(
        tmp_path,
        """
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

names = ('pd', 'np', 'plt', 'json', 'os', 'Path', 'df')
Path(os.environ['BADMINTON_OUTPUT_DIR'], 'data.json').write_text(json.dumps({
    'preloaded': all(name in globals() for name in names),
    'columns': list(df.columns),
    'shape': list(df.shape),
    'match_ids': df['match_id'].tolist(),
    'rally_ids': df['rally_id'].tolist(),
    'nulls': df['lose_reason'].tolist(),
    'filename': sys._getframe().f_code.co_filename,
    'line': sys._getframe().f_lineno,
}))
""".lstrip(),
        policy=_test_policy(),
        rows=rows,
    )

    payload = json.loads(_artifact_bytes(result, "data.json"))
    assert payload["preloaded"] is True
    assert payload["columns"] == list(MVP_REQUIRED_COLUMNS)
    assert payload["shape"] == [3, len(MVP_REQUIRED_COLUMNS)]
    assert payload["match_ids"] == ["00042"] * 3
    assert payload["rally_ids"] == ["0003", "0001", "0002"]
    assert payload["nulls"] == [None, "", "球落界外"]
    assert payload["filename"] == "/sandbox/input/analysis.py"
    assert payload["line"] == 17
    assert not list(tmp_path.iterdir())


def test_real_non_root_and_readonly_root(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        """
import json, os
from pathlib import Path
Path(os.environ['BADMINTON_OUTPUT_DIR'], 'identity.json').write_text(json.dumps({'uid': os.getuid()}))
""",
    )
    assert json.loads(_artifact_bytes(result, "identity.json")) == {"uid": 1000}

    with pytest.raises(SandboxExecutionError):
        _run(
            tmp_path,
            "from pathlib import Path\nPath('/sandbox/forbidden.txt').write_text('x')",
        )

    with pytest.raises(SandboxExecutionError):
        _run(
            tmp_path,
            "import os\nfrom pathlib import Path\n"
            "Path(os.environ['BADMINTON_INPUT_DIR'], 'events.jsonl').write_text('x')",
        )


def test_real_network_none(tmp_path: Path) -> None:
    with pytest.raises(SandboxExecutionError):
        _run(
            tmp_path,
            "import urllib.request\nurllib.request.urlopen('http://example.com', timeout=2)",
        )


def test_real_basic_artifact_limit(tmp_path: Path) -> None:
    with pytest.raises(SandboxArtifactError):
        _run(
            tmp_path,
            "import os\nfrom pathlib import Path\nPath(os.environ['BADMINTON_OUTPUT_DIR'], 'result.txt').write_text('x')",
        )

    with pytest.raises(SandboxArtifactError):
        _run(
            tmp_path,
            "import os\nfrom pathlib import Path\nPath(os.environ['BADMINTON_OUTPUT_DIR'], 'result.json').write_text('12345')",
            policy=_test_policy(max_output_total_bytes=4),
        )


def test_real_timeout_removes_exact_container(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def recording_process(command: list[str], **kwargs: Any) -> CompletedProcess[str]:
        calls.append(command)
        return subprocess.run(command, **kwargs)

    with pytest.raises(SandboxTimeoutError):
        DockerSandboxRunner(
            policy=_test_policy(timeout_seconds=2),
            materializer=SnapshotMaterializer(tmp_path),
            process_runner=recording_process,
        ).run(
            _query(tmp_path),
            "import time\ntime.sleep(30)",
        )

    create_command = next(command for command in calls if command[1] == "create")
    container_name = create_command[create_command.index("--name") + 1]
    assert [command for command in calls if command[1] == "rm"] == [
        ["docker", "rm", "-f", container_name]
    ]
    inspection = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"name=^{container_name}$",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert inspection.returncode == 0
    assert inspection.stdout.strip() == ""
    assert not list(tmp_path.iterdir())


def test_real_sandbox_safe_module_and_dynamic_key_diagnostics(tmp_path: Path) -> None:
    with pytest.raises(SandboxCodeError) as missing_module:
        _run(tmp_path, "import scipy.stats")
    missing_module_message = sandbox_code_error_message(missing_module.value)
    assert "scipy" in missing_module_message
    assert "Traceback" not in missing_module_message
    assert "/sandbox/" not in missing_module_message

    with pytest.raises(SandboxCodeError) as dynamic_key:
        _run(tmp_path, 'raise KeyError(df["player"].iloc[0])')
    dynamic_key_message = sandbox_code_error_message(dynamic_key.value)
    assert "Alice" not in dynamic_key_message
    assert "第 1 行" in dynamic_key_message

    with pytest.raises(SandboxCodeError) as missing_static_key:
        _run(tmp_path, 'value = df["score_phase"]')
    missing_static_key_message = sandbox_code_error_message(missing_static_key.value)
    assert "來源程式靜態引用鍵：score_phase" in missing_static_key_message
    assert "第 1 行" in missing_static_key_message
    assert not list(tmp_path.iterdir())
