"""評測工作台 admin API、來源限制、續跑與輸出安全測試。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.staticfiles import StaticFiles

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
        self.clarify_first = False

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
        if self.block_first and len(self.sent) == 1:
            self.release.wait(timeout=5)
        if (
            self.clarify_first
            and conversation_id == "chat-1"
            and len([item for item in self.sent if item[0] == conversation_id]) == 1
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


def _app(service: EvaluationWorkbenchService, static_dir: Path) -> FastAPI:
    static_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI()
    app.include_router(create_api_router(service, _admin_dependency))
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
    run_id = "a" * 32
    service.runs_dir.mkdir(parents=True, exist_ok=True)
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
                "turns": [],
                "pending_turn": None,
            }
        ],
    }
    (service.runs_dir / f"{run_id}.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )
    app = _app(service, tmp_path / "static")

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
        invalid_id = client.get(
            path.replace(run_id, "../escape"),
            headers={"Authorization": "Bearer admin-test"},
        )

    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("text/html")
    assert (
        'attachment; filename="evaluation-' in downloaded.headers["content-disposition"]
    )
    assert "connect-src 'none'" in downloaded.headers["content-security-policy"]
    assert "admin-test-secret" not in downloaded.text
    assert "未提供（null）" in downloaded.text
    assert "此題未產生圖表。" in downloaded.text
    assert printable.status_code == 200
    assert 'inline; filename="evaluation-' in printable.headers["content-disposition"]
    assert "window.print()" in printable.text
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
    assert not any(path.endswith(".pdf") for path in ROUTES.values())


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
                                    "content": "raw provider detail org-id-987",
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
                                    "content": "raw provider detail org-id-987",
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
                "turns": [
                    {
                        "result": {
                            "error": {
                                "message": "互動圖表附加狀態不明且未讀到圖表；需要人工複核"
                            },
                            "messages": [
                                {
                                    "role": "assistant",
                                    "content": "模型聲稱圖表已附加，但 raw provider detail org-id-987",
                                }
                            ],
                            "tool_calls": [],
                            "charts": [],
                        }
                    }
                ],
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
                                    "content": "raw provider detail org-id-987",
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
    assert question["assistant_text"] == ""
    if question_data["status"] == "completed":
        assert question["status"] == "completed"
        assert state["questions"][0]["status"] == "completed"


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
    with pytest.raises(WorkbenchError, match="另一個評測工作"):
        _service(storage, question_file=question_file).start_source(
            "v2-natural-100", source_hash
        )

    stopping = service.stop()
    assert stopping["run_status"] == "stopping"
    client.release.set()
    _wait_until(lambda: not service._worker_is_running())
    stopped = service.status()
    assert stopped["run_status"] == "stopped"
    run_id = stopped["run_id"]

    resumed = service.resume()
    assert resumed["run_status"] in {"running", "completed"}
    _wait_until(lambda: not service._worker_is_running())
    final = service.status()
    assert final["run_status"] == "completed"
    assert len(client.sent) == 2
    assert [item["status"] for item in final["run"]["questions"]] == [
        "completed",
        "completed",
    ]
    saved = json.loads(service.download_json(run_id))
    assert saved["questions"][0]["turns"][0]["request"] == "第一題原文？"


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
    assert recovered.recover_after_restart() is True
    _wait_until(lambda: not recovered._worker_is_running())
    json_download = recovered.download_json(run_id).decode("utf-8")
    summary_download = recovered.download_summary(run_id).decode("utf-8")
    status_body = json.dumps(recovered.status(), ensure_ascii=False)

    assert recovered.status()["run_status"] == "completed"
    assert runner.snapshot()["questions"][0]["turns"][0]["result"]["usage"] is None
    assert "admin-test-secret" not in json_download
    assert "admin-test-secret" not in summary_download
    assert "admin-test-secret" not in status_body
    assert "admin-test-secret" not in (
        tmp_path / "private" / "runs" / f"{run_id}.json"
    ).read_text(encoding="utf-8")
    assert '"usage": null' in json_download
    assert "Token：input_tokens=未取得模型實際用量" in summary_download


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
