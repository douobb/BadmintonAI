"""TASK-012 OpenAPI Tool Server 的契約與錯誤映射測試。"""

from __future__ import annotations

import base64
import json
import logging
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from badminton_ai.catalog import BadmintonCatalogService
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
    SandboxArtifact,
    SandboxCodeError,
    SandboxExecutionError,
    SandboxOutputError,
    SandboxPolicyError,
    SandboxResult,
    SandboxTimeoutError,
    SandboxUnavailableError,
)
from badminton_ai.server.app import (
    MAX_ANALYSIS_FAILURES_PER_MESSAGE,
    MAX_ANALYSIS_RUNS_PER_MESSAGE,
    MAX_ANALYSIS_TEXT_PREVIEW_BYTES,
    MAX_ANALYSIS_TEXT_PREVIEW_TOTAL_BYTES,
    create_app,
    main,
)
from badminton_ai.server.chart_display import (
    OpenWebUIChartBridge,
    OpenWebUIEventOutcomeUnknown,
)
from badminton_ai.server.composition import (
    CompositionError,
    ToolServices,
    build_services,
)
from badminton_ai.server.plotly_rich import PLOTLY_ASSET_PATH
from badminton_ai.server.result_store import AnalysisResultScope, AnalysisResultStore
from badminton_ai.settings import AppSettings


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


def _query(
    tmp_path: Path, *, rows: tuple[dict[str, Any], ...] | None = None
) -> BadmintonQueryService:
    snapshot = DatasetSnapshot(
        columns=MVP_REQUIRED_COLUMNS,
        rows=tuple(_row(index) for index in range(1, 3)) if rows is None else rows,
        source=tmp_path / "fixture.csv",
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


class _FakeSandbox:
    def __init__(
        self,
        result: SandboxResult | Exception | list[SandboxResult | Exception],
        render_result: SandboxResult
        | Exception
        | list[SandboxResult | Exception]
        | None = None,
    ) -> None:
        self.result = result
        self.results = list(result) if isinstance(result, list) else None
        self.render_result = render_result
        self.render_results = (
            list(render_result) if isinstance(render_result, list) else None
        )
        self.calls: list[tuple[BadmintonQueryService, str]] = []
        self.render_calls: list[tuple[tuple[Any, ...], str, str]] = []

    def run(self, query: BadmintonQueryService, code: str) -> SandboxResult:
        self.calls.append((query, code))
        result = self.results.pop(0) if self.results is not None else self.result
        if isinstance(result, Exception):
            raise result
        return result

    def run_render(
        self,
        files: tuple[Any, ...],
        *,
        snapshot_id: str,
        code: str,
    ) -> SandboxResult:
        self.render_calls.append((files, snapshot_id, code))
        if self.render_results is not None:
            result = self.render_results.pop(0)
        else:
            result = self.render_result
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise AssertionError("此測試未設定 render 結果")
        return result


def _result(
    content: bytes = b"{}",
    *,
    extension: str = ".json",
) -> SandboxResult:
    kind, mime_type = {
        ".csv": ("table", "text/csv"),
        ".json": ("json", "application/json"),
        ".jsonl": ("json", "application/jsonl"),
        ".png": ("chart", "image/png"),
    }[extension]
    manifest = MaterializationManifest(
        format_version=1,
        source="C:/private/fixture.csv",
        columns=MVP_REQUIRED_COLUMNS,
        row_count=2,
        snapshot_id="sha256:fixture",
        metadata_available=True,
    )
    artifact = SandboxArtifact(
        relative_path=f"summary{extension}",
        kind=kind,
        extension=extension,
        mime_type=mime_type,
        size_bytes=len(content),
        content_base64=base64.b64encode(content).decode("ascii"),
    )
    return SandboxResult("job-1", 0, manifest, (artifact,))


def _artifact(
    relative_path: str,
    content: bytes,
    *,
    extension: str,
) -> SandboxArtifact:
    kind, mime_type = {
        ".csv": ("table", "text/csv"),
        ".json": ("json", "application/json"),
        ".jsonl": ("json", "application/jsonl"),
        ".png": ("chart", "image/png"),
    }[extension]
    return SandboxArtifact(
        relative_path=relative_path,
        kind=kind,
        extension=extension,
        mime_type=mime_type,
        size_bytes=len(content),
        content_base64=base64.b64encode(content).decode("ascii"),
    )


def _result_with_artifacts(*artifacts: SandboxArtifact) -> SandboxResult:
    return SandboxResult("job-1", 0, _result().manifest, tuple(artifacts))


def _render_result(*, chart_count: int = 1) -> SandboxResult:
    charts = json.loads(_plotly_charts_bytes())
    for index in range(1, chart_count):
        charts["charts"].append(
            {
                "title": f"第 {index + 1} 張",
                "figure": {
                    "data": [{"type": "scatter", "x": [1, 2], "y": [2, 1]}],
                    "layout": {},
                },
            }
        )
    return _result_with_artifacts(
        _artifact(
            "plotly_charts.json",
            json.dumps(charts, ensure_ascii=False).encode("utf-8"),
            extension=".json",
        )
    )


def _chart_spec_bytes(
    *,
    valid: bool = True,
    inconsistent_exclusion: bool = False,
) -> bytes:
    spec: dict[str, Any] = {
        "schema_version": "badminton-chart/v1",
        "chart_type": "bar",
        "title": "常用擊球分布",
        "sample_size": 4,
        "denominator": 4,
        "excluded_count": 0,
        "measure": "count",
        "unit": "球",
        "data": {
            "categories": ["發短球", "殺球"],
            "series": [{"name": "球數", "values": [3, 1]}],
        },
    }
    if not valid:
        # 這不是支援的 schema；即使內含可執行文字也不得進入 iframe。
        spec["html"] = "<script>alert(1)</script>"
    if inconsistent_exclusion:
        spec.update(sample_size=5191, denominator=5191, excluded_count=224)
    return json.dumps(spec, ensure_ascii=False).encode("utf-8")


def _plotly_charts_bytes(*, valid: bool = True, malicious: bool = False) -> bytes:
    figure: dict[str, Any] = {
        "data": [{"type": "bar", "x": ["發短球", "殺球"], "y": [3, 1]}],
        "layout": {},
    }
    if not valid:
        figure["data"] = [{"type": "not_a_plotly_trace"}]
    if malicious:
        figure["data"][0]["x"] = ["</script><script>alert(1)</script>"]
    return json.dumps(
        {
            "schema_version": "badminton-plotly/v1",
            "charts": [{"title": "Plotly 擊球分布", "figure": figure}],
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _valid_png_bytes() -> bytes:
    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + chunk_type
            + data
            + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
        )

    return b"\x89PNG\r\n\x1a\n" + b"".join(
        (
            chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)),
            chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\xff")),
            chunk(b"IEND", b""),
        )
    )


def _client(
    tmp_path: Path,
    sandbox: Any | None = None,
    *,
    chart_bridge: Any | None = None,
    result_store: AnalysisResultStore | None = None,
) -> TestClient:
    query = _query(tmp_path)
    services = ToolServices(
        query=query,
        catalog=BadmintonCatalogService(query),
        sandbox=_FakeSandbox(_result()) if sandbox is None else sandbox,
        analysis_results=result_store,
    )
    client = TestClient(
        create_app(
            services,
            chart_bridge=chart_bridge
            or OpenWebUIChartBridge(base_url=None, api_key=None),
        )
    )
    client.headers.update(
        {
            "X-OpenWebUI-Chat-Id": "chat-1",
            "X-OpenWebUI-User-Id": "admin-1",
        }
    )
    return client


