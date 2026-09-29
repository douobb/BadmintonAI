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
    MaterializationManifest,
    SandboxArtifact,
    SandboxCodeError,
    SandboxExecutionError,
    SandboxOutputError,
    SandboxResult,
    SandboxTimeoutError,
    SandboxUnavailableError,
)
from badminton_ai.server.app import (
    MAX_ANALYSIS_FAILURES_PER_MESSAGE,
    MAX_ANALYSIS_RUNS_PER_MESSAGE,
    MAX_ARTIFACT_TEXT_PREVIEW_BYTES,
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
    ) -> None:
        self.result = result
        self.results = list(result) if isinstance(result, list) else None
        self.calls: list[tuple[BadmintonQueryService, str]] = []

    def run(self, query: BadmintonQueryService, code: str) -> SandboxResult:
        self.calls.append((query, code))
        result = self.results.pop(0) if self.results is not None else self.result
        if isinstance(result, Exception):
            raise result
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
) -> TestClient:
    query = _query(tmp_path)
    services = ToolServices(
        query=query,
        catalog=BadmintonCatalogService(query),
        sandbox=_FakeSandbox(_result()) if sandbox is None else sandbox,
    )
    return TestClient(
        create_app(
            services,
            chart_bridge=chart_bridge
            or OpenWebUIChartBridge(base_url=None, api_key=None),
        )
    )


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
    } <= operations
    assert "/tools/analyze" in schema["paths"]
    assert "/tools/request-clarification" in schema["paths"]
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
        "listColumnCatalog",
        "必要口徑未確認前不要呼叫本工具",
        "欄位探查先用 listColumnCatalog",
        "不得以 Python 探查",
        "BADMINTON_EVENTS_FILE",
        "BADMINTON_MANIFEST_FILE",
        "BADMINTON_METADATA_FILE",
        "BADMINTON_OUTPUT_DIR",
        "Path(os.environ['BADMINTON_OUTPUT_DIR'])",
        "每次須在 BADMINTON_OUTPUT_DIR 輸出至少一個檔案",
        "不要先以 print 探查再另呼叫分析",
        "print 不算產物",
        "聊天圖表只能用 Plotly",
        "plt/Matplotlib 產生的 PNG 不會顯示於聊天",
        "plotly_charts.json",
        "schema_version='badminton-plotly/v1'",
        "charts（1–4 個）",
        "figure=json.loads(fig.to_json())",
        "不受固定模板限制",
        "篩選結果為零時不得放寬確認條件湊非零；只輸出摘要，不輸出圖表",
        "不要另寫 plotly_charts.json 的 Markdown 連結",
    ):
        assert term in analysis_description

    for stale in ("5,191", "TASK-", "badminton-chart/v1", "custom_data"):
        assert stale not in analysis_description
    analyze_description = schema["paths"]["/tools/analyze"]["post"]["description"]
    assert "完整資料快照、metadata/schema" in analyze_description
    assert "API 程序不直接執行程式碼" in analyze_description
    artifact_schema = schema["components"]["schemas"]["ArtifactResponse"]
    artifact_properties = artifact_schema["properties"]
    assert "content_base64" in artifact_properties
    assert "chart_display_status" in artifact_properties
    assert "attachment_unknown" in artifact_properties["chart_display_status"]["enum"]
    assert "rendered_as_rich_ui" in artifact_properties["chart_display_status"]["enum"]
    assert "text_preview" in artifact_properties
    assert "preview_truncated" in artifact_properties
    assert "最多 16 KiB UTF-8" in artifact_properties["text_preview"]["description"]
    assert "不得推測" in artifact_properties["preview_truncated"]["description"]
    analysis_response = schema["components"]["schemas"]["AnalysisResponse"]
    assert "rich_ui_status" in analysis_response["properties"]
    assert "embedded" in analysis_response["properties"]["rich_ui_status"]["enum"]
    assert (
        "duplicate_suppressed"
        in analysis_response["properties"]["rich_ui_status"]["enum"]
    )
    assert (
        "沿用既有圖" in analysis_response["properties"]["rich_ui_status"]["description"]
    )
    assert "embed_unknown" in analysis_response["properties"]["rich_ui_status"]["enum"]
    assert "rich_ui_error" in analysis_response["properties"]
    assert (
        "不包含 chart spec 原文、HTML、圖表資料值"
        in analysis_response["properties"]["rich_ui_error"]["description"]
    )
    assert "圖表可選" in analysis_description
    assert "不可交回 figure JSON 字串、HTML 或 PNG" in analysis_description
    assert (
        "只能依可見且未截斷的文字預覽"
        in schema["components"]["schemas"]["AnalysisResponse"]["properties"][
            "artifacts"
        ]["description"]
    )
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
    assert response.json()["rich_ui_status"] == "not_requested"
    assert response.json()["rich_ui_error"] is None
    assert response.json()["artifacts"][0]["relative_path"] == "summary.json"
    assert str(tmp_path) not in response.text
    assert sandbox.calls[0][1] == "print('ok')"
    assert response.json()["artifacts"][0]["content_base64"] == base64.b64encode(
        b"{}"
    ).decode("ascii")


