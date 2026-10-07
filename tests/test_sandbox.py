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
    SandboxInputFile,
    SandboxJob,
    SandboxPolicy,
    SandboxPolicyError,
    SandboxResult,
    SandboxTimeoutError,
    SandboxUnavailableError,
    SnapshotMaterializer,
    sandbox_code_error_message,
)
from badminton_ai.sandbox.service import _validate_code_diagnostic


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
    input_members: list[list[str]] | None = None,
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
            if "-x" in command and input_members is not None:
                with tarfile.open(
                    fileobj=io.BytesIO(kwargs["input"]), mode="r:"
                ) as archive:
                    input_members.append(archive.getnames())
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


def test_render_materializer_contains_only_saved_files_not_snapshot_data(
    tmp_path: Path,
) -> None:
    materialized = SnapshotMaterializer(tmp_path).materialize_render(
        (SandboxInputFile("tables/points.csv", b"x,y\n1,2\n"),),
        snapshot_id="sha256:saved",
        job_id="render-1",
    )
    input_dir = Path(materialized.input_dir)
    manifest = json.loads((input_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "render"
    assert manifest["row_count"] == 0
    assert manifest["columns"] == []
    assert (
        input_dir / "results" / "tables" / "points.csv"
    ).read_bytes() == b"x,y\n1,2\n"
    assert not (input_dir / "events.jsonl").exists()
    assert not (input_dir / "metadata.json").exists()
    assert not (input_dir / "schema.json").exists()


def test_render_materializer_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(SandboxPolicyError):
        SnapshotMaterializer(tmp_path).materialize_render(
            (SandboxInputFile("../outside.csv", b"x\n1\n"),),
            snapshot_id="sha256:saved",
            job_id="render-1",
        )


def test_runner_render_uses_saved_file_archive_and_no_query_snapshot(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    input_members: list[list[str]] = []
    runner = DockerSandboxRunner(
        materializer=SnapshotMaterializer(tmp_path),
        process_runner=_fake_runner(calls, input_members=input_members),
    )
    result = runner.run_render(
        (SandboxInputFile("tables/points.csv", b"x,y\n1,2\n"),),
        snapshot_id="sha256:saved",
        code="pass",
    )

    assert result.manifest.mode == "render"
    assert input_members
    archived = input_members[0]
    assert any(name.endswith("results/tables/points.csv") for name in archived)
    assert not any(name.endswith("events.jsonl") for name in archived)
    assert not any(name.endswith("metadata.json") for name in archived)
    assert not any(name.endswith("schema.json") for name in archived)


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
        ("missing_module", "未安裝於 sandbox 映像"),
        ("missing_attribute", "不存在的屬性"),
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
    assert json.loads(envelope) == {
        "version": 2,
        "hint": "json_serialization",
        "diagnostic": {"line": 1, "exception_type": "TypeError"},
    }
    assert "SECRET" not in envelope


def test_bootstrap_omits_dynamic_error_values_from_names() -> None:
    bootstrap = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "sandbox" / "bootstrap.py")
    )
    code = 'row_value = df["player"].iloc[0]\nraise KeyError(row_value)'
    payload = bootstrap["_error_diagnostic"](
        KeyError("Alice"),
        source=code,
        source_path="analysis.py",
        columns=["player"],
    )

    assert "key" not in payload

    source = 'value = df["score_phase"]'
    try:
        exec(compile(source, "analysis.py", "exec"), {"df": {}})
    except KeyError as exc:
        missing_key = bootstrap["_error_diagnostic"](
            exc,
            source=source,
            source_path="analysis.py",
            columns=["player"],
        )
    else:
        raise AssertionError("測試程式應產生 KeyError")
    assert missing_key.get("key") == "score_phase"


def test_bootstrap_reports_only_static_error_line_diagnostics() -> None:
    bootstrap = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "sandbox" / "bootstrap.py")
    )
    source = 'value = int(paired["landing_area"])'
    try:
        exec(
            compile(source, "analysis.py", "exec"),
            {"paired": {"landing_area": "1.5"}},
        )
    except ValueError as exc:
        value_error = bootstrap["_error_diagnostic"](
            exc,
            source=source,
            source_path="analysis.py",
            columns=["player"],
        )
    else:
        raise AssertionError("測試程式應產生 ValueError")
    assert value_error == {
        "line": 1,
        "exception_type": "ValueError",
        "fields": ["landing_area"],
        "integer_conversion": True,
    }

    filename_source = 'open(results_dir / "summary-final.json")'
    try:
        exec(
            compile(filename_source, "analysis.py", "exec"),
            {"open": open, "results_dir": Path("/sandbox/input/results")},
        )
    except FileNotFoundError as exc:
        missing_file = bootstrap["_error_diagnostic"](
            exc,
            source=filename_source,
            source_path="analysis.py",
            columns=["player"],
        )
    else:
        raise AssertionError("測試程式應產生 FileNotFoundError")
    assert missing_file == {
        "line": 1,
        "exception_type": "FileNotFoundError",
        "filename": "summary-final.json",
    }

    dynamic_source = 'row_value = df["player"]\nraise KeyError(row_value)'
    try:
        exec(
            compile(dynamic_source, "analysis.py", "exec"),
            {"df": {"player": "Alice"}},
        )
    except KeyError as exc:
        dynamic_error = bootstrap["_error_diagnostic"](
            exc,
            source=dynamic_source,
            source_path="analysis.py",
            columns=["player"],
        )
    else:
        raise AssertionError("測試程式應產生 KeyError")
    assert "key" not in dynamic_error
    assert dynamic_error.get("line") == 2

    class _Column:
        iloc = ["player"]

    same_line_dynamic_source = 'raise KeyError(df["player"].iloc[0])'
    try:
        exec(
            compile(same_line_dynamic_source, "analysis.py", "exec"),
            {"df": {"player": _Column()}},
        )
    except KeyError as exc:
        same_line_dynamic_error = bootstrap["_error_diagnostic"](
            exc,
            source=same_line_dynamic_source,
            source_path="analysis.py",
            columns=["player"],
        )
    else:
        raise AssertionError("測試程式應產生 KeyError")
    assert "key" not in same_line_dynamic_error


