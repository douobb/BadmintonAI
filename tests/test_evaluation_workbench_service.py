"""評測工作台 admin API、來源限制、續跑與輸出安全測試。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.staticfiles import StaticFiles

from badminton_ai.server.plotly_rich import (
    PLOTLY_SPEC_VERSION,
    render_plotly_charts_html,
    validate_plotly_charts,
)
from scripts.evaluation_openwebui_client import OpenWebUIStateUncertain, _turn_result
from scripts.evaluation_runner import EvaluationRunner, TurnResult
from scripts.evaluation_workbench_service import (
    DEFAULT_SOURCE_PATH,
    MAX_UPLOAD_BYTES,
    ROUTE_NAMES,
    ROUTES,
    EvaluationWorkbenchService,
    WorkbenchError,
    _state_for_ui,
    create_api_router,
)


def test_openwebui_adapter_wait_cap_tracks_total_timeout_plus_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BADMINTON_AI_OPEN_WEBUI_API_KEY", "test-only-key")
    monkeypatch.setenv("AIOHTTP_CLIENT_TIMEOUT", "1200")
    monkeypatch.setenv("BADMINTON_AI_EVALUATION_ADAPTER_WAIT_GRACE_SECONDS", "30")
    monkeypatch.delenv("BADMINTON_AI_EVALUATION_ADAPTER_WAIT_SECONDS", raising=False)

    client = EvaluationWorkbenchService._client_from_environment()

    assert client._max_wait_seconds == 1230

    monkeypatch.setenv("BADMINTON_AI_EVALUATION_ADAPTER_WAIT_GRACE_SECONDS", "90")
    assert (
        EvaluationWorkbenchService._client_from_environment()._max_wait_seconds == 1290
    )

    monkeypatch.setenv("BADMINTON_AI_EVALUATION_ADAPTER_WAIT_GRACE_SECONDS", "nan")
    with pytest.raises(WorkbenchError, match="逾時設定"):
        EvaluationWorkbenchService._client_from_environment()


async def _admin_dependency(request: Request) -> dict[str, str]:
    authorization = request.headers.get("authorization")
    if authorization == "Bearer admin-test":
        return {"id": "admin-42", "username": "test-admin", "role": "admin"}
    if authorization == "Bearer user-test":
        raise HTTPException(status_code=403, detail="Admin access required")
    raise HTTPException(status_code=401, detail="Not authenticated")


class FakeClient:
    def __init__(self, *, answer: str = "分析完成") -> None:
        self.answer = answer
        self.created: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str]] = []
        self.exported_conversations: list[tuple[str, str]] = []
        self.export_html = "<!doctype html><html><body>原對話</body></html>"
        self.export_error: Exception | None = None
        self.started = threading.Event()
        self.release = threading.Event()
        self.block_first = False
        self.block_content: str | None = None
        self.block_timeout = 5
        self.clarify_first = False
        self.clarify_all_first = False

    def get_model_snapshot(self) -> dict[str, Any]:
        return {
            "model_id": "badmintonai",
            "updated_at": "2026-09-26T00:00:00Z",
            "tool_ids": ["server:badminton-ai"],
            "captured_at": "2026-09-26T00:00:00Z",
        }

    def recover_conversation(self, *_args: Any) -> None:
        return None

    def create_conversation(
        self,
        run_id: str,
        question_id: str,
        idempotency_key: str,
        model_snapshot: Any,
    ) -> str:
        del run_id, idempotency_key, model_snapshot
        conversation_id = f"chat-{question_id}"
        self.created.append((question_id, conversation_id))
        return conversation_id

    def create_conversation_in_folder(
        self,
        run_id: str,
        question_id: str,
        idempotency_key: str,
        model_snapshot: Any,
        *,
        created_at: str,
        question_count: int,
    ) -> str:
        del created_at, question_count
        return self.create_conversation(
            run_id, question_id, idempotency_key, model_snapshot
        )

    def recover_conversation_in_folder(
        self,
        run_id: str,
        question_id: str,
        idempotency_key: str,
        *,
        created_at: str,
        question_count: int,
    ) -> str | None:
        del created_at, question_count
        return self.recover_conversation(run_id, question_id, idempotency_key)

    def send_turn(
        self, conversation_id: str, content: str, operation_id: str
    ) -> TurnResult:
        del operation_id
        self.sent.append((conversation_id, content))
        self.started.set()
        if (self.block_first and len(self.sent) == 1) or content == self.block_content:
            self.release.wait(timeout=self.block_timeout)
        first_turn_for_conversation = (
            len([item for item in self.sent if item[0] == conversation_id]) == 1
        )
        if first_turn_for_conversation and (
            self.clarify_all_first
            or (self.clarify_first and conversation_id == "chat-1")
        ):
            assistant = {
                "role": "assistant",
                "content": "請選擇 A 或 B",
                "options": ["選項 A", "選項 B"],
                "output": [
                    {
                        "type": "function_call",
                        "name": "requestClarification",
                        "status": "completed",
                        "arguments": json.dumps(
                            {
                                "question": "請選擇 A 或 B",
                                "options": ["選項 A", "選項 B"],
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
            }
            return TurnResult(
                messages=[{"role": "user", "content": content}, assistant],
                awaiting_clarification=True,
                clarification_signal="event",
            )
        assistant = {"role": "assistant", "content": self.answer}
        return TurnResult(
            messages=[{"role": "user", "content": content}, assistant],
            usage=None,
        )

    def recover_turn(self, *_args: Any) -> TurnResult | None:
        return None

    def export_chat_html(
        self,
        conversation_id: str,
        *,
        theme: str = "auto",
        redact_value: Callable[[Any], Any] | None = None,
    ) -> str:
        self.exported_conversations.append((conversation_id, theme))
        if self.export_error is not None:
            raise self.export_error
        return redact_value(self.export_html) if redact_value else self.export_html


class BlockingStartWorkbench:
    """模擬 start 在同步網路呼叫期間等待，驗證 handler 不封鎖 event loop。"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def start_source(self, source_id: str, expected_sha256: str) -> dict[str, bool]:
        del source_id, expected_sha256
        self.started.set()
        self.release.wait(timeout=1.5)
        return {"started": True}


def _service(
    storage: Path,
    *,
    question_file: Path | None = None,
    client: FakeClient | None = None,
    max_upload_bytes: int = MAX_UPLOAD_BYTES,
) -> EvaluationWorkbenchService:
    fake = client or FakeClient()
    return EvaluationWorkbenchService(
        storage_dir=storage,
        question_file=question_file or DEFAULT_SOURCE_PATH,
        client_factory=lambda: fake,
        data_snapshot_provider=lambda: {
            "snapshot_id": "sha256:dataset-snapshot-v1",
            "snapshot_version": "dataset-v1",
            "tool_server_version": "0.1.0",
            "source": "events.csv",
        },
        model_snapshot_provider=lambda _client: fake.get_model_snapshot(),
        api_key_provider=lambda: "admin-test-secret",
        max_upload_bytes=max_upload_bytes,
    )


def _app(
    service: EvaluationWorkbenchService,
    static_dir: Path,
    *,
    pdf_renderer: Callable[[str], Any] | None = None,
) -> FastAPI:
    static_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI()
    app.include_router(
        create_api_router(service, _admin_dependency, pdf_renderer=pdf_renderer)
    )
    app.mount("/", StaticFiles(directory=static_dir), name="spa")
    return app


def _post_headers(**extra: str) -> dict[str, str]:
    return {"Origin": "http://testserver", **extra}