@pytest.mark.parametrize(
    ("extension", "content"),
    [
        (".json", '{"type":"發短球"}'.encode("utf-8")),
        (".jsonl", '{"type":"發短球"}\n'.encode("utf-8")),
        (".csv", "type,count\n發短球,2\n".encode("utf-8")),
    ],
)
def test_analysis_text_artifact_has_exact_preview_and_unchanged_base64(
    tmp_path: Path,
    extension: str,
    content: bytes,
) -> None:
    response = _client(
        tmp_path,
        _FakeSandbox(_result(content, extension=extension)),
    ).post("/tools/analyze", json={"code": "pass"})

    assert response.status_code == 200
    artifact = response.json()["artifacts"][0]
    assert artifact["text_preview"] == content.decode("utf-8")
    assert artifact["preview_truncated"] is False
    assert artifact["content_base64"] == base64.b64encode(content).decode("ascii")
    assert artifact["chart_display_status"] == "not_applicable"


def test_analysis_text_preview_is_bounded_at_utf8_boundary(
    tmp_path: Path,
) -> None:
    header = '{"value":"'
    content = (
        header.encode("utf-8")
        + b"A" * (MAX_ARTIFACT_TEXT_PREVIEW_BYTES - len(header.encode("utf-8")) - 1)
        + "中".encode("utf-8")
        + b'"}'
    )
    response = _client(
        tmp_path,
        _FakeSandbox(_result(content)),
    ).post("/tools/analyze", json={"code": "pass"})

    assert response.status_code == 200
    artifact = response.json()["artifacts"][0]
    preview = artifact["text_preview"]
    assert artifact["preview_truncated"] is True
    assert len(preview.encode("utf-8")) <= MAX_ARTIFACT_TEXT_PREVIEW_BYTES
    assert preview == header + "A" * (
        MAX_ARTIFACT_TEXT_PREVIEW_BYTES - len(header.encode("utf-8")) - 1
    )
    assert "�" not in preview
    assert artifact["content_base64"] == base64.b64encode(content).decode("ascii")