def test_stateless_results_dir_name_error_hint_requires_static_analysis_reference() -> (
    None
):
    bootstrap = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "sandbox" / "bootstrap.py")
    )
    code = 'p = Path(results_dir) / "streak_tactics_summary.json"'
    try:
        exec(compile(code, "analysis.py", "exec"), {"Path": Path})
    except NameError as exc:
        analysis_diagnostic = bootstrap["_error_diagnostic"](
            exc,
            source=code,
            source_path="analysis.py",
            columns=["player"],
            mode="analysis",
        )
        render_diagnostic = bootstrap["_error_diagnostic"](
            exc,
            source=code,
            source_path="analysis.py",
            columns=["player"],
            mode="render",
        )
    else:
        raise AssertionError("測試程式應產生 NameError")

    assert analysis_diagnostic == {
        "line": 1,
        "exception_type": "NameError",
        "name": "results_dir",
    }
    assert "name" not in render_diagnostic
    assert "NameError('DO_NOT_LEAK')" not in json.dumps(analysis_diagnostic)

    validated = _validate_code_diagnostic(
        analysis_diagnostic,
        hint="name_error",
        code=code,
        columns=["player"],
        mode="analysis",
    )
    assert validated == {
        "line": 1,
        "exception_type": "NameError",
        "name": "results_dir",
        "stateless_results_dir": True,
    }
    rejected = _validate_code_diagnostic(
        {**analysis_diagnostic, "name": "private_value"},
        hint="name_error",
        code=code,
        columns=["player"],
        mode="analysis",
    )
    assert "name" not in rejected
    assert "stateless_results_dir" not in rejected
    assert (
        _validate_code_diagnostic(
            analysis_diagnostic,
            hint="name_error",
            code=code,
            columns=["player"],
            mode="render",
        ).get("stateless_results_dir")
        is None
    )

    message = sandbox_code_error_message(
        SandboxCodeError(
            hint="name_error",
            diagnostic=validated,
            _host_validated=True,
        )
    )
    assert "每次從乾淨環境執行" in message
    assert "renderAnalysisChart" in message
    assert "為取回數字而重畫已發布圖表" in message
    assert "streak_tactics_summary.json" not in message
    assert "analysis.py" not in message
    assert "DO_NOT_LEAK" not in message


