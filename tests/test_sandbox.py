"""單機 Docker sandbox 的 policy、物化與 host artifact 測試。"""

from __future__ import annotations

import base64
import io
import json
import runpy
import subprocess
import sys
import tarfile
from dataclasses import FrozenInstanceError, asdict
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
    MaterializationManifest,
    SandboxArtifactError,
    SandboxCodeError,
    SandboxExecutionError,
    SandboxJob,
    SandboxPolicy,
    SandboxPolicyError,
    SandboxResult,
    SandboxTimeoutError,
    SandboxUnavailableError,
    SnapshotMaterializer,
)


def _row(index: int = 1) -> dict[str, Any]:
    return dict(
        zip(
            MVP_REQUIRED_COLUMNS,
            [
                "M1",
                "1",
                str(index),
                f"R{index}",
                "1",
                "Alice",
                "Bob",
                "發短球",
                "Alice",
                "得分",
                "",
                "1",
                "2",
            ],
        )
    )


def _query(tmp_path: Path, count: int = 2) -> BadmintonQueryService:
    snapshot = DatasetSnapshot(
        columns=MVP_REQUIRED_COLUMNS,
        rows=tuple(_row(index) for index in range(1, count + 1)),
        source=Path("fixture.csv"),
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
        court_place="fixture court",
        source_dir=tmp_path,
        alias_index=alias_index,
        shot_types=frozenset(APPROVED_SHOT_TYPES),
    )
    return BadmintonQueryService(snapshot, metadata)


def _completed(command: list[str], returncode: int = 0) -> CompletedProcess[str]:
    return CompletedProcess(command, returncode, stdout="", stderr="")


def _fake_runner(
    calls: list[tuple[list[str], dict[str, Any]]],
    *,
    output_data: bytes = b"{}",
    returncode: int = 0,
    analysis_contents: list[str] | None = None,
):
    def process(command: list[str], **kwargs: Any) -> CompletedProcess[str]:
        calls.append((command, kwargs))
        if len(command) > 1 and command[1] == "exec":
            if "-x" in command and analysis_contents is not None:
                with tarfile.open(
                    fileobj=io.BytesIO(kwargs["input"]), mode="r:"
                ) as archive:
                    analysis_contents.append(
                        archive.extractfile("analysis.py").read().decode("utf-8")
                    )
            if "-c" in command:
                stream = io.BytesIO()
                with tarfile.open(fileobj=stream, mode="w") as archive:
                    info = tarfile.TarInfo("result.json")
                    info.size = len(output_data)
                    archive.addfile(info, io.BytesIO(output_data))
                return CompletedProcess(
                    command,
                    returncode,
                    stdout=stream.getvalue(),
                    stderr=b"",
                )
            if command[-3:] == [
                "python",
                "/sandbox/bootstrap.py",
                "/sandbox/input/analysis.py",
            ]:
                if returncode == 0:
                    return _completed(command)
                return _completed(command, returncode)
        return _completed(command)

    return process


def test_materializer_writes_complete_input_and_output_dirs(tmp_path: Path) -> None:
    materializer = SnapshotMaterializer(tmp_path)
    materialized = materializer.materialize(_query(tmp_path), job_id="job-1")

    assert Path(materialized.input_dir).is_dir()
    assert Path(materialized.output_dir).is_dir()
    assert (Path(materialized.input_dir) / "events.jsonl").exists()
    assert (Path(materialized.input_dir) / "metadata.json").exists()
    assert (Path(materialized.input_dir) / "schema.json").exists()
    assert (Path(materialized.input_dir) / "manifest.json").exists()
    assert "analysis.py" in materialized.manifest.input_files
    assert materialized.manifest.row_count == 2

    materializer.cleanup(materialized)
    assert not list(tmp_path.iterdir())