def test_openapi_has_clear_tool_operation_ids(tmp_path: Path) -> None:
    schema = _client(tmp_path).app.openapi()

    operations = {
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    assert {
        "healthCheck",
        "getDatasetSummary",
        "listColumnCatalog",
        "requestClarification",
        "listPlayerCoverage",
        "listMatchCoverage",
        "runPythonAnalysis",
        "readAnalysisResult",
        "renderAnalysisChart",
    } <= operations
    assert "/tools/analyze" in schema["paths"]
    assert "/tools/analysis-result" in schema["paths"]
    assert "/tools/render-chart" in schema["paths"]
    assert "/tools/request-clarification" in schema["paths"]
    render_description = schema["paths"]["/tools/render-chart"]["post"]["description"]
    assert (
        "embedded 與 duplicate_suppressed 都代表同訊息圖表已發布" in render_description
    )
    render_status_description = schema["components"]["schemas"][
        "RenderAnalysisResponse"
    ]["properties"]["status"]["description"]
    assert "duplicate_suppressed" in render_status_description
    assert "停止重複呼叫並交付" in render_status_description
    assert "ErrorResponse" in schema["components"]["schemas"]
    column_properties = schema["components"]["schemas"]["ColumnSummaryResponse"][
        "properties"
    ]
    assert {"null_count", "blank_count", "json_null_count"} <= set(column_properties)
    player_properties = schema["components"]["schemas"]["PlayerCoverageResponse"][
        "properties"
    ]
    assert {"won_rallies", "lost_rallies"} <= set(player_properties)
    column_description = schema["paths"]["/tools/columns"]["get"]["description"]
    column_parameters = schema["paths"]["/tools/columns"]["get"]["parameters"]
    assert any(parameter["name"] == "names" for parameter in column_parameters)
    assert "names=player,type" in column_description
    assert all(
        field in column_description
        for field in ("null_count", "blank_count", "json_null_count", "distinct_count")
    )
    analysis_description = schema["components"]["schemas"]["AnalysisRequest"][
        "properties"
    ]["code"]["description"]
    assert len(analysis_description) < 900
    for term in (
        "完整、未篩選的事件 df",
        "pd、np、plt、json、os、Path、resolve_player",
        "resolve_player('周天成')",
        "df.columns 或 BADMINTON_SCHEMA_FILE",
        "澄清前可用 Python 探查／部分分析",
        "未確認必要口徑不作結論",
        "pandas/numpy/matplotlib/plotly 已安裝",
        "SciPy 未裝",
        "BADMINTON_EVENTS_FILE",
        "BADMINTON_MANIFEST_FILE",
        "BADMINTON_METADATA_FILE",
        "BADMINTON_OUTPUT_DIR",
        "Path(os.environ['BADMINTON_OUTPUT_DIR'])",
        "正式結果須在 BADMINTON_OUTPUT_DIR 保存至少一個檔案",
        "短小 print-only 探查可回傳最多 4 KiB stdout",
        "沒有 result_id、不可繪圖",
        "只分析並保存可重用的 JSON、CSV 或 JSONL",
        "不要輸出圖表",
        "renderAnalysisChart",
    ):
        assert term in analysis_description

    for stale in ("5,191", "TASK-", "badminton-chart/v1", "custom_data"):
        assert stale not in analysis_description
    analyze_description = schema["paths"]["/tools/analyze"]["post"]["description"]
    assert "完整資料快照、metadata/schema" in analyze_description
    assert "API 程序不直接執行程式碼" in analyze_description
    clarification_description = schema["paths"]["/tools/request-clarification"]["post"][
        "description"
    ]
    assert "可先以 Python 探查或做不依賴該口徑的部分分析" in clarification_description
    assert "停止本輪分析" not in clarification_description
    for concepts in (
        ("question", "共用", "範圍", "球種", "分母"),
        ("options", "白話短句", "不同定義", "自行定義", "直接分析"),
        ("最貼題", "資料支持", "先列", "僅首項", "建議"),
        ("無合理 proxy", "限制", "明確口徑", "零樣本", "不為", "湊樣本澄清"),
    ):
        assert all(concept in clarification_description for concept in concepts)
    artifact_schema = schema["components"]["schemas"]["AnalysisFileResponse"]
    artifact_properties = artifact_schema["properties"]
    assert "content_base64" not in artifact_properties
    assert "kind" in artifact_properties
    assert "text_preview" in artifact_properties
    assert "preview_truncated" in artifact_properties
    analysis_response = schema["components"]["schemas"]["AnalysisResponse"]
    assert "result_id" in analysis_response["properties"]
    assert "result_fingerprint" in analysis_response["properties"]
    assert "analysis_runs_remaining" in analysis_response["properties"]
    assert "stdout_preview" in analysis_response["properties"]
    assert "stdout_preview_truncated" in analysis_response["properties"]
    assert "rich_ui_status" not in analysis_response["properties"]
    render_description = schema["paths"]["/tools/render-chart"]["post"]["description"]
    assert "不查詢原始資料 snapshot" in render_description
    assert "Rich UI" in render_description
    render_response = schema["components"]["schemas"]["RenderAnalysisResponse"]
    assert render_response["properties"]["status"]["enum"] == [
        "embedded",
        "duplicate_suppressed",
    ]
    json.dumps(schema, ensure_ascii=False)


def test_request_clarification_marks_waiting_without_running_analysis(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    response = client.post(
        "/tools/request-clarification",
        json={"question": "請確認長回合門檻？", "options": ["10 拍以上", "12 拍以上"]},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "awaiting_clarification",
        "question": "請確認長回合門檻？",
        "options": ["10 拍以上", "12 拍以上"],
    }
    assert (
        client.post("/tools/request-clarification", json={"question": ""}).status_code
        == 422
    )
    assert (
        client.post("/tools/request-clarification", json={"question": "  "}).status_code
        == 422
    )


def test_health_and_catalog_tools_return_explicit_success_shapes(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)

    health = client.get("/health")
    dataset = client.get("/tools/dataset-summary")
    columns = client.get("/tools/columns")
    players = client.get("/tools/players")
    matches = client.get("/tools/matches")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "data_available": True,
        "version": "0.1.0",
    }
    assert dataset.status_code == 200
    assert dataset.json()["source"] == "fixture.csv"
    assert str(tmp_path) not in dataset.text
    assert dataset.json()["row_count"] == 2
    assert columns.status_code == 200
    assert columns.json()["empty"] is False
    assert [item["name"] for item in columns.json()["columns"]] == list(
        MVP_REQUIRED_COLUMNS
    )
    winner_column = next(
        item for item in columns.json()["columns"] if item["name"] == "getpoint_player"
    )
    assert winner_column["null_count"] == 0
    assert winner_column["blank_count"] == 0
    assert winner_column["json_null_count"] == 0
    assert players.json()["players"][0]["player"] == "Alice"
    assert players.json()["players"][0]["won_rallies"] == 2
    assert players.json()["players"][0]["lost_rallies"] == 0
    assert matches.json()["matches"][0]["match_id"] == "M1"


def test_column_catalog_can_select_known_names_without_changing_default(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    full = client.get("/tools/columns")
    selected = client.get("/tools/columns", params={"names": " type, player,type "})

    assert selected.status_code == 200
    assert [item["name"] for item in selected.json()["columns"]] == [
        name for name in MVP_REQUIRED_COLUMNS if name in {"player", "type"}
    ]
    assert len(selected.content) < len(full.content)
    mixed = client.get("/tools/columns", params={"names": "player,not_a_column"})
    assert mixed.status_code == 200
    assert [item["name"] for item in mixed.json()["columns"]] == ["player"]
    assert mixed.json()["unknown_names"] == ["not_a_column"]
    assert mixed.json()["available_names"] == list(MVP_REQUIRED_COLUMNS)
    unknown = client.get("/tools/columns", params={"names": "not_a_column"})
    assert unknown.status_code == 200
    assert unknown.json()["columns"] == []
    assert unknown.json()["empty"] is True
    assert unknown.json()["available_names"] == list(MVP_REQUIRED_COLUMNS)
    assert selected.json()["unknown_names"] == []
    assert selected.json()["available_names"] == []
    for names in ("player,,type", ""):
        invalid = client.get("/tools/columns", params={"names": names})
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "invalid_input"


def test_local_versioned_plotly_asset_is_served_without_cdn(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.get(PLOTLY_ASSET_PATH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["cache-control"].endswith("immutable")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-plotly-python-version"] == "6.6.0"
    assert len(response.content) > 2 * 1024 * 1024
    assert PLOTLY_ASSET_PATH not in client.get("/openapi.json").json()["paths"]


def test_empty_catalog_is_success_with_explicit_empty_flags(tmp_path: Path) -> None:
    query = _query(tmp_path, rows=())
    services = ToolServices(
        query=query,
        catalog=BadmintonCatalogService(query),
        sandbox=_FakeSandbox(_result()),
    )
    client = TestClient(create_app(services))

    assert client.get("/tools/dataset-summary").json()["row_count"] == 0
    assert client.get("/tools/columns").json()["empty"] is False
    assert client.get("/tools/players").json() == {"players": [], "empty": True}
    assert client.get("/tools/matches").json() == {"matches": [], "empty": True}


def test_invalid_input_uses_stable_error_contract(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post("/tools/analyze", json={"code": ""})

    assert response.status_code == 422
    assert response.json() == {
        "code": "invalid_input",
        "message": "請求內容不符合工具契約",
        "details": {"fields": ["code"]},
    }


def test_unknown_tool_path_uses_same_error_contract(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/tools/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {
        "code": "not_found",
        "message": "找不到工具路徑",
        "details": None,
    }


def test_analysis_forwards_code_and_hides_host_path(tmp_path: Path) -> None:
    sandbox = _FakeSandbox(_result())
    client = _client(tmp_path, sandbox)

    response = client.post("/tools/analyze", json={"code": "print('ok')"})

    assert response.status_code == 200
    assert response.json()["snapshot_id"] == "sha256:fixture"
    assert response.json()["source"] == "fixture.csv"
    assert len(response.json()["result_id"]) == 48
    assert len(response.json()["result_fingerprint"]) == 64
    assert set(response.json()["result_fingerprint"]) <= set("0123456789abcdef")
    assert response.json()["artifacts"][0]["relative_path"] == "summary.json"
    assert str(tmp_path) not in response.text
    assert sandbox.calls[0][1] == "print('ok')"
    assert response.json()["artifacts"][0]["text_preview"] == "{}"
    assert "content_base64" not in response.text


def test_stdout_only_analysis_is_a_bounded_probe_without_renderable_result(
    tmp_path: Path,
) -> None:
    result = SandboxResult(
        "probe-1",
        0,
        _result().manifest,
        (),
        stdout_preview="欄位型別已確認",
    )
    client = _client(tmp_path, _FakeSandbox(result))

    response = client.post(
        "/tools/analyze",
        json={"code": "print('欄位型別已確認')"},
        headers=_webui_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["result_id"] is None
    assert payload["artifacts"] == []
    assert payload["stdout_preview"] == "欄位型別已確認"
    assert payload["stdout_preview_truncated"] is False
    assert list(client.app.state.analysis_result_store.results_root.iterdir()) == []
    rejected_render = client.post(
        "/tools/render-chart",
        json={"result_id": payload["result_id"], "code": "pass"},
        headers=_webui_headers(),
    )
    assert rejected_render.status_code == 422


def test_artifact_result_does_not_duplicate_stdout_preview(tmp_path: Path) -> None:
    result = SandboxResult(
        "job-1",
        0,
        _result().manifest,
        _result().artifacts,
        stdout_preview="same summary should not be duplicated",
        stdout_preview_truncated=True,
    )
    response = _client(tmp_path, _FakeSandbox(result)).post(
        "/tools/analyze", json={"code": "print('same summary')"}
    )

    assert response.status_code == 200
    assert response.json()["stdout_preview"] is None
    assert response.json()["stdout_preview_truncated"] is False


def test_saved_result_fingerprint_is_content_based_and_probe_has_none(
    tmp_path: Path,
) -> None:
    content = b'{"metric":294,"count":248}'
    sandbox = _FakeSandbox(
        [
            _result_with_artifacts(
                _artifact("summary-a.json", content, extension=".json")
            ),
            _result_with_artifacts(
                _artifact("different-name.json", content, extension=".json")
            ),
            _result_with_artifacts(
                _artifact(
                    "different-name.json",
                    b'{"metric":294,"count":247}',
                    extension=".json",
                )
            ),
            SandboxResult(
                "probe-1", 0, _result().manifest, (), stdout_preview="探查完成"
            ),
        ]
    )
    client = _client(tmp_path, sandbox)
    headers = _webui_headers()

    responses = [
        client.post(
            "/tools/analyze", json={"code": f"pass  # {index}"}, headers=headers
        )
        for index in range(4)
    ]
    assert all(response.status_code == 200 for response in responses)
    payloads = [response.json() for response in responses]
    assert payloads[0]["result_fingerprint"] == payloads[1]["result_fingerprint"]
    assert payloads[0]["result_id"] != payloads[1]["result_id"]
    assert payloads[0]["result_fingerprint"] != payloads[2]["result_fingerprint"]
    assert payloads[3]["result_id"] is None
    assert payloads[3]["result_fingerprint"] is None


def test_api_caps_stdout_preview_on_a_utf8_boundary(tmp_path: Path) -> None:
    result = SandboxResult(
        "probe-large",
        0,
        _result().manifest,
        (),
        stdout_preview="拍" * 3000,
    )
    response = _client(tmp_path, _FakeSandbox(result)).post(
        "/tools/analyze", json={"code": "print('large probe')"}
    )

    assert response.status_code == 200
    payload = response.json()
    preview = payload["stdout_preview"]
    assert len(preview.encode("utf-8")) <= 4 * 1024
    assert payload["stdout_preview_truncated"] is True
    assert preview.encode("utf-8").decode("utf-8") == preview


def test_stdout_probe_does_not_reset_analysis_repair_failure_count(
    tmp_path: Path,
) -> None:
    probe = SandboxResult(
        "probe-1",
        0,
        _result().manifest,
        (),
        stdout_preview="欄位已探查",
    )
    sandbox = _FakeSandbox(
        [
            SandboxCodeError("分析程式有未定義變數"),
            probe,
            SandboxCodeError("分析程式有未定義變數"),
            SandboxCodeError("分析程式有未定義變數"),
            SandboxCodeError("分析程式有未定義變數"),
        ]
    )
    client = _client(tmp_path, sandbox)

    first_failure = client.post(
        "/tools/analyze", json={"code": "broken"}, headers=_webui_headers()
    )
    probe_response = client.post(
        "/tools/analyze", json={"code": "print('probe')"}, headers=_webui_headers()
    )
    remaining_failures = [
        client.post(
            "/tools/analyze", json={"code": "broken again"}, headers=_webui_headers()
        )
        for _ in range(3)
    ]
    terminal = client.post(
        "/tools/analyze", json={"code": "must not run"}, headers=_webui_headers()
    )

    assert first_failure.json()["code"] == "analysis_code_error"
    assert probe_response.status_code == 200
    assert probe_response.json()["analysis_runs_remaining"] == (
        MAX_ANALYSIS_RUNS_PER_MESSAGE - 2
    ), "失敗嘗試與 stdout 探查都消耗實際 server 額度"
    assert all(
        response.json()["code"] == "analysis_code_error"
        for response in remaining_failures
    )
    assert terminal.status_code == 429
    assert terminal.json()["code"] == "analysis_retry_limit"
    assert len(sandbox.calls) == 5


@pytest.mark.parametrize(
    ("extension", "content"),
    [
        (".json", '{"type":"發短球"}'.encode("utf-8")),
        (".jsonl", '{"type":"發短球"}\n'.encode("utf-8")),
        (".csv", "type,count\n發短球,2\n".encode("utf-8")),
    ],
)
def test_analysis_previews_small_json_csv_and_jsonl_without_name_convention(
    tmp_path: Path,
    extension: str,
    content: bytes,
) -> None:
    filename = {".json": "probe.json", ".jsonl": "probe.jsonl", ".csv": "probe.csv"}[
        extension
    ]
    client = _client(
        tmp_path,
        _FakeSandbox(
            _result_with_artifacts(_artifact(filename, content, extension=extension))
        ),
    )
    response = client.post("/tools/analyze", json={"code": "pass"})

    assert response.status_code == 200
    artifact = response.json()["artifacts"][0]
    assert artifact["text_preview"] == content.decode("utf-8")
    assert artifact["preview_truncated"] is False
    assert "content_base64" not in response.text


def test_analysis_artifact_previews_share_one_total_byte_budget(
    tmp_path: Path,
) -> None:
    client = _client(
        tmp_path,
        _FakeSandbox(
            _result_with_artifacts(
                _artifact("one.csv", b"A" * 2048, extension=".csv"),
                _artifact("two.jsonl", b"B" * 2048, extension=".jsonl"),
                _artifact("three.json", b"C", extension=".json"),
            )
        ),
    )

    response = client.post("/tools/analyze", json={"code": "pass"})

    assert response.status_code == 200
    artifacts = response.json()["artifacts"]
    previews = [artifact["text_preview"] for artifact in artifacts]
    assert previews == ["A" * 2048, "B" * 2047, "C"]
    assert [artifact["preview_truncated"] for artifact in artifacts] == [
        False,
        True,
        False,
    ]
    assert sum(len((preview or "").encode("utf-8")) for preview in previews) == (
        MAX_ANALYSIS_TEXT_PREVIEW_TOTAL_BYTES
    )


def test_small_summary_keeps_preview_when_large_data_file_comes_first(
    tmp_path: Path,
) -> None:
    summary = b'{"total_points":42}'
    client = _client(
        tmp_path,
        _FakeSandbox(
            _result_with_artifacts(
                _artifact("data.csv", b"D" * 4096, extension=".csv"),
                _artifact("summary.json", summary, extension=".json"),
            )
        ),
    )

    response = client.post("/tools/analyze", json={"code": "pass"})

    assert response.status_code == 200
    artifacts = response.json()["artifacts"]
    assert [artifact["relative_path"] for artifact in artifacts] == [
        "data.csv",
        "summary.json",
    ]
    assert artifacts[0]["text_preview"].startswith("D")
    assert artifacts[0]["preview_truncated"] is True
    assert artifacts[1]["text_preview"] == summary.decode("utf-8")
    assert artifacts[1]["preview_truncated"] is False
    assert (
        sum(
            len((artifact["text_preview"] or "").encode("utf-8"))
            for artifact in artifacts
        )
        <= MAX_ANALYSIS_TEXT_PREVIEW_TOTAL_BYTES
    )


def test_read_analysis_result_uses_scoped_saved_preview_without_running_analysis(
    tmp_path: Path,
) -> None:
    summary = b'{"total_points":42}'
    large_data = "拍" * 3000
    saved_result = _result_with_artifacts(
        _artifact("data.csv", large_data.encode("utf-8"), extension=".csv"),
        _artifact("summary.json", summary, extension=".json"),
    )
    sandbox = _FakeSandbox([saved_result, SandboxCodeError("analysis failure")])
    store = AnalysisResultStore(tmp_path / "stored")
    client = _client(tmp_path, sandbox, result_store=store)
    headers = _webui_headers()

    created = client.post(
        "/tools/analyze", json={"code": "save results"}, headers=headers
    )
    result_id = created.json()["result_id"]
    failed = client.post("/tools/analyze", json={"code": "fail once"}, headers=headers)
    assert failed.status_code == 422
    runs_before_read = dict(client.app.state.analysis_runs)
    failures_before_read = dict(client.app.state.analysis_failures)

    response = client.get(
        "/tools/analysis-result",
        params={"result_id": result_id},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["result_id"] == result_id
    assert [item["relative_path"] for item in payload["artifacts"]] == [
        "data.csv",
        "summary.json",
    ]
    data_preview, summary_preview = payload["artifacts"]
    assert data_preview["text_preview"].startswith("拍")
    assert data_preview["preview_truncated"] is True
    assert (
        len(data_preview["text_preview"].encode("utf-8"))
        <= MAX_ANALYSIS_TEXT_PREVIEW_BYTES
    )
    assert "�" not in data_preview["text_preview"]
    assert summary_preview["text_preview"] == summary.decode("utf-8")
    assert summary_preview["preview_truncated"] is False
    assert (
        sum(
            len((item["text_preview"] or "").encode("utf-8"))
            for item in payload["artifacts"]
        )
        <= MAX_ANALYSIS_TEXT_PREVIEW_TOTAL_BYTES
    )
    assert "content_base64" not in response.text
    assert len(sandbox.calls) == 2
    assert dict(client.app.state.analysis_runs) == runs_before_read
    assert dict(client.app.state.analysis_failures) == failures_before_read

    missing_result = client.get(
        "/tools/analysis-result",
        params={"result_id": "f" * 48},
        headers=headers,
    )
    assert missing_result.status_code == 404
    assert missing_result.json()["code"] == "analysis_result_not_found"

    selected = client.get(
        "/tools/analysis-result",
        params={"result_id": result_id, "relative_path": "summary.json"},
        headers=headers,
    )
    assert selected.status_code == 200
    assert [item["relative_path"] for item in selected.json()["artifacts"]] == [
        "summary.json"
    ]

    traversal = client.get(
        "/tools/analysis-result",
        params={"result_id": result_id, "relative_path": "../summary.json"},
        headers=headers,
    )
    assert traversal.status_code == 404
    other_user = client.get(
        "/tools/analysis-result",
        params={"result_id": result_id},
        headers={**headers, "X-OpenWebUI-User-Id": "other-user"},
    )
    assert other_user.status_code == 404
    other_chat = client.get(
        "/tools/analysis-result",
        params={"result_id": result_id},
        headers={**headers, "X-OpenWebUI-Chat-Id": "other-chat"},
    )
    assert other_chat.status_code == 404
    invalid_id = client.get(
        "/tools/analysis-result",
        params={"result_id": "../manifest.json"},
        headers=headers,
    )
    assert invalid_id.status_code == 422


@pytest.mark.parametrize(
    "text",
    ["small", "A" * 10000, "拍" * 4000, "A" * 4095 + "中🏸" * 1800],
    ids=["small", "ascii", "cjk", "mixed-boundary"],
)
def test_read_analysis_result_segments_reassemble_utf8(
    tmp_path: Path, text: str
) -> None:
    store = AnalysisResultStore(tmp_path / "stored")
    saved = store.save(
        scope=AnalysisResultScope(user_id="admin-1", chat_id="chat-1"),
        snapshot_id="sha256:fixture",
        artifacts=(_artifact("data.csv", text.encode("utf-8"), extension=".csv"),),
    )
    client = _client(tmp_path, result_store=store)
    offset, segments = 0, []
    while True:
        response = client.get(
            "/tools/analysis-result",
            params={
                "result_id": saved.result_id,
                "relative_path": "data.csv",
                "offset_bytes": offset,
            },
            headers=_webui_headers(),
        )
        assert response.status_code == 200
        item = response.json()["artifacts"][0]
        segment = item["text_preview"]
        assert item["preview_offset_bytes"] == offset
        assert len(segment.encode("utf-8")) <= 4096
        assert item["next_offset_bytes"] == offset + len(segment.encode("utf-8"))
        segments.append(segment)
        offset = item["next_offset_bytes"]
        assert offset <= len(text.encode("utf-8"))
        if not item["has_more"]:
            break
        assert segment
    assert "".join(segments) == text
    eof = client.get(
        "/tools/analysis-result",
        params={
            "result_id": saved.result_id,
            "relative_path": "data.csv",
            "offset_bytes": offset,
        },
        headers=_webui_headers(),
    ).json()["artifacts"][0]
    assert eof["text_preview"] == "" and eof["has_more"] is False


@pytest.mark.parametrize(
    "offset,status",
    [("-1", 422), ("1.5", 422), ("1.0", 422), ("true", 422), ("1", 400), ("99", 400)],
)
def test_read_analysis_result_rejects_invalid_byte_offsets(
    tmp_path: Path, offset: str, status: int
) -> None:
    store = AnalysisResultStore(tmp_path / "stored")
    saved = store.save(
        scope=AnalysisResultScope(user_id="admin-1", chat_id="chat-1"),
        snapshot_id="sha256:fixture",
        artifacts=(_artifact("data.csv", "中🏸".encode("utf-8"), extension=".csv"),),
    )
    client = _client(tmp_path, result_store=store)
    response = client.get(
        "/tools/analysis-result",
        params={
            "result_id": saved.result_id,
            "relative_path": "data.csv",
            "offset_bytes": offset,
        },
        headers=_webui_headers(),
    )
    assert response.status_code == status
    assert str(tmp_path) not in response.text
    missing_path = client.get(
        "/tools/analysis-result",
        params={
            "result_id": saved.result_id,
            "offset_bytes": 0,
        },
        headers=_webui_headers(),
    )
    assert missing_path.status_code == 400


def test_read_analysis_result_rejects_invalid_utf8(tmp_path: Path) -> None:
    store = AnalysisResultStore(tmp_path / "stored")
    saved = store.save(
        scope=AnalysisResultScope(user_id="admin-1", chat_id="chat-1"),
        snapshot_id="sha256:fixture",
        artifacts=(_artifact("data.csv", b"valid\xff", extension=".csv"),),
    )
    response = _client(tmp_path, result_store=store).get(
        "/tools/analysis-result",
        params={
            "result_id": saved.result_id,
            "relative_path": "data.csv",
        },
        headers=_webui_headers(),
    )
    assert response.status_code == 400
    assert str(tmp_path) not in response.text


def test_read_analysis_result_segment_preserves_scope_and_path_checks(
    tmp_path: Path,
) -> None:
    now = [100.0]
    store = AnalysisResultStore(
        tmp_path / "stored", ttl_seconds=10, clock=lambda: now[0]
    )
    saved = store.save(
        scope=AnalysisResultScope(user_id="admin-1", chat_id="chat-1"),
        snapshot_id="sha256:fixture",
        artifacts=(_artifact("data.csv", b"abc", extension=".csv"),),
    )
    client = _client(tmp_path, result_store=store)
    params = {
        "result_id": saved.result_id,
        "relative_path": "data.csv",
        "offset_bytes": "1",
    }
    for field in ("X-OpenWebUI-User-Id", "X-OpenWebUI-Chat-Id"):
        response = client.get(
            "/tools/analysis-result",
            params=params,
            headers={**_webui_headers(), field: "other"},
        )
        assert response.status_code == 404
    for path in ("../data.csv", "/data.csv", "data.csv\x00", "missing.csv", "*.csv"):
        response = client.get(
            "/tools/analysis-result",
            params={**params, "relative_path": path},
            headers=_webui_headers(),
        )
        assert response.status_code == 404
        assert str(tmp_path) not in response.text
    now[0] = 111.0
    response = client.get(
        "/tools/analysis-result", params=params, headers=_webui_headers()
    )
    assert response.status_code == 410


def test_read_analysis_result_observes_store_ttl(tmp_path: Path) -> None:
    now = [100.0]
    store = AnalysisResultStore(
        tmp_path / "stored", ttl_seconds=10, clock=lambda: now[0]
    )
    saved = store.save(
        scope=AnalysisResultScope(user_id="admin-1", chat_id="chat-1"),
        snapshot_id="sha256:fixture",
        artifacts=(_artifact("summary.json", b'{"value":1}', extension=".json"),),
    )
    client = _client(tmp_path, result_store=store)
    now[0] = 111.0

    response = client.get(
        "/tools/analysis-result",
        params={"result_id": saved.result_id},
        headers=_webui_headers(),
    )

    assert response.status_code == 410
    assert response.json()["code"] == "analysis_result_expired"


def test_analysis_text_preview_is_bounded_at_utf8_boundary(
    tmp_path: Path,
) -> None:
    header = '{"value":"'
    content = (
        header.encode("utf-8")
        + b"A" * (MAX_ANALYSIS_TEXT_PREVIEW_BYTES - len(header.encode("utf-8")) - 1)
        + "中".encode("utf-8")
        + b'"}'
    )
    store = AnalysisResultStore(tmp_path / "stored")
    client = _client(
        tmp_path,
        _FakeSandbox(
            _result_with_artifacts(_artifact("probe.json", content, extension=".json"))
        ),
        result_store=store,
    )
    response = client.post("/tools/analyze", json={"code": "pass"})

    assert response.status_code == 200
    artifact = response.json()["artifacts"][0]
    preview = artifact["text_preview"]
    assert artifact["preview_truncated"] is True
    assert len(preview.encode("utf-8")) <= MAX_ANALYSIS_TEXT_PREVIEW_BYTES
    assert preview == header + "A" * (
        MAX_ANALYSIS_TEXT_PREVIEW_BYTES - len(header.encode("utf-8")) - 1
    )
    assert "�" not in preview
    assert "content_base64" not in response.text
    stored = store.get(
        response.json()["result_id"],
        scope=AnalysisResultScope("admin-1", "chat-1"),
    )
    assert stored.files[0].content == content


def test_analysis_never_calls_chart_bridge(tmp_path: Path) -> None:
    class _ForbiddenBridge:
        configured = True

        def emit_chart_embed(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("分析工具不可直接嵌圖")

    response = _client(
        tmp_path,
        _FakeSandbox(
            _result_with_artifacts(_artifact("probe.json", b"{}", extension=".json"))
        ),
        chart_bridge=_ForbiddenBridge(),
    ).post("/tools/analyze", json={"code": "pass"})

    assert response.status_code == 200
    artifact = response.json()["artifacts"][0]
    assert artifact["text_preview"] == "{}"


def _webui_headers(message_id: str = "message-1") -> dict[str, str]:
    return {
        "X-OpenWebUI-Chat-Id": "chat-1",
        "X-OpenWebUI-Message-Id": message_id,
        "X-OpenWebUI-User-Id": "admin-1",
    }


def test_new_analysis_rejects_png_and_chart_spec_without_attachment(
    tmp_path: Path,
) -> None:
    class _Bridge:
        configured = True

        def emit_chart_embed(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("分析工具不可直接嵌入圖表")

    result = _result_with_artifacts(
        _artifact("chart_spec.json", _chart_spec_bytes(), extension=".json"),
        _artifact("summary.json", b'{"count":4}', extension=".json"),
        _artifact("summary.png", _valid_png_bytes(), extension=".png"),
    )
    response = _client(tmp_path, _FakeSandbox(result), chart_bridge=_Bridge()).post(
        "/tools/analyze", json={"code": "pass"}, headers=_webui_headers()
    )
    assert response.status_code == 200
    assert [a["relative_path"] for a in response.json()["artifacts"]] == [
        "summary.json"
    ]
    assert "summary.png" not in response.text


def test_renderer_embeds_bundle_once_and_new_message_can_redraw(
    tmp_path: Path,
) -> None:
    class _Bridge:
        configured = True

        def __init__(self) -> None:
            self.embeds: list[str] = []

        def emit_chart_embed(self, html_content: str, **_kwargs: Any) -> str:
            self.embeds.append(html_content)
            return "embedded"

    result = _result_with_artifacts(
        _artifact("points.csv", b"x,y\n1,2\n", extension=".csv"),
        _artifact("chart_spec.json", _chart_spec_bytes(), extension=".json"),
        _artifact("summary.json", b'{"count":4}', extension=".json"),
        _artifact("summary.png", _valid_png_bytes(), extension=".png"),
    )
    sandbox = _FakeSandbox(
        result,
        render_result=[_render_result(chart_count=2), _render_result()],
    )
    bridge = _Bridge()
    client = _client(tmp_path, sandbox, chart_bridge=bridge)
    response = client.post(
        "/tools/analyze", json={"code": "pass"}, headers=_webui_headers()
    )
    assert response.status_code == 200
    assert [a["relative_path"] for a in response.json()["artifacts"]] == [
        "points.csv",
        "summary.json",
    ]
    assert bridge.embeds == []
    result_id = response.json()["result_id"]
    rendered = client.post(
        "/tools/render-chart",
        json={"result_id": result_id, "code": "pass"},
        headers=_webui_headers(),
    )
    assert rendered.status_code == 200
    assert rendered.json() == {
        "status": "embedded",
        "result_id": result_id,
        "chart_count": 2,
    }
    assert len(bridge.embeds) == 1
    assert bridge.embeds[0].count('class="plotly-figure" data-chart-index=') == 2
    assert (
        '<script src="/badmintonai/assets/plotly-6.6.0.min.js"></script>'
        in (bridge.embeds[0])
    )
    assert "127.0.0.1:8000" not in bridge.embeds[0]
    duplicate = client.post(
        "/tools/render-chart",
        json={"result_id": result_id, "code": "raise AssertionError()"},
        headers=_webui_headers(),
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate_suppressed"
    assert len(sandbox.render_calls) == 1
    reanalysis = client.post(
        "/tools/analyze", json={"code": "another statistic"}, headers=_webui_headers()
    )
    assert reanalysis.status_code == 200
    assert reanalysis.json()["result_id"] != result_id
    assert len(sandbox.calls) == 2
    duplicate_after_reanalysis = client.post(
        "/tools/render-chart",
        json={
            "result_id": reanalysis.json()["result_id"],
            "code": "redraw with new data",
        },
        headers=_webui_headers(),
    )
    assert duplicate_after_reanalysis.status_code == 200
    assert duplicate_after_reanalysis.json() == {
        "status": "duplicate_suppressed",
        "result_id": result_id,
        "chart_count": 2,
    }
    assert len(sandbox.render_calls) == 1
    assert len(bridge.embeds) == 1
    redrawn = client.post(
        "/tools/render-chart",
        json={"result_id": result_id, "code": "pass"},
        headers=_webui_headers("message-2"),
    )
    assert redrawn.status_code == 200
    assert redrawn.json()["status"] == "embedded"
    assert len(sandbox.render_calls) == 2
    assert sandbox.render_calls[0][0][0].relative_path == "points.csv"
    assert sandbox.render_calls[0][1] == "sha256:fixture"
    assert sandbox.render_calls[0][2] == "pass"
    assert len(sandbox.calls) == 2


def test_renderer_repair_reuses_saved_result_without_rerunning_analysis(
    tmp_path: Path,
) -> None:
    class _Bridge:
        configured = True

        def emit_chart_embed(self, *_args: Any, **_kwargs: Any) -> str:
            return "embedded"

    analysis = _result_with_artifacts(
        _artifact("points.csv", b"x,y\n1,2\n", extension=".csv"),
        _artifact("summary.json", b'{"count":1}', extension=".json"),
    )
    sandbox = _FakeSandbox(
        analysis,
        render_result=[SandboxCodeError("PRIVATE DATA VALUE"), _render_result()],
    )
    client = _client(tmp_path, sandbox, chart_bridge=_Bridge())
    analyzed = client.post(
        "/tools/analyze", json={"code": "analysis"}, headers=_webui_headers()
    )
    result_id = analyzed.json()["result_id"]
    failed = client.post(
        "/tools/render-chart",
        json={"result_id": result_id, "code": "broken renderer"},
        headers=_webui_headers(),
    )
    assert failed.status_code == 422
    assert failed.json()["code"] == "render_code_error"
    assert "PRIVATE DATA VALUE" not in failed.text

    repaired = client.post(
        "/tools/render-chart",
        json={"result_id": result_id, "code": "corrected renderer"},
        headers=_webui_headers(),
    )
    assert repaired.status_code == 200
    assert repaired.json()["result_id"] == result_id
    assert len(sandbox.calls) == 1
    assert [call[2] for call in sandbox.render_calls] == [
        "broken renderer",
        "corrected renderer",
    ]
    assert sandbox.render_calls[0][0] == sandbox.render_calls[1][0]


def test_render_code_error_lists_real_saved_filenames(tmp_path: Path) -> None:
    analysis = _result_with_artifacts(
        _artifact("smash_response_summary.json", b'{"count":4}', extension=".json"),
    )
    sandbox = _FakeSandbox(
        analysis,
        render_result=SandboxCodeError(
            hint="missing_file",
            diagnostic={"line": 1, "filename": "smash_response_final.json"},
            _host_validated=True,
        ),
    )
    client = _client(tmp_path, sandbox)
    analyzed = client.post(
        "/tools/analyze", json={"code": "save summary"}, headers=_webui_headers()
    )

    rendered = client.post(
        "/tools/render-chart",
        json={"result_id": analyzed.json()["result_id"], "code": "read final"},
        headers=_webui_headers(),
    )

    assert rendered.status_code == 422
    assert rendered.json()["code"] == "render_code_error"
    assert rendered.json()["details"]["available_files"] == [
        "smash_response_summary.json"
    ]
    assert "smash_response_final.json" in rendered.json()["message"]
    assert str(tmp_path) not in rendered.text


def test_render_result_is_unavailable_outside_original_chat_scope(
    tmp_path: Path,
) -> None:
    store_root = tmp_path / "persistent-results"
    store = AnalysisResultStore(store_root)
    original_client = _client(
        tmp_path,
        _FakeSandbox(_result(b'{"count":1}')),
        result_store=store,
    )
    analyzed = original_client.post(
        "/tools/analyze", json={"code": "pass"}, headers=_webui_headers()
    )
    result_id = analyzed.json()["result_id"]

    reopened_store = AnalysisResultStore(store_root)
    other_client = _client(
        tmp_path,
        _FakeSandbox(_result(), render_result=_render_result()),
        result_store=reopened_store,
    )
    response = other_client.post(
        "/tools/render-chart",
        json={"result_id": result_id, "code": "pass"},
        headers={**_webui_headers(), "X-OpenWebUI-Chat-Id": "other-chat"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "analysis_result_not_found"


def test_zero_event_json_artifact_cannot_be_rendered_without_name_convention(
    tmp_path: Path,
) -> None:
    class _Bridge:
        configured = True

        def __init__(self) -> None:
            self.embeds: list[str] = []

        def emit_chart_embed(self, html_content: str, **_kwargs: Any) -> str:
            self.embeds.append(html_content)
            return "embedded"

    analysis = _result_with_artifacts(
        _artifact("probe.json", b'{"event_count":0}', extension=".json"),
    )
    sandbox = _FakeSandbox(analysis, render_result=_render_result())
    bridge = _Bridge()
    client = _client(tmp_path, sandbox, chart_bridge=bridge)

    analyzed = client.post(
        "/tools/analyze", json={"code": "pass"}, headers=_webui_headers()
    )
    assert analyzed.status_code == 200
    rendered = client.post(
        "/tools/render-chart",
        json={"result_id": analyzed.json()["result_id"], "code": "pass"},
        headers=_webui_headers(),
    )
    assert rendered.status_code == 422
    assert rendered.json()["code"] == "render_invalid_spec"
    assert bridge.embeds == []
    assert len(sandbox.calls) == 1
    assert len(sandbox.render_calls) == 1


def test_invalid_plotly_has_no_png_fallback_and_preserves_summary(
    tmp_path: Path,
) -> None:
    analysis = _result_with_artifacts(
        _artifact("summary.json", b'{"count":4}', extension=".json"),
        _artifact("summary.png", _valid_png_bytes(), extension=".png"),
    )
    sandbox = _FakeSandbox(
        analysis,
        render_result=_result_with_artifacts(
            _artifact(
                "plotly_charts.json",
                _plotly_charts_bytes(valid=False),
                extension=".json",
            )
        ),
    )
    client = _client(tmp_path, sandbox)
    analyzed = client.post(
        "/tools/analyze", json={"code": "pass"}, headers=_webui_headers()
    )
    rendered = client.post(
        "/tools/render-chart",
        json={"result_id": analyzed.json()["result_id"], "code": "pass"},
        headers=_webui_headers(),
    )
    assert rendered.status_code == 422
    assert rendered.json()["code"] == "render_invalid_spec"
    assert "summary.png" not in analyzed.text


def test_invalid_chart_wrapper_names_contract_and_preserves_result_id(
    tmp_path: Path,
) -> None:
    analysis = _result_with_artifacts(
        _artifact("stats.json", b'{"metric":294}', extension=".json")
    )
    invalid_chart_payload = {
        "schema_version": "badminton-plotly/v1",
        "charts": [{"figure": {"data": [{"type": "bar", "x": ["A"], "y": [1]}]}}],
    }
    render = _result_with_artifacts(
        _artifact(
            "plotly_charts.json",
            json.dumps(invalid_chart_payload).encode("utf-8"),
            extension=".json",
        )
    )
    sandbox = _FakeSandbox(analysis, render_result=render)
    client = _client(tmp_path, sandbox)
    headers = _webui_headers()
    analyzed = client.post("/tools/analyze", json={"code": "pass"}, headers=headers)
    result_id = analyzed.json()["result_id"]

    rendered = client.post(
        "/tools/render-chart",
        json={"result_id": result_id, "code": "# 修正 charts wrapper"},
        headers=headers,
    )

    assert rendered.status_code == 422
    error = rendered.json()
    assert error["code"] == "render_invalid_spec"
    assert "每個 charts 元素必須恰為 {title, figure}" in error["message"]
    assert error["details"]["result_id"] == result_id
    assert error["details"]["attempt"] == 1
    assert len(sandbox.calls) == 1, "繪圖契約失敗後沿用分析結果，只修繪圖程式"
    assert len(sandbox.render_calls) == 1


def test_unknown_embed_blocks_renderer_replay_not_new_analysis(
    tmp_path: Path,
) -> None:
    class _Bridge:
        configured = True

        def emit_chart_embed(self, *_args: Any, **_kwargs: Any) -> str:
            raise OpenWebUIEventOutcomeUnknown("private event response")

    result = _result_with_artifacts(
        _artifact("summary.json", b'{"count":4}', extension=".json"),
    )
    sandbox = _FakeSandbox(result, render_result=_render_result())
    client = _client(tmp_path, sandbox, chart_bridge=_Bridge())
    analyzed = client.post(
        "/tools/analyze", json={"code": "pass"}, headers=_webui_headers()
    )
    assert analyzed.status_code == 200
    first = client.post(
        "/tools/render-chart",
        json={"result_id": analyzed.json()["result_id"], "code": "pass"},
        headers=_webui_headers(),
    )
    assert first.status_code == 409
    assert first.json()["code"] == "chart_state_unknown"
    second = client.post(
        "/tools/render-chart",
        json={"result_id": analyzed.json()["result_id"], "code": "pass"},
        headers=_webui_headers(),
    )
    assert second.status_code == 409
    assert second.json()["code"] == "chart_state_unknown"
    assert len(sandbox.calls) == 1
    assert len(sandbox.render_calls) == 1


def test_analysis_allows_multi_step_successes_and_stops_at_message_run_limit(
    tmp_path: Path,
) -> None:
    sandbox = _FakeSandbox(_result(b'{"count":4}'))
    client = _client(tmp_path, sandbox)
    for index in range(MAX_ANALYSIS_RUNS_PER_MESSAGE):
        response = client.post(
            "/tools/analyze",
            json={"code": f"step_{index}"},
            headers=_webui_headers(),
        )
        assert response.status_code == 200
        assert response.json()["analysis_runs_remaining"] == (
            MAX_ANALYSIS_RUNS_PER_MESSAGE - index - 1
        )
    assert len(sandbox.calls) == MAX_ANALYSIS_RUNS_PER_MESSAGE

    for _ in range(5):
        limited = client.post(
            "/tools/analyze", json={"code": "pass"}, headers=_webui_headers()
        )
        assert limited.status_code == 429
        assert limited.json()["code"] == "analysis_message_limit"
        assert limited.json()["details"] == {
            "runs": MAX_ANALYSIS_RUNS_PER_MESSAGE,
            "max_runs": MAX_ANALYSIS_RUNS_PER_MESSAGE,
            "terminal": True,
            "analysis_runs_remaining": 0,
        }
    assert len(sandbox.calls) == MAX_ANALYSIS_RUNS_PER_MESSAGE


def test_analysis_retry_limit_is_for_consecutive_failures_only(tmp_path: Path) -> None:
    sandbox = _FakeSandbox([SandboxCodeError("分析程式有未定義變數") for _ in range(4)])
    client = _client(tmp_path, sandbox)
    for _ in range(MAX_ANALYSIS_FAILURES_PER_MESSAGE):
        response = client.post(
            "/tools/analyze", json={"code": "pass"}, headers=_webui_headers()
        )
        assert response.status_code == 422
        assert response.json()["code"] == "analysis_code_error"

    for _ in range(5):
        limited = client.post(
            "/tools/analyze", json={"code": "pass"}, headers=_webui_headers()
        )
        assert limited.status_code == 429
        assert limited.json()["code"] == "analysis_retry_limit"
        assert limited.json()["details"] == {
            "failed_attempts": MAX_ANALYSIS_FAILURES_PER_MESSAGE,
            "max_failed_attempts": MAX_ANALYSIS_FAILURES_PER_MESSAGE,
            "terminal": True,
            "analysis_runs_remaining": MAX_ANALYSIS_RUNS_PER_MESSAGE
            - MAX_ANALYSIS_FAILURES_PER_MESSAGE,
        }
    assert len(sandbox.calls) == MAX_ANALYSIS_FAILURES_PER_MESSAGE


def test_success_resets_consecutive_repairable_failure_count(tmp_path: Path) -> None:
    sandbox = _FakeSandbox(
        [
            SandboxCodeError("分析程式有未定義變數"),
            SandboxOutputError("缺少 artifact"),
            SandboxCodeError("分析程式有未定義變數"),
            _result(),
            SandboxCodeError("分析程式有未定義變數"),
            SandboxOutputError("缺少 artifact"),
            SandboxCodeError("分析程式有未定義變數"),
            _result(),
        ]
    )
    client = _client(tmp_path, sandbox)
    statuses = [
        client.post(
            "/tools/analyze", json={"code": f"step_{index}"}, headers=_webui_headers()
        ).status_code
        for index in range(8)
    ]

    assert statuses == [422, 422, 422, 200, 422, 422, 422, 200]
    assert len(sandbox.calls) == 8


@pytest.mark.parametrize(
    ("exception", "status"),
    [
        (SandboxExecutionError("failed"), 502),
        (SandboxUnavailableError("offline"), 503),
        (SandboxTimeoutError("timeout"), 504),
    ],
)
def test_infrastructure_failure_terminates_later_analysis_for_message(
    tmp_path: Path,
    exception: Exception,
    status: int,
) -> None:
    sandbox = _FakeSandbox([exception, _result()])
    client = _client(tmp_path, sandbox)

    first = client.post(
        "/tools/analyze", json={"code": "step_1"}, headers=_webui_headers()
    )
    second = client.post(
        "/tools/analyze", json={"code": "step_2"}, headers=_webui_headers()
    )
    next_message = client.post(
        "/tools/analyze",
        json={"code": "step_3"},
        headers=_webui_headers("message-2"),
    )

    assert first.status_code == status
    assert first.json()["details"] == {
        "terminal": True,
        "analysis_runs_remaining": MAX_ANALYSIS_RUNS_PER_MESSAGE - 1,
    }
    assert second.status_code == 409
    assert second.json()["code"] == "analysis_terminated"
    assert next_message.status_code == 200
    assert len(sandbox.calls) == 2


def test_access_log_tracks_tool_error_without_sensitive_request_data(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = _client(tmp_path, _FakeSandbox(SandboxExecutionError("private failure")))
    secret_code = "private_user_analysis_code_9273"
    secret_key = "private_api_key_9273"
    with caplog.at_level(logging.INFO, logger="uvicorn.error.badminton_ai"):
        response = client.post(
            "/tools/analyze?private_query=9273",
            json={"code": secret_code},
            headers={"Authorization": f"Bearer {secret_key}", **_webui_headers()},
        )

    assert response.status_code == 502
    assert len(response.headers["X-Request-ID"]) == 32
    assert response.headers["X-Badminton-Error-Code"] == "sandbox_execution_failure"
    records = [
        record.message
        for record in caplog.records
        if record.name == "uvicorn.error.badminton_ai"
    ]
    assert len(records) == 1
    event = json.loads(records[0])
    assert event == {
        "event": "http_request",
        "request_id": response.headers["X-Request-ID"],
        "method": "POST",
        "route": "/tools/analyze",
        "status": 502,
        "duration_ms": event["duration_ms"],
        "error_code": "sandbox_execution_failure",
    }
    assert event["duration_ms"] >= 0
    assert all(
        secret not in records[0]
        for secret in (secret_code, secret_key, "private_query", "chat-1", "admin-1")
    )


def test_server_disables_unsafe_default_access_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uvicorn

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda _app, **kwargs: calls.append(kwargs))
    monkeypatch.setenv("BADMINTON_AI_HOST", "127.0.0.1")
    monkeypatch.setenv("BADMINTON_AI_PORT", "8000")

    main()

    assert calls == [{"host": "127.0.0.1", "port": 8000, "access_log": False}]


@pytest.mark.parametrize(
    ("exception", "status", "code"),
    [
        (SandboxUnavailableError("offline"), 503, "sandbox_unavailable"),
        (SandboxTimeoutError("timeout"), 504, "sandbox_timeout"),
        (SandboxExecutionError("failed"), 502, "sandbox_execution_failure"),
        (SandboxCodeError("分析程式有未定義變數"), 422, "analysis_code_error"),
        (
            SandboxOutputError("sandbox 必須產生至少一個 artifact"),
            422,
            "analysis_output_error",
        ),
    ],
)
def test_sandbox_errors_use_stable_error_contract(
    tmp_path: Path,
    exception: Exception,
    status: int,
    code: str,
) -> None:
    client = _client(tmp_path, _FakeSandbox(exception))

    response = client.post("/tools/analyze", json={"code": "pass"})

    assert response.status_code == status
    assert response.json() == {
        "code": code,
        "message": {
            "sandbox_unavailable": "分析 sandbox 目前不可用",
            "sandbox_timeout": "分析執行超過時間上限",
            "sandbox_execution_failure": "分析 sandbox 執行失敗",
            "analysis_code_error": "Python 程式使用未定義名稱；請核對變數與 import 後重試",
            "analysis_output_error": "分析沒有產生可重用檔案；正式結果請在 BADMINTON_OUTPUT_DIR 保存 JSON、CSV 或 JSONL，stdout-only 探查不能供繪圖",
        }[code],
        "details": None,
    }


def test_sandbox_code_error_does_not_echo_untrusted_exception_text(
    tmp_path: Path,
) -> None:
    client = _client(
        tmp_path,
        _FakeSandbox(SandboxCodeError("PRIVATE /host/path api_key=secret")),
    )

    response = client.post("/tools/analyze", json={"code": "pass"})

    assert response.status_code == 422
    assert "PRIVATE" not in response.text
    assert "/host/path" not in response.text
    assert "api_key" not in response.text


def test_other_output_errors_keep_generic_message_without_internal_paths(
    tmp_path: Path,
) -> None:
    client = _client(
        tmp_path,
        _FakeSandbox(SandboxOutputError("private/path/secret.txt")),
    )

    response = client.post("/tools/analyze", json={"code": "pass"})

    assert response.status_code == 422
    assert response.json() == {
        "code": "analysis_output_error",
        "message": "分析程式輸出的檔案不符合契約；請核對產物位置、數量、格式與大小",
        "details": None,
    }


def test_missing_data_is_degraded_and_returns_data_unavailable(tmp_path: Path) -> None:
    del tmp_path

    def unavailable() -> ToolServices:
        raise CompositionError("資料來源不存在")

    client = TestClient(create_app(service_loader=unavailable))

    assert client.get("/health").json()["status"] == "degraded"
    response = client.get("/tools/dataset-summary")
    assert response.status_code == 503
    assert response.json() == {
        "code": "data_unavailable",
        "message": "核准資料目前不可用",
        "details": None,
    }


def test_composition_loads_only_source_directory_data(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    data_file = source_dir / "events.csv"
    data_file.write_text(
        ",".join(MVP_REQUIRED_COLUMNS)
        + "\n"
        + ",".join(str(_row()[column]) for column in MVP_REQUIRED_COLUMNS)
        + "\n",
        encoding="utf-8",
    )
    settings = AppSettings("test", source_dir, tmp_path / "runtime")
    fake_sandbox = _FakeSandbox(_result())

    services = build_services(
        settings,
        data_file=Path("events.csv"),
        sandbox=fake_sandbox,  # type: ignore[arg-type]
    )

    assert services.query.snapshot.row_count == 1
    assert services.query.snapshot.source == data_file.resolve()
    assert services.analysis_results is not None
    assert services.analysis_results.root == (tmp_path / "runtime" / "analysis-results")

    with pytest.raises(CompositionError):
        build_services(
            settings,
            data_file=tmp_path / "outside.csv",
            sandbox=fake_sandbox,  # type: ignore[arg-type]
        )


def test_composition_uses_optional_validated_sandbox_image_override(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    data_file = source_dir / "events.csv"
    data_file.write_text(
        ",".join(MVP_REQUIRED_COLUMNS)
        + "\n"
        + ",".join(str(_row()[column]) for column in MVP_REQUIRED_COLUMNS)
        + "\n",
        encoding="utf-8",
    )
    settings = AppSettings("test", source_dir, tmp_path / "runtime")
    image = "badmintonai-sandbox:diagnostics-20261003"

    services = build_services(
        settings,
        data_file=Path("events.csv"),
        environ={
            "BADMINTON_AI_SANDBOX_IMAGE": image,
            "BADMINTON_AI_ANALYSIS_RESULTS_DIR": str(tmp_path / "saved-results"),
        },
    )
    assert isinstance(services.sandbox, DockerSandboxRunner)
    assert services.sandbox.policy.image == image
    assert services.analysis_results is not None
    assert services.analysis_results.root == (tmp_path / "saved-results")

    default_services = build_services(
        settings,
        data_file=Path("events.csv"),
        environ={},
    )
    assert isinstance(default_services.sandbox, DockerSandboxRunner)
    assert default_services.sandbox.policy.image == DEFAULT_SANDBOX_IMAGE

    with pytest.raises(SandboxPolicyError):
        build_services(
            settings,
            data_file=Path("events.csv"),
            environ={"BADMINTON_AI_SANDBOX_IMAGE": "badmintonai-sandbox:latest"},
        )


def test_catalog_internal_failure_does_not_leak_exception(tmp_path: Path) -> None:
    query = _query(tmp_path)

    class BrokenCatalog:
        def dataset_summary(self) -> None:
            raise RuntimeError("C:/private/secret.csv")

    services = ToolServices(
        query=query,
        catalog=BrokenCatalog(),  # type: ignore[arg-type]
        sandbox=_FakeSandbox(_result()),
    )
    response = TestClient(
        create_app(services),
        raise_server_exceptions=False,
    ).get("/tools/dataset-summary")

    assert response.status_code == 500
    assert response.json() == {
        "code": "internal_error",
        "message": "服務內部錯誤",
        "details": None,
    }