def _wait_until(predicate: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("背景評測未在期限內完成")
        time.sleep(0.01)


def _wait_for_worker_idle(service: EvaluationWorkbenchService) -> None:
    _wait_until(
        lambda: not service._worker_is_running() and not service._has_other_worker()
    )


def _failed_retry_service(
    tmp_path: Path,
) -> tuple[EvaluationWorkbenchService, FakeClient, str]:
    class RetryClient(FakeClient):
        def send_turn(
            self, conversation_id: str, content: str, operation_id: str
        ) -> TurnResult:
            self.sent.append((conversation_id, content))
            self.started.set()
            if len(self.sent) > 1 and self.block_content == content:
                self.release.wait(timeout=5)
            return TurnResult(error={"message": "失敗", "retryable": False})

    fake = RetryClient()
    service = _service(tmp_path / "private", client=fake)
    data = "1: 原題\n".encode("utf-8")
    result = service.start_upload("q.txt", data, hashlib.sha256(data).hexdigest())
    _wait_for_worker_idle(service)
    return service, fake, result["run_id"]


def test_manual_retry_route_permissions_binding_and_double_click(
    tmp_path: Path,
) -> None:
    service, fake, run_id = _failed_retry_service(tmp_path)
    assert service._read_state(run_id)["manifest"]["max_attempts"] == 1
    app = _app(service, tmp_path / "static")
    route = (
        ROUTES["badmintonai_evaluation_retry"]
        .replace("{run_id}", run_id)
        .replace("{question_id}", "1")
    )
    admin = {**_post_headers(), "Authorization": "Bearer admin-test"}
    with TestClient(app) as client:
        assert (
            client.post(
                route,
                json={"expected_retry_count": 0},
                headers={**_post_headers(), "Authorization": "Bearer user-test"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                route,
                json={"expected_retry_count": 0},
                headers={"Authorization": "Bearer admin-test"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                route.replace("/1/retry", "/99/retry"),
                json={"expected_retry_count": 0},
                headers=admin,
            ).status_code
            == 409
        )
        fake.block_content = "原題"
        fake.release.clear()
        response = client.post(route, json={"expected_retry_count": 0}, headers=admin)
        assert response.status_code == 200
        repeated = client.post(route, json={"expected_retry_count": 0}, headers=admin)
        assert repeated.status_code == 200
        assert repeated.json()["already_queued"] is True
        fake.release.set()
        _wait_for_worker_idle(service)
        assert (
            client.post(
                route, json={"expected_retry_count": 0}, headers=admin
            ).status_code
            == 409
        )
    assert len(fake.sent) == 2
    assert service._read_state(run_id)["questions"][0]["status"] == "failed"


def test_manual_retry_busy_or_unknown_task_never_sends(tmp_path: Path) -> None:
    service, fake, run_id = _failed_retry_service(tmp_path)
    lease = service._try_lease()
    assert lease is not None
    try:
        queued = service.retry_failed(run_id, "1", 0)
        assert queued["accepted"] is True
        assert queued["already_queued"] is False
        assert service.status(run_id)["run"]["questions"][0]["work_type"] == "retry"
    finally:
        service._release_lease(lease)

    def unknown(*args: Any) -> None:
        raise OpenWebUIStateUncertain("仍活躍")

    fake.recover_turn = unknown
    service.resume(run_id)
    _wait_for_worker_idle(service)
    assert len(fake.sent) == 1
    assert service._read_state(run_id)["questions"][0]["status"] == "failed"


def test_manual_retry_success_projection_and_exports_agree(tmp_path: Path) -> None:
    service, fake, run_id = _failed_retry_service(tmp_path)

    def success(conversation_id: str, content: str, operation_id: str) -> TurnResult:
        fake.sent.append((conversation_id, content))
        return TurnResult(messages=[{"role": "assistant", "content": "完成"}])

    fake.send_turn = success
    service.retry_failed(run_id, "1", 0)
    _wait_for_worker_idle(service)
    projected = service.status(run_id)["run"]
    assert projected["questions"][0]["status"] == "completed"
    assert projected["questions"][0]["manual_retry_succeeded"] is True
    assert projected["manual_retry_succeeded_count"] == 1
    exported = json.loads(service.download_json(run_id))
    assert exported["questions"][0]["turns"][-1]["manual_retry"] is True
    assert "重試後成功：1 題" in service.download_summary(run_id).decode("utf-8")
    assert "重試後成功" in service.report_html(run_id).decode("utf-8")


def _questions_file(
    path: Path, text: str = "1: 第一題原文？\n2: 第二題原文。\n"
) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_only_allowlisted_source_and_utf8_txt_upload_are_previewed(
    tmp_path: Path,
) -> None:
    official = _questions_file(tmp_path / "評估問題_v2.txt")
    service = _service(tmp_path / "private", question_file=official)
    app = _app(service, tmp_path / "static")

    with TestClient(app) as client:
        source_preview = client.post(
            ROUTES["badmintonai_evaluation_preview"],
            json={"source": "v2-natural-100"},
            headers={**_post_headers(), "Authorization": "Bearer admin-test"},
        )
        unknown_source = client.post(
            ROUTES["badmintonai_evaluation_preview"],
            json={"source": "../../etc/passwd"},
            headers={**_post_headers(), "Authorization": "Bearer admin-test"},
        )
        upload_preview = client.post(
            ROUTES["badmintonai_evaluation_preview_upload"],
            content="1: 上傳原題？\n".encode("utf-8"),
            headers={
                **_post_headers(),
                "X-Upload-Filename": "%E8%87%AA%E8%A8%82%E9%A1%8C%E7%9B%AE.txt",
                "Authorization": "Bearer admin-test",
                "Content-Type": "text/plain; charset=utf-8",
            },
        )
        traversal = client.post(
            ROUTES["badmintonai_evaluation_preview_upload"],
            content=b"1: q\n",
            headers={
                **_post_headers(),
                "X-Upload-Filename": "%2e%2e%2fsecret.txt",
                "Authorization": "Bearer admin-test",
                "Content-Type": "text/plain",
            },
        )
        invalid_utf8 = client.post(
            ROUTES["badmintonai_evaluation_preview_upload"],
            content=b"1: \xff\n",
            headers={
                **_post_headers(),
                "X-Upload-Filename": "bad.txt",
                "Authorization": "Bearer admin-test",
                "Content-Type": "text/plain",
            },
        )

    assert source_preview.status_code == 200
    assert source_preview.json()["filename"] == "評估問題_v2.txt"
    assert source_preview.json()["questions"][0]["prompt"] == "第一題原文？"
    assert source_preview.json()["question_count"] == 2
    assert unknown_source.status_code == 400
    assert upload_preview.status_code == 200
    assert upload_preview.json()["questions"][0]["prompt"] == "上傳原題？"
    assert traversal.status_code == 400
    assert invalid_utf8.status_code == 400
    assert not list((tmp_path / "private").rglob("*.txt"))


def test_upload_enforces_body_size_and_numbered_utf8_lines(tmp_path: Path) -> None:
    service = _service(
        tmp_path / "private",
        max_upload_bytes=20,
        question_file=tmp_path / "missing.txt",
    )

    with pytest.raises(WorkbenchError, match="允許大小"):
        service.preview_upload("large.txt", b"1: " + b"a" * 30)
    with pytest.raises(WorkbenchError, match="格式錯誤"):
        service.preview_upload("invalid.txt", b"invalid\n")
    with pytest.raises(WorkbenchError, match="檔名"):
        service.preview_upload("folder\\escape.txt", b"1: safe\n")


def test_every_api_route_requires_admin_and_all_post_routes_check_origin(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "private", question_file=tmp_path / "missing.txt")
    app = _app(service, tmp_path / "static")
    routes = {
        route.name: route
        for route in app.router.routes
        if getattr(route, "name", None) in ROUTE_NAMES
    }

    assert set(routes) == set(ROUTE_NAMES)
    assert all(
        any(item.call is _admin_dependency for item in route.dependant.dependencies)
        for route in routes.values()
    )
    with TestClient(app) as client:
        for route in routes.values():
            if "GET" not in route.methods:
                continue
            response = client.get(route.path.replace("{run_id}", "a" * 32))
            assert response.status_code == 401
        for route_name in (
            "badmintonai_evaluation_download_html",
            "badmintonai_evaluation_print_report",
        ):
            response = client.get(
                ROUTES[route_name].replace("{run_id}", "a" * 32),
                headers={"Authorization": "Bearer user-test"},
            )
            assert response.status_code == 403
        unauthorized_post = client.post(
            ROUTES["badmintonai_evaluation_stop"],
            headers={"Origin": "http://testserver"},
        )
        missing_origin = client.post(
            ROUTES["badmintonai_evaluation_stop"],
            headers={"Authorization": "Bearer admin-test"},
        )
        foreign_origin = client.post(
            ROUTES["badmintonai_evaluation_stop"],
            headers={
                "Authorization": "Bearer admin-test",
                "Origin": "https://attacker.example",
            },
        )

    assert unauthorized_post.status_code == 401
    assert missing_origin.status_code == 403
    assert foreign_origin.status_code == 403


def test_classification_annotation_route_is_admin_only_same_origin_and_separate(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "private")
    run_id = "f" * 32
    service.runs_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": 1,
        "run_id": run_id,
        "manifest": {"question_source": {}, "model_snapshot": {}, "data_snapshot": {}},
        "questions": [
            {
                "id": "14",
                "prompt": "原始題目",
                "status": "completed",
                "turns": [
                    {
                        "result": {
                            "messages": [
                                {"role": "assistant", "content": "您指的是哪一場？"}
                            ],
                            "tool_calls": [],
                            "charts": [],
                        }
                    }
                ],
            }
        ],
    }
    checkpoint = service.runs_dir / f"{run_id}.json"
    checkpoint.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    before = checkpoint.read_bytes()
    app = _app(service, tmp_path / "static")

    with TestClient(app) as client:
        detail_path = ROUTES["badmintonai_evaluation_run"].replace("{run_id}", run_id)
        annotation_path = (
            ROUTES["badmintonai_evaluation_classification_annotation"]
            .replace("{run_id}", run_id)
            .replace("{question_id}", "14")
        )
        anonymous = client.get(detail_path)
        regular_user = client.get(
            detail_path, headers={"Authorization": "Bearer user-test"}
        )
        no_origin = client.post(
            annotation_path,
            json={"classification": "missed_clarification", "reason": "已複核"},
            headers={"Authorization": "Bearer admin-test"},
        )
        saved = client.post(
            annotation_path,
            json={
                "classification": "missed_clarification",
                "reason": "原聊天只追問，人工確認為漏判。",
            },
            headers={
                "Authorization": "Bearer admin-test",
                "Origin": "http://testserver",
            },
        )

    assert anonymous.status_code == 401
    assert regular_user.status_code == 403
    assert no_origin.status_code == 403
    assert saved.status_code == 200
    annotated = saved.json()["run"]["questions"][0]["classification_annotation"]
    assert annotated["actor"] == "test-admin"
    assert annotated["original_status"] == "completed"
    assert checkpoint.read_bytes() == before


def test_html_report_download_and_print_routes_are_admin_only_and_redacted(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "private")
    service._plotly_javascript_provider = lambda: (
        "/*! plotly.js v3.4.0 */ window.Plotly = {};"
    )
    run_id = "a" * 32
    service.runs_dir.mkdir(parents=True, exist_ok=True)
    chart_embed = render_plotly_charts_html(
        validate_plotly_charts(
            {
                "schema_version": PLOTLY_SPEC_VERSION,
                "charts": [
                    {
                        "title": "評測 PDF 圖表",
                        "figure": {
                            "data": [{"type": "bar", "x": ["A"], "y": [1]}],
                            "layout": {},
                        },
                    }
                ],
            },
            native_validation=False,
        )
    )
    chart_result = {
        "messages": [
            {"role": "user", "content": "第一題圖表提問"},
            {
                "role": "assistant",
                "content": "結果 **保留格式**。",
                "embeds": [chart_embed],
            },
        ],
        "usage": None,
        "tool_calls": [],
        "charts": [],
    }
    state = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": "2026-09-27T00:00:00Z",
        "updated_at": "2026-09-27T00:00:01Z",
        "manifest": {
            "question_source": {"name": "questions.txt", "sha256": "b" * 64},
            "model_snapshot": {"model_id": "badmintonai"},
            "data_snapshot": {"snapshot_id": "sha256:dataset-v1"},
        },
        "questions": [
            {
                "id": "1",
                "prompt": "題目包含 admin-test-secret，匯出前必須遮罩。",
                "status": "completed",
                "elapsed_ms": None,
                "processing_elapsed_ms": None,
                "user_wait_ms": 0,
                "usage_totals": None,
                "turns": [
                    {
                        "kind": "question",
                        "request": "第一題圖表提問",
                        "attempts": [{"duration_ms": 10, "result": chart_result}],
                    }
                ],
                "pending_turn": None,
            },
            {
                "id": "2",
                "prompt": "第二題多題 PDF 報告驗收。",
                "status": "completed",
                "elapsed_ms": 25,
                "processing_elapsed_ms": 25,
                "user_wait_ms": 0,
                "usage_totals": None,
                "turns": [],
                "pending_turn": None,
            },
        ],
    }
    (service.runs_dir / f"{run_id}.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )
    rendered_html: list[str] = []

    async def fake_pdf_renderer(document: str) -> bytes:
        rendered_html.append(document)
        return b"%PDF-1.7\nfixture"

    app = _app(service, tmp_path / "static", pdf_renderer=fake_pdf_renderer)

    with TestClient(app) as client:
        path = ROUTES["badmintonai_evaluation_download_html"].replace(
            "{run_id}", run_id
        )
        print_path = ROUTES["badmintonai_evaluation_print_report"].replace(
            "{run_id}", run_id
        )
        downloaded = client.get(path, headers={"Authorization": "Bearer admin-test"})
        printable = client.get(
            print_path, headers={"Authorization": "Bearer admin-test"}
        )
        pdf_path = ROUTES["badmintonai_evaluation_download_pdf"].replace(
            "{run_id}", run_id
        )
        direct_pdf = client.get(
            pdf_path, headers={"Authorization": "Bearer admin-test"}
        )
        regular_user_pdf = client.get(
            pdf_path, headers={"Authorization": "Bearer user-test"}
        )
        invalid_id = client.get(
            path.replace(run_id, "../escape"),
            headers={"Authorization": "Bearer admin-test"},
        )

    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.headers["content-type"].startswith("text/html")
    assert (
        'attachment; filename="evaluation-' in downloaded.headers["content-disposition"]
    )
    assert "connect-src 'none'" in downloaded.headers["content-security-policy"]
    assert "admin-test-secret" not in downloaded.text
    assert "未提供（null）" in downloaded.text
    assert "此題未產生圖表。" in downloaded.text
    assert "第二題多題 PDF 報告驗收。" in downloaded.text
    assert printable.status_code == 200
    assert 'inline; filename="evaluation-' in printable.headers["content-disposition"]
    assert "window.print()" in printable.text
    assert direct_pdf.status_code == 200
    assert direct_pdf.headers["content-type"] == "application/pdf"
    assert direct_pdf.content.startswith(b"%PDF-")
    assert direct_pdf.headers["content-disposition"] == (
        f'attachment; filename="evaluation-{run_id}.pdf"'
    )
    assert "report.pdf" in ROUTES["badmintonai_evaluation_download_pdf"]
    assert rendered_html and run_id in rendered_html[0]
    assert (
        'window.__BADMINTON_PDF_RENDER__ = {status: "pending", error: null}'
        in rendered_html[0]
    )
    assert "評測 PDF 圖表" in rendered_html[0]
    assert "第二題多題 PDF 報告驗收。" in rendered_html[0]
    assert 'pdfState.status = "ready"' in rendered_html[0]
    assert regular_user_pdf.status_code == 403
    assert invalid_id.status_code == 404


def test_conversation_html_route_is_admin_only_and_bound_to_run_question(
    tmp_path: Path,
) -> None:
    fake = FakeClient()
    fake.export_html = (
        "<!doctype html><html><body>admin-test-secret 原始對話</body></html>"
    )
    service = _service(tmp_path / "private", client=fake)
    service.runs_dir.mkdir(parents=True, exist_ok=True)
    run_a = "a" * 32
    run_b = "b" * 32

    def save_run(run_id: str, question_id: str, conversation_id: str | None) -> None:
        state = {
            "schema_version": 1,
            "run_id": run_id,
            "created_at": "2026-09-28T00:00:00Z",
            "updated_at": "2026-09-28T00:00:01Z",
            "manifest": {},
            "questions": [
                {
                    "id": question_id,
                    "prompt": "原題",
                    "status": "completed",
                    "conversation_id": conversation_id,
                    "turns": [],
                    "errors": [],
                }
            ],
        }
        (service.runs_dir / f"{run_id}.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )

    save_run(run_a, "1", "chat-from-run-a")
    save_run(run_b, "4", "chat-from-run-b")
    missing_chat_run = "e" * 32
    save_run(missing_chat_run, "2", "../../not-a-chat")
    app = _app(service, tmp_path / "static")
    template = ROUTES["badmintonai_evaluation_conversation_html"]
    valid_path = template.replace("{run_id}", run_a).replace("{question_id}", "1")
    foreign_question_path = template.replace("{run_id}", run_a).replace(
        "{question_id}", "4"
    )
    run_b_path = template.replace("{run_id}", run_b).replace("{question_id}", "4")
    missing_chat_path = template.replace("{run_id}", missing_chat_run).replace(
        "{question_id}", "2"
    )

    with TestClient(app) as web_client:
        anonymous = web_client.get(valid_path)
        regular_user = web_client.get(
            valid_path, headers={"Authorization": "Bearer user-test"}
        )
        rendered = web_client.get(
            f"{valid_path}?chat_id=chat-from-run-b",
            headers={"Authorization": "Bearer admin-test"},
        )
        cross_run_question = web_client.get(
            foreign_question_path,
            headers={"Authorization": "Bearer admin-test"},
        )
        missing_run = web_client.get(
            template.replace("{run_id}", "c" * 32).replace("{question_id}", "1"),
            headers={"Authorization": "Bearer admin-test"},
        )
        other_run_same_question = web_client.get(
            run_b_path, headers={"Authorization": "Bearer admin-test"}
        )
        missing_chat = web_client.get(
            missing_chat_path, headers={"Authorization": "Bearer admin-test"}
        )
        fake.export_error = RuntimeError("provider-secret org-id-987")
        failed_export = web_client.get(
            valid_path, headers={"Authorization": "Bearer admin-test"}
        )
        fake.export_error = None
        fake.export_html = "<!doctype html><html>\ud800</html>"
        invalid_html = web_client.get(
            valid_path, headers={"Authorization": "Bearer admin-test"}
        )

    assert anonymous.status_code == 401
    assert regular_user.status_code == 403
    assert rendered.status_code == 200
    assert rendered.headers["content-type"].startswith("text/html")
    assert rendered.headers["cache-control"] == "no-store"
    assert rendered.headers["x-content-type-options"] == "nosniff"
    assert rendered.headers["x-frame-options"] == "SAMEORIGIN"
    assert rendered.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'self'" in rendered.headers["content-security-policy"]
    assert "admin-test-secret" not in rendered.text
    assert "[REDACTED] 原始對話" in rendered.text
    assert cross_run_question.status_code == 404
    assert missing_run.status_code == 404
    assert missing_run.headers["cache-control"] == "no-store"
    assert "找不到此題的原對話" in missing_run.text
    assert missing_chat.status_code == 404
    assert "找不到此題的原對話" in missing_chat.text
    assert other_run_same_question.status_code == 200
    assert failed_export.status_code == 502
    assert failed_export.headers["cache-control"] == "no-store"
    assert "原對話讀取失敗或內容無法安全呈現" in failed_export.text
    assert "provider-secret" not in failed_export.text
    assert "org-id-987" not in failed_export.text
    assert invalid_html.status_code == 502
    assert invalid_html.headers["cache-control"] == "no-store"
    assert "原對話讀取失敗或內容無法安全呈現" in invalid_html.text
    assert fake.exported_conversations == [
        ("chat-from-run-a", "auto"),
        ("chat-from-run-b", "auto"),
        ("chat-from-run-a", "auto"),
        ("chat-from-run-a", "auto"),
    ]


def test_q4_processing_time_excludes_saved_manual_wait_without_rewriting_checkpoint(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "private")
    run_id = "d" * 32
    service.runs_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": "2026-09-28T00:00:00Z",
        "updated_at": "2026-09-28T00:00:01Z",
        "manifest": {},
        "questions": [
            {
                "id": "4",
                "prompt": "Q4 fixture",
                "status": "completed",
                "elapsed_ms": 259918,
                "processing_elapsed_ms": None,
                "user_wait_ms": 224405,
                "usage_totals": None,
                "turns": [],
                "errors": [],
            }
        ],
    }
    checkpoint = service.runs_dir / f"{run_id}.json"
    checkpoint.write_text(json.dumps(state), encoding="utf-8")
    original = checkpoint.read_bytes()

    question = service.get_run(run_id)["run"]["questions"][0]
    summary = service.download_summary(run_id).decode("utf-8")

    assert question["elapsed_ms"] == 259918
    assert question["user_wait_ms"] == 224405
    assert question["processing_elapsed_ms"] == 35513
    assert "總處理耗時（不含人工等待補答）：00:35.513（1/1 題有值）" in summary
    assert "總人工等待澄清：03:44.405（1/1 題有值）" in summary
    assert "端到端耗時：04:19.918" in summary
    assert "處理耗時（不含人工等待）：00:35.513" in summary
    assert checkpoint.read_bytes() == original
    assert "未取得模型實際用量" in summary
    assert ROUTES["badmintonai_evaluation_download_pdf"].endswith("report.pdf")


def test_report_asset_fetch_runs_off_event_loop(tmp_path: Path) -> None:
    service = _service(tmp_path / "private")
    started = threading.Event()
    release = threading.Event()

    def blocking_plotly_asset() -> str:
        started.set()
        release.wait(timeout=2)
        return "/*! plotly.js v3.4.0 */ window.Plotly = {};"

    service._plotly_javascript_provider = blocking_plotly_asset
    run_id = "c" * 32
    service.runs_dir.mkdir(parents=True, exist_ok=True)
    embed = (
        '<script id="plotly-figure-data" type="application/json">'
        '{"charts":[{"title":"測試圖","figure":{"data":'
        '[{"type":"bar","x":["A"],"y":[1]}],"layout":{}}}]}'
        "</script>"
    )
    result = {
        "messages": [
            {
                "role": "assistant",
                "content": "圖表結果",
                "embeds": [embed],
            }
        ],
        "usage": None,
        "tool_calls": [],
        "charts": [],
    }
    state = {
        "schema_version": 1,
        "run_id": run_id,
        "manifest": {
            "question_source": {},
            "model_snapshot": {},
            "data_snapshot": {},
        },
        "questions": [
            {
                "id": "1",
                "prompt": "圖表題",
                "status": "completed",
                "turns": [
                    {
                        "kind": "question",
                        "request": "圖表題",
                        "attempts": [{"result": result}],
                    }
                ],
                "usage_totals": None,
            }
        ],
    }
    (service.runs_dir / f"{run_id}.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )
    static_dir = tmp_path / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI()
    app.include_router(create_api_router(service, _admin_dependency))

    @app.get("/event-loop-probe")
    async def event_loop_probe() -> dict[str, bool]:
        return {"responsive": True}

    app.mount("/", StaticFiles(directory=static_dir), name="spa")

    async def exercise() -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            report_task = asyncio.create_task(
                client.get(
                    ROUTES["badmintonai_evaluation_print_report"].replace(
                        "{run_id}", run_id
                    ),
                    headers={"Authorization": "Bearer admin-test"},
                )
            )
            try:
                assert await asyncio.to_thread(started.wait, 0.5)
                probe = await asyncio.wait_for(
                    client.get("/event-loop-probe"), timeout=0.5
                )
                assert probe.status_code == 200
                assert probe.json() == {"responsive": True}
            finally:
                release.set()
            response = await asyncio.wait_for(report_task, timeout=1)
            assert response.status_code == 200
            assert 'id="chart-0"' in response.text

    asyncio.run(exercise())


def test_status_api_route_is_served_before_root_spa_mount(tmp_path: Path) -> None:
    service = _service(tmp_path / "private", question_file=tmp_path / "missing.txt")
    app = _app(service, tmp_path / "static")
    with TestClient(app) as client:
        response = client.get(
            ROUTES["badmintonai_evaluation_status"],
            headers={"Authorization": "Bearer admin-test"},
        )
    assert response.status_code == 200
    assert response.json()["run_status"] == "idle"


def test_ui_only_exposes_safe_conversation_ids_for_local_chat_links() -> None:
    def question_view(conversation_id: str) -> dict[str, Any]:
        state = {
            "run_id": "a" * 32,
            "created_at": "2026-09-26T00:00:00Z",
            "updated_at": "2026-09-26T00:00:01Z",
            "manifest": {
                "question_source": {},
                "model_snapshot": {},
                "data_snapshot": {},
            },
            "questions": [
                {
                    "id": "1",
                    "prompt": "原題",
                    "status": "completed",
                    "conversation_id": conversation_id,
                    "retry_count": 0,
                    "elapsed_ms": 1,
                    "processing_elapsed_ms": 1,
                    "user_wait_ms": 0,
                    "usage_totals": {},
                    "turns": [],
                    "errors": [],
                }
            ],
        }
        return _state_for_ui(state)["questions"][0]

    assert question_view("chat-0123_abcd")["conversation_id"] == "chat-0123_abcd"
    assert question_view("../admin")["conversation_id"] is None
    assert question_view("https://attacker.example")["conversation_id"] is None


def _saved_partial_analysis_chart_result() -> dict[str, Any]:
    """以真實 renderer／adapter 建立合法部分產物，再模擬後續圖表失敗。"""
    embed = render_plotly_charts_html(
        validate_plotly_charts(
            {
                "schema_version": PLOTLY_SPEC_VERSION,
                "charts": [
                    {
                        "title": "擊球事件筆數",
                        "figure": {
                            "data": [{"type": "bar", "x": ["全部事件"], "y": [5191]}],
                            "layout": {},
                        },
                    }
                ],
            },
            native_validation=False,
        )
    )
    assistant = {
        "role": "assistant",
        "done": True,
        "content": "資料筆數分析與圖表已完成：共有 5,191 筆擊球事件；後續空間圖表尚未完成。",
        "embeds": [embed],
        "error": {"content": "互動圖表附加狀態不明且未讀到後續圖表；需要人工複核"},
        "output": [
            {
                "type": "function_call",
                "id": "analysis-ok",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "analysis-ok",
                "output": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {
                                "result_id": "a" * 48,
                                "artifacts": [
                                    {
                                        "relative_path": "event_count.json",
                                        "text_preview": '{"event_count":5191}',
                                    }
                                ],
                            }
                        ),
                    }
                ],
            },
        ],
    }
    result = _turn_result(
        {
            "chat": {
                "history": {
                    "messages": {
                        "user": {
                            "role": "user",
                            "content": "先計算資料筆數，再畫空間分布",
                        },
                        "assistant": assistant,
                    }
                }
            }
        },
        "user",
        "assistant",
        lambda _: False,
    )
    assert result is not None and result.charts
    return asdict(result)