def test_runner_uses_readonly_root_and_streamed_container_transport(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    analysis_contents: list[str] = []
    runner = DockerSandboxRunner(
        materializer=SnapshotMaterializer(tmp_path),
        process_runner=_fake_runner(calls, analysis_contents=analysis_contents),
    )
    result = runner.run(
        _query(tmp_path),
        "from pathlib import Path\nPath('/sandbox/output/result.json').write_text('{}')",
    )

    command = next(command for command, _ in calls if command[1] == "create")
    assert DEFAULT_SANDBOX_IMAGE in command
    assert "--network" in command and command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert command[command.index("--user") + 1] == "1000:1000"
    assert command[command.index("--memory-swap") + 1] == "536870912b"
    assert command[command.index("--pids-limit") + 1] == "64"
    assert "--mount" not in command
    tmpfs = [
        command[index + 1]
        for index, argument in enumerate(command)
        if argument == "--tmpfs"
    ]
    assert any(item.startswith("/sandbox/input:") for item in tmpfs)
    assert any(item.startswith("/sandbox/output:") for item in tmpfs)
    assert "wrapper.py" not in command
    assert "BADMINTON_MAX_ENVELOPE_BYTES" not in " ".join(command)
    analysis_command = next(
        command
        for command, _ in calls
        if command[-3:]
        == [
            "python",
            "/sandbox/bootstrap.py",
            "/sandbox/input/analysis.py",
        ]
    )
    assert analysis_command[1] == "exec"
    assert analysis_command[-2:] == [
        "/sandbox/bootstrap.py",
        "/sandbox/input/analysis.py",
    ]
    assert command[-3:] == ["tail", "-f", "/dev/null"]
    assert analysis_contents == [
        "from pathlib import Path\nPath('/sandbox/output/result.json').write_text('{}')"
    ]
    assert result.artifacts[0].relative_path == "result.json"
    assert not list(tmp_path.iterdir())


def test_artifacts_are_host_encoded_and_extension_classified(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    runner = DockerSandboxRunner(
        materializer=SnapshotMaterializer(tmp_path),
        process_runner=_fake_runner(calls, output_data=b"a,b\n1,2\n"),
    )
    result = runner.run(_query(tmp_path, count=1), "pass")

    artifact = result.artifacts[0]
    assert artifact.kind == "json"
    assert base64.b64decode(artifact.content_base64) == b"a,b\n1,2\n"
    json.dumps(asdict(result), ensure_ascii=False)
    with pytest.raises(FrozenInstanceError):
        result.artifacts = ()


def test_policy_rejects_invalid_values_but_not_svg_specific_logic() -> None:
    invalid = (
        lambda: SandboxPolicy(image="sandbox"),
        lambda: SandboxPolicy(image="sandbox:latest"),
        lambda: SandboxPolicy(container_name_prefix="Bad Name"),
        lambda: SandboxPolicy(max_output_files=0),
        lambda: SandboxJob(job_id="../escape", code="pass"),
    )
    for factory in invalid:
        with pytest.raises(SandboxPolicyError):
            factory()
    assert SandboxPolicy(
        allowed_output_extensions=(".svg",)
    ).allowed_output_extensions == (".svg",)


def test_runner_fails_closed_when_daemon_is_unavailable(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def unavailable(command: list[str], **kwargs: Any) -> CompletedProcess[str]:
        del kwargs
        calls.append(command)
        return _completed(command, returncode=1)

    runner = DockerSandboxRunner(
        materializer=SnapshotMaterializer(tmp_path),
        process_runner=unavailable,
    )
    with pytest.raises(SandboxUnavailableError):
        runner.run(_query(tmp_path), "pass")
    assert [command[1] for command in calls] == ["version"]
    assert not list(tmp_path.iterdir())


def test_execution_error_cleans_job_root(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def failed_run(command: list[str], **kwargs: Any) -> CompletedProcess[str]:
        calls.append((command, kwargs))
        if len(command) > 1 and command[1] == "exec":
            if command[-3:] == [
                "python",
                "/sandbox/bootstrap.py",
                "/sandbox/input/analysis.py",
            ]:
                return _completed(command, returncode=1)
        return _completed(command)

    runner = DockerSandboxRunner(
        materializer=SnapshotMaterializer(tmp_path),
        process_runner=failed_run,
    )
    with pytest.raises(SandboxExecutionError):
        runner.run(_query(tmp_path), "pass")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("hint", "expected_message"),
    [
        ("json_serialization", "json.loads"),
        ("missing_module", "未安裝的套件"),
        ("missing_attribute", "不存在的物件屬性"),
    ],
)
def test_analysis_code_error_returns_safe_hint_and_cleans_job(
    tmp_path: Path, hint: str, expected_message: str
) -> None:
    calls: list[list[str]] = []

    def failed_code(command: list[str], **kwargs: Any) -> CompletedProcess[str]:
        del kwargs
        calls.append(command)
        if command[-3:] == [
            "python",
            "/sandbox/bootstrap.py",
            "/sandbox/input/analysis.py",
        ]:
            return _completed(command, returncode=86)
        if command[-2] == "-c":
            return CompletedProcess(
                command,
                0,
                stdout=json.dumps({"version": 1, "hint": hint}) + "\n",
                stderr="",
            )
        return _completed(command)

    runner = DockerSandboxRunner(
        materializer=SnapshotMaterializer(tmp_path),
        process_runner=failed_code,
    )
    with pytest.raises(SandboxCodeError, match=expected_message):
        runner.run(_query(tmp_path), "pass")
    assert any(command[-2] == "-c" for command in calls)
    assert not list(tmp_path.iterdir())


def test_bootstrap_error_envelope_does_not_expose_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "sandbox" / "bootstrap.py")
    )
    manifest = tmp_path / "manifest.json"
    events = tmp_path / "events.jsonl"
    analysis = tmp_path / "analysis.py"
    output = tmp_path / "output"
    manifest.write_text('{"columns": ["player"], "row_count": 1}', encoding="utf-8")
    events.write_text('{"player": "Alice"}\n', encoding="utf-8")
    analysis.write_text(
        'raise TypeError("Object of type ndarray is not JSON serializable: SECRET")',
        encoding="utf-8",
    )
    monkeypatch.setenv("BADMINTON_MANIFEST_FILE", str(manifest))
    monkeypatch.setenv("BADMINTON_EVENTS_FILE", str(events))
    monkeypatch.setenv("BADMINTON_OUTPUT_DIR", str(output))
    monkeypatch.setattr(sys, "argv", ["bootstrap.py", str(analysis)])

    with pytest.raises(SystemExit) as exc_info:
        bootstrap["main"]()
    assert exc_info.value.code == 86
    envelope = (output / "_badminton_analysis_error.json").read_text(encoding="utf-8")
    assert json.loads(envelope) == {"version": 1, "hint": "json_serialization"}
    assert "SECRET" not in envelope


@pytest.mark.parametrize(
    ("exception", "hint"),
    [
        (ModuleNotFoundError("No module named 'private_package'"), "missing_module"),
        (
            AttributeError("'DataFrame' object has no attribute 'private_column'"),
            "missing_attribute",
        ),
    ],
)
def test_bootstrap_classifies_repairable_errors_without_leaking_details(
    exception: Exception, hint: str
) -> None:
    bootstrap = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "sandbox" / "bootstrap.py")
    )

    assert bootstrap["_error_hint"](exception) == hint