def test_bootstrap_emits_safe_integer_conversion_and_assertion_diagnostics() -> None:
    bootstrap = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "sandbox" / "bootstrap.py")
    )
    integer_source = "converted = int(value)"
    try:
        exec(compile(integer_source, "analysis.py", "exec"), {"value": "12.0"})
    except ValueError as exc:
        integer_diagnostic = bootstrap["_error_diagnostic"](
            exc,
            source=integer_source,
            source_path="analysis.py",
            columns=["landing_area"],
        )
    else:
        raise AssertionError("測試程式應產生 ValueError")
    assert integer_diagnostic == {
        "line": 1,
        "exception_type": "ValueError",
        "integer_conversion": True,
    }
    assert "12.0" not in json.dumps(integer_diagnostic)

    assertion_source = "assert False, private_value"
    private_value = "SECRET_PLAYER_VALUE"
    try:
        exec(
            compile(assertion_source, "analysis.py", "exec"),
            {
                "private_value": private_value,
            },
        )
    except AssertionError as exc:
        assertion_diagnostic = bootstrap["_error_diagnostic"](
            exc,
            source=assertion_source,
            source_path="analysis.py",
            columns=["getpoint_player_result"],
        )
    else:
        raise AssertionError("測試程式應產生 AssertionError")
    assert bootstrap["_error_hint"](AssertionError(private_value)) == "assertion_failed"
    assert assertion_diagnostic == {
        "line": 1,
        "exception_type": "AssertionError",
    }
    assert private_value not in json.dumps(assertion_diagnostic)


def test_bootstrap_and_host_revalidate_aggregated_sort_key_diagnostics() -> None:
    bootstrap = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "sandbox" / "bootstrap.py")
    )
    import pandas as pd

    source = (
        'summary = df.groupby(["match_id", "set"], sort=False)'
        '.agg(points=("score", "sum"))\n'
        'summary = summary.sort_values(["match_id", "set", "rally"])'
    )
    namespace = {
        "pd": pd,
        "df": pd.DataFrame(
            {
                "match_id": [1],
                "set": [1],
                "rally_id": [1],
                "score": [3],
            }
        ),
    }
    try:
        exec(compile(source, "analysis.py", "exec"), namespace)
    except KeyError as exc:
        diagnostic = bootstrap["_error_diagnostic"](
            exc,
            source=source,
            source_path="analysis.py",
            columns=["match_id", "set", "rally_id", "score"],
        )
    else:
        raise AssertionError("聚合後按舊欄名排序應觸發 KeyError")

    assert diagnostic["line"] == 2
    assert diagnostic["exception_type"] == "KeyError"
    assert set(diagnostic.get("keys", [diagnostic.get("key")])) == {
        "rally",
    }
    serialized = json.dumps(diagnostic)
    assert "PRIVATE" not in serialized
    validated = _validate_code_diagnostic(
        diagnostic,
        hint="missing_key",
        code=source,
        columns=["match_id", "set", "rally_id", "score"],
    )
    message = sandbox_code_error_message(
        SandboxCodeError(
            hint="missing_key",
            diagnostic=validated,
            _host_validated=True,
        )
    )
    assert "目前物件" in message
    assert "聚合後可能與原始 df 不同" in message
    assert "來源程式靜態引用鍵" in message
    assert "rally" in message
    assert "match_id" not in message and "rally_id" not in message