@pytest.mark.parametrize(
    ("question_data", "expected_message", "expected_display_status"),
    [
        (
            {
                "status": "running",
                "pending_turn": {
                    "attempts": [
                        {
                            "error": {
                                "message": "Open WebUI API 回傳 HTTP 429 provider-secret",
                                "retryable": True,
                            },
                            "recovery_error": {
                                "message": "provider raw error with org-id-987",
                                "uncertain": True,
                            },
                        }
                    ]
                },
            },
            "服務暫時受限",
            "running",
        ),
        (
            {
                "status": "failed",
                "turns": [
                    {
                        "result": {
                            "messages": [
                                {
                                    "role": "assistant",
                                    "content": "資料筆數分析已完成：共有 5,191 筆擊球事件。球種比較尚未完成。",
                                }
                            ],
                            "tool_calls": [
                                {
                                    "name": "runPythonAnalysis",
                                    "status": "completed",
                                    "output": {
                                        "status": "ok",
                                        "files": [
                                            {
                                                "relative_path": "event_count.json",
                                                "text_preview": '{"event_count":5191}',
                                            }
                                        ],
                                    },
                                },
                                {"name": "runPythonAnalysis", "status": "failed"},
                            ],
                            "charts": [],
                        }
                    }
                ],
            },
            "Python 分析工具未成功完成",
            "failed",
        ),
        (
            {
                "status": "failed",
                "turns": [
                    {
                        "result": {
                            "messages": [
                                {
                                    "role": "assistant",
                                    "content": "已確認本次使用完整逐拍資料，欄位查詢尚未完成，未交付依賴該欄位的結論。",
                                }
                            ],
                            "tool_calls": [
                                {
                                    "name": "listColumnCatalog",
                                    "status": "in_progress",
                                }
                                for _ in range(16)
                            ],
                            "charts": [],
                        }
                    }
                ],
            },
            "本輪有未完成的工具呼叫",
            "failed",
        ),
        (
            {
                "status": "failed",
                "turns": [{"result": _saved_partial_analysis_chart_result()}],
            },
            "圖表附加狀態未確認",
            "failed",
        ),
        (
            {
                "status": "failed",
                "turns": [
                    {
                        "result": {
                            "messages": [{"role": "assistant", "content": ""}],
                            "tool_calls": [],
                            "charts": [],
                        }
                    }
                ],
            },
            "本輪沒有可驗證的回答",
            "failed",
        ),
        (
            {
                "status": "completed",
                "turns": [
                    {
                        "result": {
                            "messages": [
                                {
                                    "role": "assistant",
                                    "content": "已確認資料範圍；本輪分析工具未完成，需人工複核。",
                                }
                            ],
                            "tool_calls": [
                                {"name": "runPythonAnalysis", "status": "failed"}
                            ],
                            "charts": [],
                        }
                    }
                ],
            },
            "原 run 標記為完成",
            "needs_review",
        ),
    ],
)
def test_ui_failure_message_is_safe_and_prevents_completed_appearance(
    question_data: dict[str, Any],
    expected_message: str,
    expected_display_status: str,
) -> None:
    state = {
        "run_id": "f" * 32,
        "manifest": {
            "question_source": {},
            "model_snapshot": {},
            "data_snapshot": {},
        },
        "questions": [
            {
                "id": "1",
                "prompt": "原題",
                "retry_count": 0,
                "usage_totals": {},
                "turns": [],
                "errors": [],
                **question_data,
            }
        ],
    }

    question = _state_for_ui(state)["questions"][0]

    assert question["failure_message"].startswith(expected_message)
    assert question["display_status"] == expected_display_status
    assert "org-id-987" not in question["failure_message"]
    assert "provider-secret" not in question["failure_message"]
    assert "raw provider detail" not in question["failure_message"]
    saved_partial = (
        question_data["turns"][-1]["result"]["messages"][-1]["content"]
        if question_data["status"] == "failed"
        else ""
    )
    assert question["assistant_text"] == saved_partial
    assert "org-id-987" not in question["assistant_text"]
    assert "provider-secret" not in question["assistant_text"]
    assert "raw provider detail" not in question["assistant_text"]
    assert question["chart_count"] == sum(
        len(turn.get("result", {}).get("charts", []))
        for turn in question_data.get("turns", [])
    )
    if question_data["status"] == "completed":
        assert question["status"] == "completed"
        assert state["questions"][0]["status"] == "completed"