def test_bootstrap_preserves_empty_string_winner_as_raw_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "sandbox" / "bootstrap.py")
    )
    manifest = tmp_path / "manifest.json"
    events = tmp_path / "events.jsonl"
    analysis = tmp_path / "analysis.py"
    output = tmp_path / "output"
    output.mkdir()
    manifest.write_text(
        '{"columns": ["getpoint_player"], "row_count": 1}', encoding="utf-8"
    )
    events.write_text('{"getpoint_player": ""}\n', encoding="utf-8")
    analysis.write_text(
        "Path(os.environ['BADMINTON_OUTPUT_DIR'], 'result.json').write_text("
        "json.dumps({'value': df.loc[0, 'getpoint_player'], "
        "'is_missing': pd.isna(df.loc[0, 'getpoint_player'])}))",
        encoding="utf-8",
    )
    monkeypatch.setenv("BADMINTON_MANIFEST_FILE", str(manifest))
    monkeypatch.setenv("BADMINTON_EVENTS_FILE", str(events))
    monkeypatch.setenv("BADMINTON_OUTPUT_DIR", str(output))
    monkeypatch.setattr(sys, "argv", ["bootstrap.py", str(analysis)])

    bootstrap["main"]()

    assert json.loads((output / "result.json").read_text(encoding="utf-8")) == {
        "value": "",
        "is_missing": False,
    }