def test_bootstrap_hints_merge_suffixes_for_q53_shaped_key_error() -> None:
    bootstrap = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "sandbox" / "bootstrap.py")
    )
    import pandas as pd

    source = (
        "d = df\n"
        'meta = pd.DataFrame({"match_id": [1], "rally": ["PRIVATE_META_VALUE"]})\n'
        'term = d[d["getpoint_player"].fillna("").ne("")].groupby(["match_id"], sort=False).tail(1).copy()\n'
        'term = term.merge(meta, on="match_id")\n'
        'value = term["rally"]'
    )
    namespace = {
        "pd": pd,
        "df": pd.DataFrame(
            {
                "match_id": [1],
                "getpoint_player": ["PRIVATE_PLAYER"],
                "rally": ["PRIVATE_DATA_VALUE"],
            }
        ),
    }
    try:
        exec(compile(source, "analysis.py", "exec"), namespace)
    except KeyError as exc:
        diagnostic = bootstrap["_error_diagnostic"](
            exc,
            source=source,
            source_path="analysis.py",
            columns=["match_id", "getpoint_player", "rally"],
        )
    else:
        raise AssertionError("Q53 型態的合併欄位引用應觸發 KeyError")

    assert diagnostic == {
        "line": 5,
        "exception_type": "KeyError",
        "key": "rally",
        "schema_column": True,
    }
    serialized = json.dumps(diagnostic)
    assert "PRIVATE_DATA_VALUE" not in serialized
    assert "PRIVATE_META_VALUE" not in serialized


def test_host_validates_error_fields_against_same_source_line() -> None:
    code = 'value = int(paired["landing_area"])'
    diagnostic = _validate_code_diagnostic(
        {
            "line": 1,
            "exception_type": "ValueError",
            "fields": ["landing_area"],
            "filename": "private.csv",
        },
        hint="invalid_value",
        code=code,
        columns=["player"],
    )
    assert diagnostic == {
        "line": 1,
        "exception_type": "ValueError",
        "fields": ["landing_area"],
    }
    value_message = sandbox_code_error_message(
        SandboxCodeError(
            hint="invalid_value",
            diagnostic=diagnostic,
            _host_validated=True,
        )
    )
    assert "ValueError" in value_message
    assert "landing_area" in value_message
    assert "1.5" not in value_message

    dynamic = _validate_code_diagnostic(
        {"line": 2, "exception_type": "KeyError", "key": "Alice"},
        hint="missing_key",
        code='row_value = df["player"]\nraise KeyError(row_value)',
        columns=["player"],
    )
    assert dynamic == {"line": 2, "exception_type": "KeyError"}

    same_line_dynamic = _validate_code_diagnostic(
        {"line": 1, "exception_type": "KeyError", "key": "player"},
        hint="missing_key",
        code='raise KeyError(df["player"].iloc[0])',
        columns=["player"],
    )
    assert same_line_dynamic == {"line": 1, "exception_type": "KeyError"}
    dynamic_attribute = _validate_code_diagnostic(
        {"line": 1, "exception_type": "AttributeError", "attribute": "player"},
        hint="missing_attribute",
        code="getattr(df.player, requested_attribute)",
        columns=["player"],
    )
    assert dynamic_attribute == {"line": 1, "exception_type": "AttributeError"}

    nested_key = _validate_code_diagnostic(
        {"line": 1, "exception_type": "KeyError", "key": "getpoint_player"},
        hint="missing_key",
        code='paired = frame["getpoint_player"]',
        columns=["player"],
    )
    assert nested_key == {
        "line": 1,
        "exception_type": "KeyError",
        "key": "getpoint_player",
    }

    plotly_fields = _validate_code_diagnostic(
        {
            "line": 1,
            "exception_type": "ValueError",
            "fields": ["margin", "share_pct", "private value"],
        },
        hint="invalid_value",
        code='fig = px.bar(df, x="margin", y="share_pct")',
        columns=["player"],
    )
    assert plotly_fields == {
        "line": 1,
        "exception_type": "ValueError",
        "fields": ["margin", "share_pct"],
    }

    missing_file = _validate_code_diagnostic(
        {
            "line": 1,
            "exception_type": "FileNotFoundError",
            "filename": "summary-final.json",
        },
        hint="missing_file",
        code='open(results_dir / "summary-final.json")',
        columns=["player"],
    )
    assert missing_file == {
        "line": 1,
        "exception_type": "FileNotFoundError",
        "filename": "summary-final.json",
    }
    missing_file_message = sandbox_code_error_message(
        SandboxCodeError(
            hint="missing_file",
            diagnostic=missing_file,
            _host_validated=True,
        )
    )
    assert "summary-final.json" in missing_file_message
    assert "/sandbox/" not in missing_file_message