def test_ui_renders_known_render_failure_distinctly() -> None:
    state = {
        "run_id": "f" * 32,
        "manifest": {
            "question_source": {},
            "model_snapshot": {},
            "data_snapshot": {},
        },
        "questions": [
            {
                "id": "1",
                "prompt": "原題",
                "status": "failed",
                "retry_count": 0,
                "usage_totals": {},
                "turns": [
                    {
                        "result": {
                            "error": {
                                "code": "render_failed",
                                "message": "安全固定訊息",
                            },
                            "messages": [],
                            "tool_calls": [],
                            "charts": [],
                        }
                    }
                ],
                "errors": [],
            }
        ],
    }

    question = _state_for_ui(state)["questions"][0]
    assert question["display_status"] == "failed"
    assert (
        question["failure_message"] == "圖表產生或附加失敗；請查看原對話中的既有結果。"
    )


def test_ui_keeps_unknown_render_state_message_for_legacy_errors() -> None:
    state = {
        "run_id": "f" * 32,
        "manifest": {
            "question_source": {},
            "model_snapshot": {},
            "data_snapshot": {},
        },
        "questions": [
            {
                "id": "1",
                "prompt": "原題",
                "status": "failed",
                "retry_count": 0,
                "usage_totals": {},
                "turns": [
                    {
                        "result": {
                            "error": {
                                "message": "互動圖表的發布狀態無法確認；請先查看原對話是否已有圖表，避免重複發布。"
                            },
                            "messages": [],
                            "tool_calls": [],
                            "charts": [],
                        }
                    }
                ],
                "errors": [],
            }
        ],
    }

    question = _state_for_ui(state)["questions"][0]
    assert question["display_status"] == "failed"
    assert "圖表附加狀態未確認" in question["failure_message"]


def test_workbench_card_renders_projected_status_and_failure_as_text() -> None:
    source = (
        Path(__file__).parents[1] / "openwebui_functions" / "evaluation_workbench.js"
    ).read_text(encoding="utf-8")
    helper_start = source.index("  function displayRunStatus(")
    helper_end = source.index("\n  function usageText(", helper_start)
    run_status_helper = source[helper_start:helper_end]

    assert "question.display_status || question.status" in source
    assert 'question.display_status === "needs_review"' in source
    assert 'if (runStatus !== "completed") return runStatus;' in run_status_helper
    assert "displayRunStatus(state.run_status, run?.questions)" in source
    assert 'createText("p", question.failure_message, "notice error")' in source
    assert "element.textContent = text;" in source


@pytest.mark.parametrize(
    ("charts", "output_call_id", "expected_display_status"),
    [
        ([], "analysis-call-1", "needs_review"),
        ([{"id": "saved-chart"}], "analysis-call-1", "completed"),
        ([], "unrelated-call", "completed"),
    ],
)
def test_ui_projects_only_correlated_unattached_chart_failure(
    charts: list[dict[str, Any]],
    output_call_id: str,
    expected_display_status: str,
) -> None:
    assistant = {
        "role": "assistant",
        "content": "已完成分析。",
        "output": [
            {
                "type": "function_call",
                "id": "analysis-call-1",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": output_call_id,
                "status": "completed",
                "output": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {"rich_ui_status": "embed_unknown"},
                            ensure_ascii=False,
                        ),
                    }
                ],
            },
        ],
    }
    state = {
        "run_id": "9" * 32,
        "manifest": {
            "question_source": {},
            "model_snapshot": {},
            "data_snapshot": {},
        },
        "questions": [
            {
                "id": "1",
                "prompt": "圖表題",
                "status": "completed",
                "retry_count": 0,
                "usage_totals": {},
                "turns": [
                    {
                        "result": {
                            "messages": [assistant],
                            "tool_calls": [assistant["output"][0]],
                            "charts": charts,
                        }
                    }
                ],
                "errors": [],
            }
        ],
    }

    question = _state_for_ui(state)["questions"][0]

    assert question["status"] == "completed"
    assert question["display_status"] == expected_display_status
    if expected_display_status == "needs_review":
        assert question["failure_message"].startswith("原 run 標記為完成")
        assert "圖表附加狀態未確認" in question["failure_message"]
        assert question["assistant_text"] == ""
    else:
        assert question["failure_message"] is None
        assert question["assistant_text"] == "已完成分析。"


def test_failed_question_preserves_original_assistant_stop_message() -> None:
    stop_message = "分析未完成；已停止自動修正，本次不提供未驗證的統計數值。"
    call = {
        "type": "function_call",
        "id": "analysis-limit",
        "call_id": "analysis-limit",
        "name": "runPythonAnalysis",
        "status": "failed",
    }
    assistant = {
        "role": "assistant",
        "content": stop_message,
        "output": [
            call,
            {
                "type": "function_call_output",
                "call_id": "analysis-limit",
                "status": "failed",
                "output": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {
                                "code": "analysis_message_limit",
                                "details": {"terminal": True},
                            }
                        ),
                    }
                ],
            },
        ],
    }
    state = {
        "run_id": "f" * 32,
        "manifest": {
            "question_source": {},
            "model_snapshot": {},
            "data_snapshot": {},
        },
        "questions": [
            {
                "id": "64",
                "prompt": "原始分析題",
                "status": "failed",
                "retry_count": 0,
                "usage_totals": {},
                "turns": [
                    {
                        "result": {
                            "messages": [assistant],
                            "tool_calls": [call],
                            "charts": [],
                        }
                    }
                ],
                "errors": [],
            }
        ],
    }

    overview = _state_for_ui(state)
    question = overview["questions"][0]

    assert question["status"] == "failed"
    assert question["display_status"] == "failed"
    assert "分析執行次數已達上限" in question["failure_message"]
    assert question["assistant_text"] == stop_message


def test_ui_prefers_completed_clarification_tool_question_and_choices() -> None:
    assistant = {
        "role": "assistant",
        "content": "模型的非結構化說明",
        "output": [
            {
                "type": "function_call",
                "name": "requestClarification",
                "status": "completed",
                "arguments": json.dumps(
                    {"question": "長回合至少幾拍？", "options": ["10 拍", "12 拍"]},
                    ensure_ascii=False,
                ),
            }
        ],
    }
    state = {
        "run_id": "a" * 32,
        "created_at": "2026-09-26T00:00:00Z",
        "updated_at": "2026-09-26T00:00:01Z",
        "manifest": {"question_source": {}, "model_snapshot": {}, "data_snapshot": {}},
        "questions": [
            {
                "id": "1",
                "prompt": "原題",
                "status": "awaiting_clarification",
                "usage_totals": {},
                "turns": [
                    {
                        "result": {
                            "messages": [assistant],
                            "tool_calls": assistant["output"],
                            "charts": [],
                        }
                    }
                ],
                "errors": [],
            }
        ],
    }

    question = _state_for_ui(state)["questions"][0]
    assert question["assistant_text"] == "長回合至少幾拍？"
    assert question["choices"] == ["10 拍", "12 拍"]


def test_completed_clarification_without_options_remains_free_input() -> None:
    assistant = {
        "role": "assistant",
        "content": "非結構化說明",
        "output": [
            {
                "type": "function_call",
                "name": "requestClarification",
                "status": "completed",
                "arguments": json.dumps({"question": "請指定比賽場次。"}),
            }
        ],
    }
    state = {
        "run_id": "b" * 32,
        "manifest": {"question_source": {}, "model_snapshot": {}, "data_snapshot": {}},
        "clarification_policy_version": "requestClarification-event-v1",
        "questions": [
            {
                "id": "1",
                "prompt": "原題",
                "status": "awaiting_clarification",
                "turns": [
                    {
                        "result": {
                            "messages": [assistant],
                            "tool_calls": assistant["output"],
                            "charts": [],
                        }
                    }
                ],
                "errors": [],
            }
        ],
    }

    question = _state_for_ui(state)["questions"][0]

    assert question["assistant_text"] == "請指定比賽場次。"
    assert question["choices"] == []
    assert question["clarification_review_candidate"] is False