def test_analysis_without_png_does_not_call_chart_bridge(tmp_path: Path) -> None:
    class _ForbiddenBridge:
        configured = True

        def attach_pngs(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("沒有 PNG 時不可呼叫圖表橋接")

    response = _client(
        tmp_path,
        _FakeSandbox(_result(b"{}", extension=".json")),
        chart_bridge=_ForbiddenBridge(),
    ).post("/tools/analyze", json={"code": "pass"})

    assert response.status_code == 200
    artifact = response.json()["artifacts"][0]
    assert artifact["chart_display_status"] == "not_applicable"
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

        def attach_pngs(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("新圖不可附加 PNG")

        def emit_chart_embed(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("舊 chart_spec 不可嵌入")

    result = _result_with_artifacts(
        _artifact("chart_spec.json", _chart_spec_bytes(), extension=".json"),
        _artifact("summary.json", b'{"count":4}', extension=".json"),
        _artifact("summary.png", _valid_png_bytes(), extension=".png"),
    )
    response = _client(tmp_path, _FakeSandbox(result), chart_bridge=_Bridge()).post(
        "/tools/analyze", json={"code": "pass"}, headers=_webui_headers()
    )
    body = response.json()
    assert response.status_code == 200
    assert body["rich_ui_status"] == "invalid_spec"
    assert [a["relative_path"] for a in body["artifacts"]] == ["summary.json"]
    assert "summary.png" not in response.text


def test_plotly_embeds_multiple_figures_once_and_hides_legacy_outputs(
    tmp_path: Path,
) -> None:
    class _Bridge:
        configured = True

        def __init__(self) -> None:
            self.embeds: list[str] = []

        def emit_chart_embed(self, html_content: str, **_kwargs: Any) -> str:
            self.embeds.append(html_content)
            return "embedded"

        def attach_pngs(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("不使用 PNG 備援")

    charts = json.loads(_plotly_charts_bytes())
    charts["charts"].append(
        {
            "title": "第二張",
            "figure": {
                "data": [{"type": "scatter", "x": [1, 2], "y": [2, 1]}],
                "layout": {},
            },
        }
    )
    result = _result_with_artifacts(
        _artifact("plotly_charts.json", json.dumps(charts).encode(), extension=".json"),
        _artifact("chart_spec.json", _chart_spec_bytes(), extension=".json"),
        _artifact("summary.json", b'{"count":4}', extension=".json"),
        _artifact("summary.png", _valid_png_bytes(), extension=".png"),
    )
    sandbox = _FakeSandbox(result)
    bridge = _Bridge()
    client = _client(tmp_path, sandbox, chart_bridge=bridge)
    response = client.post(
        "/tools/analyze", json={"code": "pass"}, headers=_webui_headers()
    )
    assert response.status_code == 200
    assert response.json()["rich_ui_status"] == "embedded"
    assert [a["relative_path"] for a in response.json()["artifacts"]] == [
        "summary.json"
    ]
    assert len(bridge.embeds) == 1
    assert bridge.embeds[0].count('class="plotly-figure" data-chart-index=') == 2
    duplicate = client.post(
        "/tools/analyze", json={"code": "pass"}, headers=_webui_headers()
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "chart_already_embedded"
    assert len(sandbox.calls) == 1


def test_zero_event_summary_does_not_embed_chart_or_block_correction(
    tmp_path: Path,
) -> None:
    class _Bridge:
        configured = True

        def __init__(self) -> None:
            self.embeds: list[str] = []

        def emit_chart_embed(self, html_content: str, **_kwargs: Any) -> str:
            self.embeds.append(html_content)
            return "embedded"

    first = _result_with_artifacts(
        _artifact("plotly_charts.json", _plotly_charts_bytes(), extension=".json"),
        _artifact("summary.json", b'{"event_count":0}', extension=".json"),
    )
    corrected = _result_with_artifacts(
        _artifact("plotly_charts.json", _plotly_charts_bytes(), extension=".json"),
        _artifact("summary.json", b'{"event_count":4}', extension=".json"),
    )
    sandbox = _FakeSandbox([first, corrected])
    bridge = _Bridge()
    client = _client(tmp_path, sandbox, chart_bridge=bridge)

    empty = client.post(
        "/tools/analyze", json={"code": "pass"}, headers=_webui_headers()
    )
    assert empty.status_code == 200
    assert empty.json()["rich_ui_status"] == "invalid_spec"
    assert "event_count 為 0" in empty.json()["rich_ui_error"]
    assert bridge.embeds == []

    retry = client.post(
        "/tools/analyze", json={"code": "pass"}, headers=_webui_headers()
    )
    assert retry.status_code == 200
    assert retry.json()["rich_ui_status"] == "embedded"
    assert len(bridge.embeds) == 1
    assert len(sandbox.calls) == 2


def test_invalid_plotly_has_no_png_fallback_and_preserves_summary(
    tmp_path: Path,
) -> None:
    result = _result_with_artifacts(
        _artifact(
            "plotly_charts.json", _plotly_charts_bytes(valid=False), extension=".json"
        ),
        _artifact("summary.json", b'{"count":4}', extension=".json"),
        _artifact("summary.png", _valid_png_bytes(), extension=".png"),
    )
    response = _client(tmp_path, _FakeSandbox(result)).post(
        "/tools/analyze", json={"code": "pass"}, headers=_webui_headers()
    )
    body = response.json()
    assert response.status_code == 200
    assert body["rich_ui_status"] == "invalid_spec"
    assert [a["relative_path"] for a in body["artifacts"]] == ["summary.json"]


def test_unknown_embed_blocks_reanalysis_to_avoid_duplicate_chart(
    tmp_path: Path,
) -> None:
    class _Bridge:
        configured = True

        def emit_chart_embed(self, *_args: Any, **_kwargs: Any) -> str:
            raise OpenWebUIEventOutcomeUnknown("private event response")

    result = _result_with_artifacts(
        _artifact("plotly_charts.json", _plotly_charts_bytes(), extension=".json"),
        _artifact("summary.json", b'{"count":4}', extension=".json"),
    )
    sandbox = _FakeSandbox(result)
    client = _client(tmp_path, sandbox, chart_bridge=_Bridge())
    first = client.post(
        "/tools/analyze", json={"code": "pass"}, headers=_webui_headers()
    )
    assert first.status_code == 200
    assert first.json()["rich_ui_status"] == "embed_unknown"
    second = client.post(
        "/tools/analyze", json={"code": "pass"}, headers=_webui_headers()
    )
    assert second.status_code == 409
    assert second.json()["code"] == "chart_state_unknown"
    assert len(sandbox.calls) == 1


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
    assert first.json()["details"] == {"terminal": True}
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
            "analysis_code_error": "分析程式有未定義變數",
            "analysis_output_error": "分析沒有產生檔案；請在 BADMINTON_OUTPUT_DIR 寫入至少一個檔案（例如 summary.json），print 輸出不算產物",
        }[code],
        "details": None,
    }


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

    with pytest.raises(CompositionError):
        build_services(
            settings,
            data_file=tmp_path / "outside.csv",
            sandbox=fake_sandbox,  # type: ignore[arg-type]
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