def test_bootstrap_captures_utf8_stdout_with_a_hard_byte_limit(
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
    manifest.write_text('{"columns": ["player"], "row_count": 1}', encoding="utf-8")
    events.write_text('{"player": "Alice"}\n', encoding="utf-8")
    analysis.write_text('print("拍" * 3000)', encoding="utf-8")
    monkeypatch.setenv("BADMINTON_MANIFEST_FILE", str(manifest))
    monkeypatch.setenv("BADMINTON_EVENTS_FILE", str(events))
    monkeypatch.setenv("BADMINTON_OUTPUT_DIR", str(output))
    monkeypatch.setattr(sys, "argv", ["bootstrap.py", str(analysis)])

    bootstrap["main"]()

    from badminton_ai.sandbox.service import DockerSandboxRunner

    preview, truncated = DockerSandboxRunner._read_stdout_preview(output)
    assert preview is not None
    assert len(preview.encode("utf-8")) <= 4 * 1024
    assert truncated is True
    assert DockerSandboxRunner()._collect_artifacts(output) == ()


def test_host_limits_merge_suffix_hint_to_a_static_schema_column() -> None:
    code = (
        "d = df\n"
        'meta = pd.DataFrame({"match_id": [1], "rally": ["PRIVATE_META_VALUE"]})\n'
        'term = d[d["getpoint_player"].fillna("").ne("")].groupby(["match_id"], sort=False).tail(1).copy()\n'
        'term = term.merge(meta, on="match_id")\n'
        'value = term["rally"]'
    )
    validated = _validate_code_diagnostic(
        {
            "line": 5,
            "exception_type": "KeyError",
            "key": "rally",
            "schema_column": True,
        },
        hint="missing_key",
        code=code,
        columns=["match_id", "getpoint_player", "rally"],
    )
    assert validated == {
        "line": 5,
        "exception_type": "KeyError",
        "key": "rally",
        "schema_column": True,
    }
    message = sandbox_code_error_message(
        SandboxCodeError(
            hint="missing_key",
            diagnostic=validated,
            _host_validated=True,
        )
    )
    assert "請檢查出錯物件的欄位與型別" in message
    assert "_x／_y" in message
    assert "PRIVATE_META_VALUE" not in message
    assert "欄位不存在" not in message

    attribute_code = code.rsplit("\n", maxsplit=1)[0] + "\nvalue = term.rally"
    attribute_diagnostic = _validate_code_diagnostic(
        {
            "line": 5,
            "attribute": "rally",
            "schema_column": True,
        },
        hint="missing_attribute",
        code=attribute_code,
        columns=["match_id", "getpoint_player", "rally"],
    )
    assert attribute_diagnostic == {
        "line": 5,
        "attribute": "rally",
        "schema_column": True,
    }
    attribute_message = sandbox_code_error_message(
        SandboxCodeError(
            hint="missing_attribute",
            diagnostic=attribute_diagnostic,
            _host_validated=True,
        )
    )
    assert "請檢查出錯物件的欄位與型別" in attribute_message
    assert "_x／_y" in attribute_message

    unknown_schema_column = _validate_code_diagnostic(
        {
            "line": 5,
            "key": "rally",
            "schema_column": True,
        },
        hint="missing_key",
        code=code,
        columns=["match_id", "getpoint_player"],
    )
    assert unknown_schema_column == {"line": 5, "key": "rally"}


def test_host_revalidates_sandbox_error_diagnostic_and_never_echoes_payload() -> None:
    code = 'import scipy.stats as stats\nvalue = df["player"]\nresult = df.groupby("player")'
    columns = ["player"]

    valid = _validate_code_diagnostic(
        {
            "module": "scipy",
            "key": "player",
            "attribute": "groupby",
            "line": 2,
            "offset": 999,
            "secret": "DO_NOT_LEAK",
        },
        hint="missing_module",
        code=code,
        columns=columns,
    )
    assert valid == {"line": 2, "module": "scipy"}
    assert "DO_NOT_LEAK" not in sandbox_code_error_message(
        SandboxCodeError("host secret", hint="missing_module", diagnostic=valid)
    )

    rejected = _validate_code_diagnostic(
        {
            "module": "private_package",
            "key": "Alice",
            "attribute": "private_attribute",
            "line": 99,
        },
        hint="missing_key",
        code=code,
        columns=columns,
    )
    assert rejected == {}
    assert _validate_code_diagnostic(
        {"module": "private_package", "line": 2},
        hint="missing_module",
        code=code,
        columns=columns,
    ) == {"line": 2}
    assert _validate_code_diagnostic(
        {"attribute": "private_attribute", "line": 2},
        hint="missing_attribute",
        code=code,
        columns=columns,
    ) == {"line": 2}
    assert _validate_code_diagnostic(
        {"key": "player", "line": 2},
        hint="missing_key",
        code=code,
        columns=columns,
    ) == {"key": "player", "line": 2}
    missing_static_key = 'value = df["score_phase"]'
    assert _validate_code_diagnostic(
        {"key": "score_phase", "line": 1},
        hint="missing_key",
        code=missing_static_key,
        columns=columns,
    ) == {"key": "score_phase", "line": 1}
    missing_key_message = sandbox_code_error_message(
        SandboxCodeError(
            hint="missing_key",
            diagnostic={"key": "score_phase"},
            _host_validated=True,
        )
    )
    assert "來源程式靜態引用鍵：score_phase" in missing_key_message
    assert (
        _validate_code_diagnostic(
            {"key": "Alice"},
            hint="missing_key",
            code=missing_static_key,
            columns=columns,
        )
        == {}
    )
    assert (
        _validate_code_diagnostic(
            {"line": 99, "offset": 3},
            hint="syntax_error",
            code=code,
            columns=columns,
        )
        == {}
    )

    integer_source = "converted = int(value)"
    integer_diagnostic = _validate_code_diagnostic(
        {
            "line": 1,
            "exception_type": "ValueError",
            "integer_conversion": True,
            "private_value": "12.0",
        },
        hint="invalid_value",
        code=integer_source,
        columns=columns,
    )
    assert integer_diagnostic == {
        "line": 1,
        "exception_type": "ValueError",
        "integer_conversion": True,
    }
    integer_message = sandbox_code_error_message(
        SandboxCodeError(
            hint="invalid_value",
            diagnostic=integer_diagnostic,
            _host_validated=True,
        )
    )
    assert "pd.to_numeric(errors='coerce')" in integer_message
    assert "檢查缺值與整數性" in integer_message
    assert "12.0" not in integer_message
    assert _validate_code_diagnostic(
        {"line": 1, "integer_conversion": True},
        hint="invalid_value",
        code="value = pd.to_numeric(value)",
        columns=columns,
    ) == {"line": 1}

    assertion_diagnostic = _validate_code_diagnostic(
        {
            "line": 1,
            "exception_type": "AssertionError",
            "private_value": "SECRET_PLAYER_VALUE",
        },
        hint="assertion_failed",
        code='assert df["getpoint_player_result"].nunique() == 1',
        columns=columns,
    )
    assert assertion_diagnostic == {
        "line": 1,
        "exception_type": "AssertionError",
    }
    assertion_message = sandbox_code_error_message(
        SandboxCodeError(
            hint="assertion_failed",
            diagnostic=assertion_diagnostic,
            _host_validated=True,
        )
    )
    assert "AssertionError" in assertion_message
    assert "SECRET_PLAYER_VALUE" not in assertion_message


def test_host_code_error_reader_accepts_only_validated_module_names() -> None:
    envelope = {
        "version": 2,
        "hint": "missing_module",
        "diagnostic": {
            "module": "scipy",
            "line": 1,
            "prompt": "PRIVATE PROMPT",
            "answer": "PRIVATE ANSWER",
        },
    }

    def read_envelope(command: list[str], **kwargs: Any) -> CompletedProcess[str]:
        del kwargs
        return CompletedProcess(
            command,
            0,
            stdout=json.dumps(envelope),
            stderr="PRIVATE STDERR",
        )

    runner = DockerSandboxRunner(process_runner=read_envelope)
    error = runner._code_error(
        "sandbox-test", code="import scipy.stats", columns=["player"]
    )
    message = sandbox_code_error_message(error)
    assert "scipy" in message
    assert "PRIVATE" not in message

    error = runner._code_error("sandbox-test", code="pass", columns=["player"])
    assert "scipy" not in sandbox_code_error_message(error)


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


def test_bootstrap_render_mode_exposes_saved_files_without_original_dataframe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "sandbox" / "bootstrap.py")
    )
    results = tmp_path / "results"
    output = tmp_path / "output"
    results.mkdir()
    output.mkdir()
    (results / "points.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    analysis = tmp_path / "analysis.py"
    manifest.write_text('{"mode":"render","columns":[]}', encoding="utf-8")
    analysis.write_text(
        "assert 'df' not in globals()\n"
        "assert 'resolve_player' not in globals()\n"
        "data = pd.read_csv(results_dir / 'points.csv')\n"
        "Path(output_dir / 'plotly_charts.json').write_text(json.dumps({'rows': len(data)}))\n",
        encoding="utf-8",
    )
    real_path = Path

    def redirected_path(value: Any) -> Path:
        if str(value) == "/sandbox/input/results":
            return real_path(results)
        return real_path(value)

    bootstrap["main"].__globals__["Path"] = redirected_path
    monkeypatch.setenv("BADMINTON_MANIFEST_FILE", str(manifest))
    monkeypatch.setenv("BADMINTON_OUTPUT_DIR", str(output))
    monkeypatch.delenv("BADMINTON_EVENTS_FILE", raising=False)
    monkeypatch.delenv("BADMINTON_METADATA_FILE", raising=False)
    monkeypatch.setattr(sys, "argv", ["bootstrap.py", str(analysis)])

    bootstrap["main"]()

    assert json.loads((output / "plotly_charts.json").read_text(encoding="utf-8")) == {
        "rows": 1
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


def test_internal_stdout_preview_does_not_consume_artifact_limits(
    tmp_path: Path,
) -> None:
    runner = DockerSandboxRunner(
        policy=SandboxPolicy(max_output_files=1, max_output_total_bytes=2)
    )
    archive_path = tmp_path / "stdout-output.tar"
    output = tmp_path / "output"
    output.mkdir()
    with tarfile.open(archive_path, mode="w") as archive:
        summary = b"{}"
        summary_info = tarfile.TarInfo("./summary.json")
        summary_info.size = len(summary)
        archive.addfile(summary_info, io.BytesIO(summary))
        preview = b"\x01" + ("拍" * 1365).encode("utf-8")[:4096]
        preview_info = tarfile.TarInfo("./.badminton-stdout-preview")
        preview_info.size = len(preview)
        archive.addfile(preview_info, io.BytesIO(preview))

    runner._extract_output_archive(archive_path.read_bytes(), output)
    artifacts = runner._collect_artifacts(output)
    stdout_preview, truncated = runner._read_stdout_preview(output)

    assert [artifact.relative_path for artifact in artifacts] == ["summary.json"]
    assert stdout_preview is not None
    assert truncated is True
    assert len(stdout_preview.encode("utf-8")) <= 4 * 1024


def test_public_result_contract_is_immutable() -> None:
    manifest = MaterializationManifest(1, "fixture.csv", (), 0, "sha256:x")
    result = SandboxResult("job-1", 0, manifest, ())
    assert asdict(result)["manifest"]["analysis_file"] == "analysis.py"
    with pytest.raises(FrozenInstanceError):
        result.exit_code = 1