def test_legacy_missed_clarification_annotation_never_rewrites_frozen_checkpoint(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "private")
    run_id = "d" * 32
    service.runs_dir.mkdir(parents=True, exist_ok=True)
    assistant = {"role": "assistant", "content": "您指的是哪一場比賽？"}
    state = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": "2026-09-27T00:00:00Z",
        "updated_at": "2026-09-27T00:00:01Z",
        "manifest": {
            "question_source": {"name": "questions.txt", "sha256": "a" * 64},
            "model_snapshot": {"model_id": "badmintonai"},
            "data_snapshot": {"snapshot_id": "snapshot-v1"},
        },
        "questions": [
            {
                "id": "14",
                "prompt": "原始題目文字。",
                "status": "completed",
                "conversation_id": "chat-q14",
                "turns": [
                    {
                        "kind": "question",
                        "request": "原始題目文字。",
                        "result": {
                            "messages": [assistant],
                            "tool_calls": [],
                            "charts": [],
                        },
                        "attempts": [],
                    }
                ],
                "errors": [],
                "usage_totals": {},
            }
        ],
    }
    checkpoint = service.runs_dir / f"{run_id}.json"
    checkpoint.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    original_bytes = checkpoint.read_bytes()

    before = service.get_run(run_id)
    assert before["run"]["questions"][0]["status"] == "completed"
    assert before["run"]["questions"][0]["clarification_review_candidate"] is True
    assert before["run"]["questions"][0]["clarification_review_source"] == (
        "legacy_text_hint"
    )

    annotated = service.annotate_classification(
        run_id,
        "14",
        "missed_clarification",
        "原聊天確實只追問；人工確認為漏判，補答在外部完成。",
        "admin-42",
    )

    question = annotated["run"]["questions"][0]
    assert question["status"] == "completed"
    assert question["classification_annotation"]["classification"] == (
        "missed_clarification"
    )
    assert question["classification_annotation"]["actor"] == "admin-42"
    assert checkpoint.read_bytes() == original_bytes
    assert (
        len(json.loads(checkpoint.read_text(encoding="utf-8"))["questions"][0]["turns"])
        == 1
    )

    downloaded = json.loads(service.download_json(run_id).decode("utf-8"))
    summary = service.download_summary(run_id).decode("utf-8")
    assert downloaded["questions"][0]["status"] == "completed"
    assert downloaded["classification_annotations"][0]["reason"].startswith("原聊天")
    assert "管理員分類註記（外掛標註；不改寫 checkpoint）" in summary
    assert "admin-42" in summary
    assert service.recent_runs()[0]["classification_annotation_count"] == 1


def test_answered_response_with_followup_question_is_not_a_review_candidate() -> None:
    assistant = {
        "role": "assistant",
        "content": "Q44 統計結果為 31.8%。還要看其他場次嗎？",
        "output": [
            {
                "type": "function_call",
                "name": "runPythonAnalysis",
                "status": "completed",
            }
        ],
    }
    state = {
        "run_id": "e" * 32,
        "manifest": {"question_source": {}, "model_snapshot": {}, "data_snapshot": {}},
        "questions": [
            {
                "id": "44",
                "prompt": "Q44 原題",
                "status": "completed",
                "turns": [
                    {
                        "result": {
                            "messages": [assistant],
                            "tool_calls": assistant["output"],
                            "charts": [],
                        }
                    }
                ],
                "errors": [],
            }
        ],
    }

    question = _state_for_ui(state)["questions"][0]

    assert question["clarification_review_candidate"] is False
    assert question["status"] == "completed"


def test_admin_can_annotate_a_legacy_miss_without_a_text_hint(tmp_path: Path) -> None:
    service = _service(tmp_path / "private")
    run_id = "c" * 32
    service.runs_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": 1,
        "run_id": run_id,
        "manifest": {"question_source": {}, "model_snapshot": {}, "data_snapshot": {}},
        "questions": [
            {
                "id": "16",
                "prompt": "需要人工判讀的原題",
                "status": "completed",
                "turns": [
                    {
                        "result": {
                            "messages": [
                                {"role": "assistant", "content": "暫以預設範圍分析。"}
                            ],
                            "tool_calls": [],
                            "charts": [],
                        }
                    }
                ],
            }
        ],
    }
    checkpoint = service.runs_dir / f"{run_id}.json"
    checkpoint.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    original = checkpoint.read_bytes()
    before = service.get_run(run_id)["run"]["questions"][0]
    assert before["clarification_review_candidate"] is False

    annotated = service.annotate_classification(
        run_id,
        "16",
        "missed_clarification",
        "人工檢查原對話後確認當時應先追問；補答已在原聊天完成。",
        "admin-42",
    )

    question = annotated["run"]["questions"][0]
    assert question["status"] == "completed"
    assert question["classification_annotation"]["classification"] == (
        "missed_clarification"
    )
    assert checkpoint.read_bytes() == original


def test_blocking_start_does_not_block_same_app_async_endpoint() -> None:
    service = BlockingStartWorkbench()
    app = FastAPI()
    app.include_router(create_api_router(service, _admin_dependency))

    @app.get("/event-loop-probe")
    async def event_loop_probe() -> dict[str, bool]:
        return {"responsive": True}

    async def exercise() -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            started_at = asyncio.get_running_loop().time()
            start_task = asyncio.create_task(
                client.post(
                    ROUTES["badmintonai_evaluation_start"],
                    json={"source": "v2-natural-100", "sha256": "a" * 64},
                    headers={
                        "Authorization": "Bearer admin-test",
                        "Origin": "http://testserver",
                    },
                )
            )
            try:
                assert await asyncio.to_thread(service.started.wait, 0.5)
                probe = await asyncio.wait_for(
                    client.get("/event-loop-probe"), timeout=0.5
                )
                elapsed = asyncio.get_running_loop().time() - started_at
                assert probe.status_code == 200
                assert probe.json() == {"responsive": True}
                assert elapsed < 1.0
            finally:
                service.release.set()
            start_response = await asyncio.wait_for(start_task, timeout=1.0)
            assert start_response.status_code == 200
            assert start_response.json() == {"started": True}

    asyncio.run(exercise())


def test_start_stop_resume_is_single_worker_persistent_and_never_repeats_completed_q(
    tmp_path: Path,
) -> None:
    client = FakeClient()
    client.block_first = True
    question_file = _questions_file(tmp_path / "source.txt")
    storage = tmp_path / "private"
    service = _service(storage, question_file=question_file, client=client)
    source_hash = hashlib.sha256(question_file.read_bytes()).hexdigest()

    started = service.start_source("v2-natural-100", source_hash)
    assert started["run_status"] == "running"
    assert client.started.wait(timeout=1)
    queued_service = _service(storage, question_file=question_file, client=client)
    queued_result: dict[str, Any] = {}
    queued_errors: list[BaseException] = []

    def enqueue_another_run() -> None:
        try:
            queued_result.update(
                queued_service.start_source("v2-natural-100", source_hash)
            )
        except BaseException as exc:
            queued_errors.append(exc)

    start_thread = threading.Thread(target=enqueue_another_run)
    start_thread.start()
    _wait_until(
        lambda: any(
            item.get("run_id") != started["run_id"]
            for item in json.loads(
                service._clarification_queue_path.read_text(encoding="utf-8")
            )["items"]
        )
    )

    stopping = service.stop(expected_run_id=started["run_id"])
    assert stopping["run_status"] == "stopping"
    client.release.set()
    start_thread.join(timeout=5)
    assert not start_thread.is_alive()
    assert queued_errors == []
    queued_run_id = queued_result["run_id"]
    assert queued_run_id != started["run_id"]
    assert queued_result["run_status"] == "queued"
    _wait_until(lambda: not service._worker_is_running())
    stopped = service.status(started["run_id"])
    assert stopped["run_status"] == "stopped"
    run_id = started["run_id"]
    assert service.status(queued_run_id)["run_status"] == "completed"

    resumed = service.resume(run_id)
    assert resumed["run_status"] in {"running", "completed"}
    _wait_until(lambda: not service._worker_is_running())
    final = service.status(run_id)
    assert final["run_status"] == "completed"
    assert len(client.sent) == 4
    assert [item["status"] for item in final["run"]["questions"]] == [
        "completed",
        "completed",
    ]
    saved = json.loads(service.download_json(run_id))
    assert saved["questions"][0]["turns"][0]["request"] == "第一題原文？"


def test_original_q1_to_q3_precede_busy_clarification_and_retry(tmp_path: Path) -> None:
    class QueueOrderClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.clarify_first = True
            self.q3_started = threading.Event()
            self.q3_release = threading.Event()
            self.q2_calls = 0

        def send_turn(
            self, conversation_id: str, content: str, operation_id: str
        ) -> TurnResult:
            if content == "Q3 原題":
                self.sent.append((conversation_id, content))
                self.q3_started.set()
                self.q3_release.wait(timeout=5)
                return TurnResult(
                    messages=[{"role": "assistant", "content": "Q3 完成"}]
                )
            if content == "Q2 原題":
                self.sent.append((conversation_id, content))
                self.q2_calls += 1
                if self.q2_calls == 1:
                    return TurnResult(
                        error={"message": "429 rate limit", "retryable": False}
                    )
                return TurnResult(
                    messages=[{"role": "assistant", "content": "Q2 重試成功"}]
                )
            if content == "Q1 補答":
                self.sent.append((conversation_id, content))
                return TurnResult(
                    messages=[{"role": "assistant", "content": "Q1 補答完成"}]
                )
            return super().send_turn(conversation_id, content, operation_id)

    client = QueueOrderClient()
    source = _questions_file(
        tmp_path / "ordered.txt", "1: Q1 原題\n2: Q2 原題\n3: Q3 原題\n"
    )
    service = _service(tmp_path / "private", question_file=source, client=client)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    run_id = service.start_source("v2-natural-100", digest)["run_id"]
    assert client.q3_started.wait(timeout=3)

    clarification = service.clarify("1", "Q1 補答", run_id=run_id)
    retry = service.retry_failed(run_id, "2", expected_retry_count=0)
    assert clarification["already_queued"] is False
    assert retry["already_queued"] is False
    assert [
        (item["question_id"], item.get("work_type"), item["status"])
        for item in service._clarification_queue_items(run_id)
    ] == [
        ("3", "question", "executing"),
        ("1", "clarification", "queued"),
        ("2", "retry", "queued"),
    ]

    client.q3_release.set()
    _wait_for_worker_idle(service)
    assert [content for _conversation_id, content in client.sent] == [
        "Q1 原題",
        "Q2 原題",
        "Q3 原題",
        "Q1 補答",
        "Q2 原題",
    ]
    completed = service.status(run_id)["run"]["questions"]
    assert [question["status"] for question in completed] == [
        "completed",
        "completed",
        "completed",
    ]
    assert completed[1]["manual_retry_succeeded"] is True


def test_new_run_keeps_paused_run_selectable_and_stale_stop_cannot_switch_pointer(
    tmp_path: Path,
) -> None:
    client = FakeClient()
    client.block_first = True
    question_file = _questions_file(tmp_path / "source.txt")
    storage = tmp_path / "private"
    service = _service(storage, question_file=question_file, client=client)
    source_hash = hashlib.sha256(question_file.read_bytes()).hexdigest()

    first = service.start_source("v2-natural-100", source_hash)
    first_run_id = first["run_id"]
    assert client.started.wait(timeout=1)
    service.stop(expected_run_id=first_run_id)
    client.release.set()
    _wait_for_worker_idle(service)
    first_checkpoint = storage / "runs" / f"{first_run_id}.json"
    paused_bytes = first_checkpoint.read_bytes()

    second = service.start_source("v2-natural-100", source_hash)
    second_run_id = second["run_id"]
    assert second_run_id != first_run_id
    _wait_for_worker_idle(service)
    assert first_checkpoint.read_bytes() == paused_bytes
    assert (storage / "runs" / f"{second_run_id}.json").is_file()

    selected = service.status(first_run_id)
    assert selected["run_id"] == first_run_id
    assert selected["active_run_id"] == second_run_id
    assert selected["can_resume"] is True
    with pytest.raises(WorkbenchError, match="run 已切換"):
        service.stop(expected_run_id=first_run_id)
    assert service._read_pointer()["run_id"] == second_run_id
    app = _app(service, tmp_path / "static")
    with TestClient(app) as http:
        selected_response = http.get(
            ROUTES["badmintonai_evaluation_status"],
            params={"run_id": first_run_id},
            headers={"Authorization": "Bearer admin-test"},
        )
        stale_stop_response = http.post(
            ROUTES["badmintonai_evaluation_stop"],
            json={"run_id": first_run_id},
            headers={
                **_post_headers(),
                "Authorization": "Bearer admin-test",
            },
        )
    assert selected_response.status_code == 200
    assert selected_response.json()["run_id"] == first_run_id
    assert selected_response.json()["active_run_id"] == second_run_id
    assert stale_stop_response.status_code == 409
    assert service._read_pointer()["run_id"] == second_run_id

    resumed = service.resume(first_run_id)
    assert resumed["run_id"] == first_run_id
    _wait_for_worker_idle(service)
    assert service.status()["run_id"] == first_run_id
    assert service.status()["run_status"] == "completed"
    assert (storage / "runs" / f"{second_run_id}.json").is_file()