def test_bootstrap_resolves_player_alias_without_changing_raw_df(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "sandbox" / "bootstrap.py")
    )
    manifest = tmp_path / "manifest.json"
    events = tmp_path / "events.jsonl"
    metadata = tmp_path / "metadata.json"
    analysis = tmp_path / "analysis.py"
    output = tmp_path / "output"
    output.mkdir()
    manifest.write_text('{"columns":["player"],"row_count":1}', encoding="utf-8")
    events.write_text('{"player":"CHOU Tien Chen"}\n', encoding="utf-8")
    metadata.write_text(
        json.dumps(
            {
                "available": True,
                "actor_aliases": {"CHOU Tien Chen": ["周天成"]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    analysis.write_text(
        "Path(os.environ['BADMINTON_OUTPUT_DIR'], 'result.json').write_text("
        "json.dumps({'resolved': resolve_player('周天成'), "
        "'raw': df.loc[0, 'player']}))",
        encoding="utf-8",
    )
    monkeypatch.setenv("BADMINTON_MANIFEST_FILE", str(manifest))
    monkeypatch.setenv("BADMINTON_EVENTS_FILE", str(events))
    monkeypatch.setenv("BADMINTON_METADATA_FILE", str(metadata))
    monkeypatch.setenv("BADMINTON_OUTPUT_DIR", str(output))
    monkeypatch.setattr(sys, "argv", ["bootstrap.py", str(analysis)])

    bootstrap["main"]()

    assert json.loads((output / "result.json").read_text(encoding="utf-8")) == {
        "resolved": "CHOU Tien Chen",
        "raw": "CHOU Tien Chen",
    }


def test_timeout_removes_exact_container_and_cleans_job(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def timeout_process(command: list[str], **kwargs: Any) -> CompletedProcess[str]:
        del kwargs
        calls.append(command)
        if command[1] == "exec" and command[-3:] == [
            "python",
            "/sandbox/bootstrap.py",
            "/sandbox/input/analysis.py",
        ]:
            raise subprocess.TimeoutExpired(command, 0.05)
        return _completed(command)

    runner = DockerSandboxRunner(
        policy=SandboxPolicy(timeout_seconds=0.05),
        materializer=SnapshotMaterializer(tmp_path),
        process_runner=timeout_process,
    )
    with pytest.raises(SandboxTimeoutError):
        runner.run(_query(tmp_path), "pass")
    create_command = next(command for command in calls if command[1] == "create")
    container_name = create_command[create_command.index("--name") + 1]
    assert [command for command in calls if command[1] == "rm"] == [
        ["docker", "rm", "-f", container_name]
    ]
    assert not list(tmp_path.iterdir())


def test_artifact_basic_limits(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    policy = SandboxPolicy(max_output_total_bytes=4)
    runner = DockerSandboxRunner(
        policy=policy,
        materializer=SnapshotMaterializer(tmp_path),
        process_runner=_fake_runner(calls, output_data=b"12345"),
    )
    with pytest.raises(SandboxArtifactError):
        runner.run(_query(tmp_path, count=1), "pass")
    assert not list(tmp_path.iterdir())


def test_public_result_contract_is_immutable() -> None:
    manifest = MaterializationManifest(1, "fixture.csv", (), 0, "sha256:x")
    result = SandboxResult("job-1", 0, manifest, ())
    assert asdict(result)["manifest"]["analysis_file"] == "analysis.py"
    with pytest.raises(FrozenInstanceError):
        result.exit_code = 1