def test_queued_run_can_pause_and_resume_while_another_run_is_executing(
    tmp_path: Path,
) -> None:
    client = FakeClient()
    client.block_first = True
    question_file = _questions_file(tmp_path / "source.txt", "1: Q1 原題\n2: Q2 原題\n")
    storage = tmp_path / "private"
    active_service = _service(storage, question_file=question_file, client=client)
    digest = hashlib.sha256(question_file.read_bytes()).hexdigest()
    active = active_service.start_source("v2-natural-100", digest)
    active_run_id = active["run_id"]
    assert client.started.wait(timeout=1)

    queued_service = _service(storage, question_file=question_file, client=client)
    queued = queued_service.start_source("v2-natural-100", digest)
    queued_run_id = queued["run_id"]
    assert queued["run_status"] == "queued"
    assert queued_service._read_pointer()["run_id"] == active_run_id
    before_pause = queued_service.status(queued_run_id)
    assert before_pause["worker_running"] is True
    assert before_pause["can_stop"] is True
    assert before_pause["can_resume"] is False

    app = _app(queued_service, tmp_path / "static")
    admin = {**_post_headers(), "Authorization": "Bearer admin-test"}
    with TestClient(app) as http:
        paused = http.post(
            ROUTES["badmintonai_evaluation_stop"],
            json={"run_id": queued_run_id},
            headers=admin,
        )
        assert paused.status_code == 200
        paused_state = paused.json()
        assert paused_state["run_id"] == queued_run_id
        assert paused_state["active_run_id"] == active_run_id
        assert paused_state["worker_running"] is True
        assert paused_state["run_status"] == "stopped"
        assert paused_state["can_resume"] is True
        assert queued_service._read_pointer()["run_id"] == active_run_id

        resumed = http.post(
            ROUTES["badmintonai_evaluation_resume"],
            json={"run_id": queued_run_id},
            headers=admin,
        )
        assert resumed.status_code == 200
        resumed_state = resumed.json()
        assert resumed_state["run_id"] == queued_run_id
        assert resumed_state["active_run_id"] == active_run_id
        assert resumed_state["worker_running"] is True
        assert resumed_state["run_status"] == "queued"
        assert resumed_state["can_resume"] is False
        assert queued_service._read_pointer()["run_id"] == active_run_id

    client.release.set()
    _wait_for_worker_idle(active_service)
    _wait_for_worker_idle(queued_service)
    assert active_service.status(active_run_id)["run_status"] == "completed"
    assert queued_service.status(queued_run_id)["run_status"] == "completed"
    assert len(client.sent) == 4


def test_terminal_failed_run_with_queued_retry_can_be_paused(tmp_path: Path) -> None:
    service, _fake, run_id = _failed_retry_service(tmp_path)
    lease = service._try_lease()
    assert lease is not None
    try:
        accepted = service.retry_failed(run_id, "1", expected_retry_count=0)
        assert accepted["accepted"] is True
        assert service._read_state(run_id)["questions"][0]["status"] == "failed"
        assert service.status(run_id)["can_stop"] is True

        app = _app(service, tmp_path / "static")
        with TestClient(app) as http:
            response = http.post(
                ROUTES["badmintonai_evaluation_stop"],
                json={"run_id": run_id},
                headers={
                    **_post_headers(),
                    "Authorization": "Bearer admin-test",
                },
            )
        assert response.status_code == 200
        paused = response.json()
        assert paused["run_id"] == run_id
        assert paused["can_resume"] is True
        assert paused["run"]["questions"][0]["work_type"] == "retry"
        assert service._run_is_paused(run_id) is True
    finally:
        service._release_lease(lease)

    stopped = service.status(run_id)
    assert stopped["run_status"] == "stopped"
    assert stopped["can_resume"] is True
    assert service._worker_is_running() is False
    assert service._queue_has_work(run_id) is True


def test_unanswered_run_can_be_clarified_after_a_new_run_finishes(
    tmp_path: Path,
) -> None:
    client = FakeClient()
    client.clarify_first = True
    question_file = _questions_file(tmp_path / "source.txt")
    service = _service(tmp_path / "private", question_file=question_file, client=client)
    source_hash = hashlib.sha256(question_file.read_bytes()).hexdigest()

    first = service.start_source("v2-natural-100", source_hash)
    first_run_id = first["run_id"]
    _wait_for_worker_idle(service)
    assert service.status(first_run_id)["run_status"] == "awaiting_clarification"

    second = service.start_source("v2-natural-100", source_hash)
    second_run_id = second["run_id"]
    _wait_for_worker_idle(service)
    assert service.status()["run_id"] == second_run_id

    clarified = service.clarify("1", "選項 A", run_id=first_run_id)
    assert clarified["run_id"] == first_run_id
    _wait_for_worker_idle(service)
    first_question = next(
        question
        for question in service.status()["run"]["questions"]
        if question["id"] == "1"
    )
    assert first_question["status"] == "completed"
    assert client.sent[-1] == ("chat-1", "選項 A")
    assert (service.runs_dir / f"{second_run_id}.json").is_file()


def test_question_clarification_keeps_original_message_choices_and_same_chat(
    tmp_path: Path,
) -> None:
    client = FakeClient()
    client.clarify_first = True
    source = _questions_file(tmp_path / "questions.txt")
    storage = tmp_path / "private"
    service = _service(storage, question_file=source, client=client)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    service.start_source("v2-natural-100", digest)
    _wait_until(lambda: not service._worker_is_running())

    restarted = _service(storage, question_file=source, client=client)
    assert restarted.recover_after_restart() is False
    state = restarted.status()
    waiting = next(q for q in state["run"]["questions"] if q["id"] == "1")
    assert waiting["status"] == "awaiting_clarification"
    assert waiting["assistant_text"] == "請選擇 A 或 B"
    assert waiting["choices"] == ["選項 A", "選項 B"]
    original_chat = dict(client.created)["1"]

    restarted.clarify("1", "選項 A")
    _wait_until(lambda: not restarted._worker_is_running())
    updated = restarted.status()
    first_question = next(q for q in updated["run"]["questions"] if q["id"] == "1")
    assert first_question["status"] == "completed", updated["last_error"]
    assert client.sent[-1] == (original_chat, "選項 A")
    assert updated["run_status"] == "completed"

    summary = restarted.download_summary(updated["run_id"]).decode("utf-8")
    assert "第 1 輪原始提問：\n第一題原文？" in summary
    assert "第 2 輪補答：\n選項 A" in summary
    assert "admin-test-secret" not in summary


def test_restart_recovers_saved_pending_run_and_downloads_keep_missing_usage_null(
    tmp_path: Path,
) -> None:
    client = FakeClient(answer="回覆含 admin-test-secret")
    source = _questions_file(tmp_path / "restart.txt", "1: 重啟後續跑？\n")
    storage = tmp_path / "private"
    service = _service(storage, question_file=source, client=client)
    run_id = "a" * 32
    runner = EvaluationRunner.start(
        client,
        question_file=source,
        store_path=storage / "runs" / f"{run_id}.json",
        model_snapshot=client.get_model_snapshot(),
        data_snapshot={"snapshot_id": "sha256:dataset-snapshot-v1"},
        run_id=run_id,
    )
    service._write_pointer(run_id, paused=False)

    recovered = _service(storage, question_file=source, client=client)
    assert recovered.recover_after_restart() is False
    assert recovered.status(run_id)["run"]["questions"][0]["status"] == "pending"
    assert client.sent == []
    recovered.resume(run_id)
    _wait_until(lambda: not recovered._worker_is_running())
    json_download = recovered.download_json(run_id).decode("utf-8")
    summary_download = recovered.download_summary(run_id).decode("utf-8")
    status_body = json.dumps(recovered.status(), ensure_ascii=False)

    assert recovered.status(run_id)["run_status"] == "completed"
    assert runner.snapshot()["questions"][0]["turns"][0]["result"]["usage"] is None
    assert "admin-test-secret" not in json_download
    assert "admin-test-secret" not in summary_download
    assert "admin-test-secret" not in status_body
    assert "admin-test-secret" not in (
        tmp_path / "private" / "runs" / f"{run_id}.json"
    ).read_text(encoding="utf-8")
    assert '"usage": null' in json_download
    assert "尚未取得模型實際用量" in summary_download


def test_upload_start_requires_matching_preview_digest_and_persists_private_state(
    tmp_path: Path,
) -> None:
    data = b"1: Upload question?\n"
    digest = hashlib.sha256(data).hexdigest()
    client = FakeClient()
    service = _service(
        tmp_path / "private",
        question_file=tmp_path / "missing.txt",
        client=client,
    )

    with pytest.raises(WorkbenchError, match="預覽後已改變"):
        service.start_upload("upload.txt", data, "0" * 64)
    result = service.start_upload("upload.txt", data, digest)
    _wait_until(lambda: not service._worker_is_running())

    run_file = tmp_path / "private" / "runs" / f"{result['run_id']}.json"
    assert run_file.is_file()
    assert list((tmp_path / "private" / "staging").iterdir()) == []
    assert "admin-test-secret" not in run_file.read_text(encoding="utf-8")


def test_start_routes_execute_only_selected_questions_and_preserve_source_manifest(
    tmp_path: Path,
) -> None:
    source_bytes = "1:  第一題原文  \r\n\r\n3: 第三題原文？\n8: 第八題。\n".encode(
        "utf-8"
    )
    official = tmp_path / "評估問題_v2.txt"
    official.write_bytes(source_bytes)
    storage = tmp_path / "private"
    client = FakeClient()
    service = _service(storage, question_file=official, client=client)
    app = _app(service, tmp_path / "static")

    with TestClient(app) as api_client:
        headers = {**_post_headers(), "Authorization": "Bearer admin-test"}
        preview = api_client.post(
            ROUTES["badmintonai_evaluation_preview"],
            json={"source": "v2-natural-100"},
            headers=headers,
        )
        invalid_start = api_client.post(
            ROUTES["badmintonai_evaluation_start"],
            json={
                "source": "v2-natural-100",
                "sha256": preview.json()["sha256"],
                "question_ids": ["77"],
            },
            headers=headers,
        )
        assert preview.status_code == 200
        assert invalid_start.status_code == 400
        if storage.exists():
            assert not list((storage / "runs").glob("*.json"))
        source_start = api_client.post(
            ROUTES["badmintonai_evaluation_start"],
            json={
                "source": "v2-natural-100",
                "sha256": preview.json()["sha256"],
                "question_ids": ["8", "1"],
            },
            headers=headers,
        )
        assert source_start.status_code == 200, source_start.text
        _wait_until(lambda: not service._worker_is_running())
        source_run_id = source_start.json()["run_id"]
        source_state = json.loads(
            (storage / "runs" / f"{source_run_id}.json").read_text(encoding="utf-8")
        )

        upload_bytes = b"2: Uploaded two?\n\n5: Uploaded five?\n9: Uploaded nine?\n"
        upload_hash = hashlib.sha256(upload_bytes).hexdigest()
        upload_headers = {
            **headers,
            "Content-Type": "text/plain; charset=utf-8",
            "X-Upload-Filename": "selected.txt",
            "X-Expected-SHA256": upload_hash,
            "X-Selected-Question-Ids": json.dumps(["9", "5"]),
        }
        invalid_upload = api_client.post(
            ROUTES["badmintonai_evaluation_start_upload"],
            content=upload_bytes,
            headers={**upload_headers, "X-Selected-Question-Ids": '["5","5"]'},
        )
        assert invalid_upload.status_code == 400
        assert len(list((storage / "runs").glob("*.json"))) == 1
        upload_start = api_client.post(
            ROUTES["badmintonai_evaluation_start_upload"],
            content=upload_bytes,
            headers=upload_headers,
        )

    assert upload_start.status_code == 200, upload_start.text
    _wait_until(lambda: not service._worker_is_running())
    upload_run_id = upload_start.json()["run_id"]
    upload_state = json.loads(
        (storage / "runs" / f"{upload_run_id}.json").read_text(encoding="utf-8")
    )

    assert source_state["manifest"]["question_source"] == {
        "name": official.name,
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
        "question_count": 3,
        "selected_question_ids": ["1", "8"],
    }
    assert [item["id"] for item in source_state["questions"]] == ["1", "8"]
    assert [item["source_line"] for item in source_state["questions"]] == [1, 4]
    assert [item["prompt"] for item in source_state["questions"]] == [
        " 第一題原文  ",
        "第八題。",
    ]
    assert [content for _conversation, content in client.sent[:2]] == [
        " 第一題原文  ",
        "第八題。",
    ]
    assert upload_state["manifest"]["question_source"] == {
        "name": "selected.txt",
        "sha256": upload_hash,
        "question_count": 3,
        "selected_question_ids": ["5", "9"],
    }
    assert [item["id"] for item in upload_state["questions"]] == ["5", "9"]
    assert [item["source_line"] for item in upload_state["questions"]] == [3, 4]
    assert [content for _conversation, content in client.sent[2:]] == [
        "Uploaded five?",
        "Uploaded nine?",
    ]


@pytest.mark.parametrize(
    ("selected_question_ids", "message"),
    [
        ([], "至少選取一題"),
        (["1", "1"], "不可重複"),
        (["99"], "不存在"),
        ([1], "格式無效"),
    ],
)
def test_invalid_question_selection_is_rejected_before_run_or_client_creation(
    tmp_path: Path,
    selected_question_ids: list[Any],
    message: str,
) -> None:
    source = _questions_file(tmp_path / "questions.txt")
    storage = tmp_path / "private"
    client = FakeClient()
    client_factory_calls: list[bool] = []
    service = EvaluationWorkbenchService(
        storage_dir=storage,
        question_file=source,
        client_factory=lambda: (client_factory_calls.append(True), client)[1],
        data_snapshot_provider=lambda: {"snapshot_id": "unused"},
        model_snapshot_provider=lambda _client: {"model_id": "unused"},
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    with pytest.raises(WorkbenchError, match=message):
        service.start_source(
            "v2-natural-100", digest, selected_question_ids=selected_question_ids
        )

    assert client_factory_calls == []
    assert client.created == []
    assert not storage.exists()


def test_selected_start_rechecks_original_source_digest_before_side_effects(
    tmp_path: Path,
) -> None:
    source = _questions_file(tmp_path / "questions.txt")
    storage = tmp_path / "private"
    service = _service(storage, question_file=source)
    preview = service.preview_source("v2-natural-100")
    _questions_file(source, "1: 改動後第一題\n2: 改動後第二題\n")

    with pytest.raises(WorkbenchError, match="預覽後已改變"):
        service.start_source(
            "v2-natural-100",
            preview["sha256"],
            selected_question_ids=["1"],
        )

    assert not storage.exists()


def _service_with_two_waiting_questions(
    tmp_path: Path, *, client: FakeClient | None = None
):
    source = _questions_file(
        tmp_path / "waiting-questions.txt",
        "1: 等待補答第一題\n2: 等待補答第二題\n",
    )
    client = client or FakeClient()
    client.clarify_all_first = True
    service = _service(tmp_path / "private", question_file=source, client=client)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    run_id = service.start_source("v2-natural-100", digest)["run_id"]
    _wait_for_worker_idle(service)
    assert [item["status"] for item in service.status(run_id)["run"]["questions"]] == [
        "awaiting_clarification",
        "awaiting_clarification",
    ]
    client.clarify_all_first = False
    return service, client, source, run_id


def test_clarifications_enqueue_while_busy_and_drain_fifo_at_question_boundary(
    tmp_path: Path,
) -> None:
    service, client, source, waiting_run_id = _service_with_two_waiting_questions(
        tmp_path
    )
    waiting_checkpoint = service.runs_dir / f"{waiting_run_id}.json"
    checkpoint_before = waiting_checkpoint.read_bytes()

    client.started.clear()
    client.block_content = "等待補答第一題"
    busy = service.start_source(
        "v2-natural-100", hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert client.started.wait(timeout=2)
    _wait_until(lambda: service._worker_is_running())
    try:
        queued_q2 = service.clarify("2", "第二題先補", run_id=waiting_run_id)
        queued_q1 = service.clarify("1", "第一題後補", run_id=waiting_run_id)
        assert queued_q2["run"]["queued_clarification_count"] == 1
        assert queued_q1["run"]["queued_clarification_count"] == 2
        assert waiting_checkpoint.read_bytes() == checkpoint_before
        repeated = service.clarify("2", "重複補答", run_id=waiting_run_id)
        assert repeated["already_queued"] is True
    finally:
        client.release.set()

    _wait_for_worker_idle(service)
    queued_answers = [
        content
        for _conversation_id, content in client.sent
        if content in {"第二題先補", "第一題後補"}
    ]
    assert queued_answers == ["第二題先補", "第一題後補"]
    assert service.status(waiting_run_id)["run"]["queued_clarification_count"] == 0
    assert service.status(busy["run_id"])["run_status"] == "completed"


def test_queued_clarification_can_be_edited_or_cancelled_without_checkpoint_write(
    tmp_path: Path,
) -> None:
    service, _client, _source, run_id = _service_with_two_waiting_questions(tmp_path)
    checkpoint = service.runs_dir / f"{run_id}.json"
    original_checkpoint = checkpoint.read_bytes()
    lease = service._try_lease()
    assert lease is not None
    try:
        saved = service.clarify("1", "原補答", run_id=run_id)
        question = saved["run"]["questions"][0]
        assert question["display_status"] == "queued"
        assert question["queued_clarification"]["answer"] == "原補答"
        assert checkpoint.read_bytes() == original_checkpoint
        repeated = service.clarify("1", "重複補答", run_id=run_id)
        assert repeated["already_queued"] is True

        updated = service.update_queued_clarification(run_id, "1", "修改後補答")
        assert updated["run"]["questions"][0]["queued_clarification"]["answer"] == (
            "修改後補答"
        )
        cancelled = service.cancel_queued_clarification(run_id, "1")
        assert cancelled["run"]["queued_clarification_count"] == 0
        assert cancelled["run"]["questions"][0]["status"] == ("awaiting_clarification")
        service.clarify("1", "API 原補答", run_id=run_id)
        update_path = ROUTES[
            "badmintonai_evaluation_clarification_queue_update"
        ].format(run_id=run_id, question_id="1")
        cancel_path = ROUTES[
            "badmintonai_evaluation_clarification_queue_cancel"
        ].format(run_id=run_id, question_id="1")
        app = _app(service, tmp_path / "static")
        with TestClient(app) as http:
            missing_origin_update = http.patch(
                update_path,
                json={"answer": "不應保存"},
                headers={"Authorization": "Bearer admin-test"},
            )
            foreign_origin_cancel = http.delete(
                cancel_path,
                headers={
                    "Authorization": "Bearer admin-test",
                    "Origin": "https://attacker.example",
                },
            )
            updated_response = http.patch(
                update_path,
                json={"answer": "API 修改後補答"},
                headers={
                    **_post_headers(),
                    "Authorization": "Bearer admin-test",
                },
            )
            cancelled_response = http.delete(
                cancel_path,
                headers={
                    **_post_headers(),
                    "Authorization": "Bearer admin-test",
                },
            )
        assert updated_response.status_code == 200
        assert missing_origin_update.status_code == 403
        assert foreign_origin_cancel.status_code == 403
        assert (
            updated_response.json()["run"]["questions"][0]["queued_clarification"][
                "answer"
            ]
            == "API 修改後補答"
        )
        assert cancelled_response.status_code == 200
        assert cancelled_response.json()["run"]["queued_clarification_count"] == 0
        assert checkpoint.read_bytes() == original_checkpoint
    finally:
        service._release_lease(lease)


def test_legacy_event_conflict_is_read_projected_and_answered_via_queue(
    tmp_path: Path,
) -> None:
    service, client, _source, run_id = _service_with_two_waiting_questions(tmp_path)
    checkpoint = service.runs_dir / f"{run_id}.json"
    state = json.loads(checkpoint.read_text(encoding="utf-8"))
    question = state["questions"][0]
    question["status"] = "needs_review"
    question["clarification_review_reason"] = "舊版事件衝突"
    result = question["turns"][-1]["result"]
    result["clarification_signal"] = "event_conflict"
    result["error"] = {"message": "舊版衝突標記", "retryable": False}
    checkpoint.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    saved_legacy_checkpoint = checkpoint.read_bytes()

    read_projection = service.get_run(run_id)
    projected_question = read_projection["run"]["questions"][0]
    assert projected_question["status"] == "awaiting_clarification"
    assert projected_question["checkpoint_status"] == "needs_review"
    assert projected_question["clarification_projection"] == "legacy_event_conflict"
    assert "唯讀相容投影" in projected_question["clarification_projection_note"]
    assert projected_question["assistant_text"] == "請選擇 A 或 B"
    assert projected_question["clarification_review_candidate"] is False
    assert read_projection["run"]["status_counts"]["awaiting_clarification"] == 2
    assert read_projection["run_status"] == "awaiting_clarification"
    assert checkpoint.read_bytes() == saved_legacy_checkpoint

    lease = service._try_lease()
    assert lease is not None
    try:
        queued = service.clarify("1", "選擇 A", run_id=run_id)
        queued_question = queued["run"]["questions"][0]
        assert queued_question["display_status"] == "queued"
        assert queued_question["queued_clarification"]["answer"] == "選擇 A"
        assert checkpoint.read_bytes() == saved_legacy_checkpoint
        updated = service.update_queued_clarification(run_id, "1", "修改後 A")
        assert updated["run"]["questions"][0]["queued_clarification"]["answer"] == (
            "修改後 A"
        )
        cancelled = service.cancel_queued_clarification(run_id, "1")
        assert cancelled["run"]["questions"][0]["status"] == ("awaiting_clarification")
        assert checkpoint.read_bytes() == saved_legacy_checkpoint
        service.clarify("1", "選擇 A", run_id=run_id)
    finally:
        service._release_lease(lease)

    service.resume(run_id)
    _wait_for_worker_idle(service)
    completed = service.get_run(run_id)["run"]["questions"][0]
    assert completed["status"] == "completed"
    assert checkpoint.read_bytes() != saved_legacy_checkpoint
    saved_question = json.loads(checkpoint.read_text(encoding="utf-8"))["questions"][0]
    assert saved_question["clarification_legacy_projection"] == "legacy_event_conflict"
    assert saved_question["turns"][0]["result"]["error"]["message"] == "舊版衝突標記"
    assert any(content == "選擇 A" for _chat, content in client.sent)


def test_event_conflict_without_completed_question_is_not_projected_or_queued(
    tmp_path: Path,
) -> None:
    service, _client, _source, run_id = _service_with_two_waiting_questions(tmp_path)
    checkpoint = service.runs_dir / f"{run_id}.json"
    state = json.loads(checkpoint.read_text(encoding="utf-8"))
    question = state["questions"][0]
    question["status"] = "needs_review"
    result = question["turns"][-1]["result"]
    result["clarification_signal"] = "event_conflict"
    assistant = result["messages"][-1]
    assistant["output"][0]["arguments"] = '{"question":"  "}'
    checkpoint.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    before = checkpoint.read_bytes()

    projected = service.get_run(run_id)["run"]["questions"][0]
    assert projected["status"] == "needs_review"
    assert projected["clarification_projection"] is None
    with pytest.raises(WorkbenchError, match="不等待澄清"):
        service.clarify("1", "猜一個", run_id=run_id)
    assert checkpoint.read_bytes() == before


def test_paused_run_queue_is_skipped_until_explicit_resume(
    tmp_path: Path,
) -> None:
    source = _questions_file(
        tmp_path / "three-questions.txt",
        "1: 等待補答第一題\n2: 等待補答第二題\n3: 尚未執行的第三題\n",
    )
    client = FakeClient()
    client.clarify_all_first = True
    client.block_content = "等待補答第二題"
    service = _service(tmp_path / "private", question_file=source, client=client)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    waiting_run_id = service.start_source("v2-natural-100", digest)["run_id"]
    _wait_until(
        lambda: any(content == "等待補答第二題" for _chat, content in client.sent)
    )
    service.stop(expected_run_id=waiting_run_id)
    client.release.set()
    _wait_for_worker_idle(service)
    waiting_questions = service.status(waiting_run_id)["run"]["questions"]
    assert [item["status"] for item in waiting_questions] == [
        "awaiting_clarification",
        "awaiting_clarification",
        "pending",
    ]

    client.clarify_all_first = False
    client.block_content = "等待補答第一題"
    client.release.clear()
    client.started.clear()
    busy = service.start_source("v2-natural-100", digest)
    _wait_until(
        lambda: (
            service._worker_is_running()
            and bool(client.sent)
            and client.sent[-1][1] == "等待補答第一題"
        )
    )
    try:
        service.clarify("2", "另一輪第二題先補", run_id=waiting_run_id)
        service.clarify("1", "另一輪第一題後補", run_id=waiting_run_id)
    finally:
        client.release.set()

    _wait_for_worker_idle(service)
    queued_answers = [
        content
        for _chat, content in client.sent
        if content in {"另一輪第二題先補", "另一輪第一題後補"}
    ]
    assert queued_answers == []
    assert service.status(waiting_run_id)["run"]["queued_clarification_count"] == 2
    service.resume(waiting_run_id)
    _wait_for_worker_idle(service)
    queued_answers = [
        content
        for _chat, content in client.sent
        if content in {"另一輪第二題先補", "另一輪第一題後補"}
    ]
    assert queued_answers == ["另一輪第二題先補", "另一輪第一題後補"]
    finished_waiting_run = service.status(waiting_run_id)["run"]["questions"]
    assert [item["status"] for item in finished_waiting_run] == [
        "completed",
        "completed",
        "completed",
    ]
    assert any(content == "尚未執行的第三題" for _chat, content in client.sent)
    assert service.status(busy["run_id"])["run_status"] == "completed"


def test_running_queued_clarification_cannot_be_edited_or_cancelled(
    tmp_path: Path,
) -> None:
    service, client, _source, run_id = _service_with_two_waiting_questions(tmp_path)
    client.block_content = "開始執行的補答"
    client.release.clear()
    service.clarify("1", "開始執行的補答", run_id=run_id)
    _wait_until(
        lambda: any(content == "開始執行的補答" for _chat, content in client.sent)
    )
    question = service.status(run_id)["run"]["questions"][0]
    assert question["display_status"] == "running"
    with pytest.raises(WorkbenchError, match="已開始執行，無法修改"):
        service.update_queued_clarification(run_id, "1", "不能修改")
    with pytest.raises(WorkbenchError, match="已開始執行，無法取消"):
        service.cancel_queued_clarification(run_id, "1")
    client.release.set()
    _wait_for_worker_idle(service)
    assert service.status(run_id)["run"]["questions"][0]["status"] == "completed"


def test_queued_answer_survives_restart_but_explicit_stop_requires_resume(
    tmp_path: Path,
) -> None:
    service, client, source, run_id = _service_with_two_waiting_questions(tmp_path)
    lease = service._try_lease()
    assert lease is not None
    try:
        service.clarify("1", "重啟後補答", run_id=run_id)
    finally:
        service._release_lease(lease)

    service.stop(expected_run_id=run_id)
    restarted = _service(service.storage_dir, question_file=source, client=client)
    assert restarted.recover_after_restart() is False
    persisted = restarted.status(run_id)["run"]
    assert persisted["queued_clarification_count"] == 1
    assert persisted["questions"][0]["queued_clarification"]["answer"] == ("重啟後補答")
    assert not any(content == "重啟後補答" for _chat, content in client.sent)

    restarted.resume(run_id)
    _wait_for_worker_idle(restarted)
    completed = restarted.status(run_id)["run"]["questions"][0]
    assert completed["status"] == "completed"
    assert client.sent[-1] == ("chat-1", "重啟後補答")


def test_uncertain_queued_recovery_uses_stoppable_backoff_and_preserves_fifo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UncertainQueueClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.uncertain_operation_id: str | None = None
            self.recovery_calls = 0
            self.recovery_success = False

        def send_turn(
            self, conversation_id: str, content: str, operation_id: str
        ) -> TurnResult:
            if content == "狀態不明的第一題補答":
                self.sent.append((conversation_id, content))
                self.uncertain_operation_id = operation_id
                raise OpenWebUIStateUncertain("completion 狀態未確認")
            return super().send_turn(conversation_id, content, operation_id)

        def recover_turn(
            self, conversation_id: str, operation_id: str
        ) -> TurnResult | None:
            if operation_id != self.uncertain_operation_id:
                return None
            self.recovery_calls += 1
            if self.recovery_success:
                return TurnResult(
                    messages=[
                        {"role": "user", "content": "狀態不明的第一題補答"},
                        {"role": "assistant", "content": "已恢復完成"},
                    ]
                )
            raise OpenWebUIStateUncertain("task 仍在執行，狀態尚未確認")

    monkeypatch.setattr(
        EvaluationRunner, "CLARIFICATION_RECOVERY_BACKOFF_INITIAL_SECONDS", 0.5
    )
    monkeypatch.setattr(
        EvaluationRunner, "CLARIFICATION_RECOVERY_BACKOFF_MAX_SECONDS", 2.0
    )
    client = UncertainQueueClient()
    client.clarify_all_first = True
    service, client, _source, run_id = _service_with_two_waiting_questions(
        tmp_path, client=client
    )

    service.clarify("1", "狀態不明的第一題補答", run_id=run_id)
    _wait_until(lambda: client.recovery_calls == 1)

    def first_retry_is_scheduled() -> bool:
        queue_items = json.loads(
            service._clarification_queue_path.read_text(encoding="utf-8")
        )["items"]
        entry = next(item for item in queue_items if item["question_id"] == "1")
        return isinstance(entry.get("next_attempt_at"), str)

    _wait_until(first_retry_is_scheduled)
    question = service._read_state(run_id)["questions"][0]
    pending = question["pending_turn"]
    queue_id = pending["queue_id"]
    assert question["status"] == "running"
    assert not any(turn.get("queue_id") == queue_id for turn in question["turns"]), (
        "恢復尚未確認前不得將補答寫為完成 turn"
    )
    queued_entry = next(
        item
        for item in json.loads(
            service._clarification_queue_path.read_text(encoding="utf-8")
        )["items"]
        if item["queue_id"] == queue_id
    )
    assert queued_entry["status"] == "executing"
    assert queued_entry["answer"] == "狀態不明的第一題補答"
    assert isinstance(queued_entry.get("next_attempt_at"), str)
    time.sleep(0.1)
    assert client.recovery_calls == 1, "backoff 期間不得重複查詢遠端 task"

    service.clarify("2", "後續可執行的第二題補答", run_id=run_id)
    _wait_until(
        lambda: service._read_state(run_id)["questions"][1]["status"] == "completed"
    )
    _wait_until(lambda: client.recovery_calls >= 2)
    _wait_until(
        lambda: (
            service._read_state(run_id)["questions"][0]["pending_turn"]["attempts"][
                -1
            ].get("recovery_failure_count")
            == 2
        )
    )
    state = service._read_state(run_id)
    first_question = state["questions"][0]
    second_question = state["questions"][1]
    assert first_question["status"] == "running"
    assert first_question["pending_turn"]["queue_id"] == queue_id
    assert second_question["status"] == "completed", (
        "較後的可執行補答不得被不確定項目阻擋"
    )
    recovery_errors = [
        error
        for error in first_question["errors"]
        if error.get("stage") == "recover_turn"
        and error.get("operation_id") == client.uncertain_operation_id
    ]
    assert len(recovery_errors) == 1, "重複 uncertain recovery error 應去重"
    assert (
        sum(content == "狀態不明的第一題補答" for _chat, content in client.sent) == 1
    ), "task 尚存時不得重送 completion"

    service.stop(expected_run_id=run_id)
    _wait_for_worker_idle(service)
    assert service.recover_after_restart() is False, "明確停止後重啟不得暗中恢復"

    client.recovery_success = True
    checkpoint_path = service.runs_dir / f"{run_id}.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["questions"][0]["pending_turn"]["attempts"][-1][
        "recovery_not_before"
    ] = "2000-01-01T00:00:00+00:00"
    checkpoint_path.write_text(
        json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8"
    )
    queue_state = json.loads(
        service._clarification_queue_path.read_text(encoding="utf-8")
    )
    next(item for item in queue_state["items"] if item["queue_id"] == queue_id)[
        "next_attempt_at"
    ] = "2000-01-01T00:00:00+00:00"
    service._clarification_queue_path.write_text(
        json.dumps(queue_state, ensure_ascii=False), encoding="utf-8"
    )

    service.resume(run_id)
    _wait_for_worker_idle(service)
    finished = service._read_state(run_id)
    assert [question["status"] for question in finished["questions"]] == [
        "completed",
        "completed",
    ]
    assert finished["questions"][0]["pending_turn"] is None
    assert client.recovery_calls == 3
    assert sum(content == "狀態不明的第一題補答" for _chat, content in client.sent) == 1
    assert service.status(run_id)["run"]["queued_clarification_count"] == 0


def test_expired_queued_clarification_is_discarded_without_sending_to_chat(
    tmp_path: Path,
) -> None:
    service, client, _source, run_id = _service_with_two_waiting_questions(tmp_path)
    lease = service._try_lease()
    assert lease is not None
    service.clarify("1", "不可送出的舊補答", run_id=run_id)
    state_path = service.runs_dir / f"{run_id}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["questions"][0]["status"] = "completed"
    state["questions"][0]["completed_at"] = "2026-10-03T00:00:00+00:00"
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    service._release_lease(lease)

    assert service.recover_after_restart() is False
    assert service.status(run_id)["run"]["queued_clarification_count"] == 1
    service.resume(run_id)
    _wait_for_worker_idle(service)
    _wait_for_worker_idle(service)
    assert not any(content == "不可送出的舊補答" for _chat, content in client.sent)
    assert service.status(run_id)["run"]["queued_clarification_count"] == 0


def test_queue_wait_is_not_counted_as_processing_time(tmp_path: Path) -> None:
    service, _client, _source, run_id = _service_with_two_waiting_questions(tmp_path)
    lease = service._try_lease()
    assert lease is not None
    service.clarify("1", "計時補答", run_id=run_id)
    service._release_lease(lease)
    time.sleep(1.2)

    service.resume(run_id)
    _wait_for_worker_idle(service)
    question = service.status(run_id)["run"]["questions"][0]
    assert question["processing_elapsed_ms"] == sum(
        attempt["duration_ms"]
        for turn in service._read_state(run_id)["questions"][0]["turns"]
        for attempt in turn["attempts"]
    )
    assert (
        question["elapsed_ms"]
        - question["user_wait_ms"]
        - question["processing_elapsed_ms"]
        >= 500
    )


def test_ui_projection_distinguishes_queued_and_executing_clarification() -> None:
    state = {
        "run_id": "a" * 32,
        "manifest": {},
        "questions": [
            {
                "id": "1",
                "prompt": "題目",
                "status": "awaiting_clarification",
                "turns": [],
                "errors": [],
                "usage_totals": {},
            }
        ],
    }
    queued = _state_for_ui(
        state,
        clarification_queue=[
            {
                "question_id": "1",
                "queue_id": "queued-id",
                "status": "queued",
                "answer": "已保存補答",
            }
        ],
    )
    executing = _state_for_ui(
        state,
        clarification_queue=[
            {
                "question_id": "1",
                "queue_id": "running-id",
                "status": "executing",
                "answer": "已開始執行",
            }
        ],
    )
    assert queued["queued_clarification_count"] == 1
    assert queued["queued_work_count"] == 1
    assert queued["questions"][0]["display_status"] == "queued"
    assert set(queued["status_counts"]) == {
        "queued",
        "running",
        "awaiting_clarification",
        "completed",
        "failed",
        "needs_review",
    }
    assert queued["questions"][0]["queued_clarification"]["answer"] == "已保存補答"
    assert executing["questions"][0]["display_status"] == "running"
