"""Open WebUI Event Function 的管理員路由、SPA 順序與生命週期測試。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from scripts.evaluation_workbench_service import (
    ROUTES,
    EvaluationWorkbenchService,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FUNCTION_PATH = PROJECT_ROOT / "openwebui_functions" / "evaluation_workbench_event.py"
BOOTSTRAP_HARNESS = (
    PROJECT_ROOT / "tests" / "evaluation_workbench_bootstrap_harness.cjs"
)
SELECTION_HARNESS = (
    PROJECT_ROOT / "tests" / "evaluation_workbench_selection_harness.cjs"
)
PAGE_PATH = "/badmintonai/evaluation"
NEW_PAGE_PATH = f"{PAGE_PATH}/new"
HISTORY_PAGE_PATH = f"{PAGE_PATH}/history"
FUNCTION_ID = "badmintonai-evaluation-workbench"
API_KEY = "server-only-test-secret"


async def _fake_get_admin_user(request: Request) -> dict[str, str]:
    authorization = request.headers.get("authorization")
    if authorization == "Bearer test-admin":
        return {"id": "admin-42", "username": "test-admin", "role": "admin"}
    if authorization == "Bearer test-user":
        raise HTTPException(status_code=403, detail="Admin access required")
    raise HTTPException(status_code=401, detail="Not authenticated")


class _IdleClient:
    def get_model_snapshot(self) -> dict[str, Any]:
        return {"model_id": "badmintonai", "tool_ids": [], "updated_at": None}


@pytest.fixture
def event_module(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, Any]:
    open_webui = ModuleType("open_webui")
    open_webui.__path__ = []  # type: ignore[attr-defined]
    utils = ModuleType("open_webui.utils")
    utils.__path__ = []  # type: ignore[attr-defined]
    auth = ModuleType("open_webui.utils.auth")
    auth.get_admin_user = _fake_get_admin_user  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "open_webui", open_webui)
    monkeypatch.setitem(sys.modules, "open_webui.utils", utils)
    monkeypatch.setitem(sys.modules, "open_webui.utils.auth", auth)

    spec = importlib.util.spec_from_file_location(
        "evaluation_workbench_event_under_test", FUNCTION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, auth.get_admin_user


@pytest.fixture
def workbench(tmp_path: Path) -> EvaluationWorkbenchService:
    question_file = tmp_path / "approved-questions.txt"
    question_file.write_text("1: 原始題目？\n", encoding="utf-8")
    return EvaluationWorkbenchService(
        storage_dir=tmp_path / "private-storage",
        question_file=question_file,
        client_factory=_IdleClient,
        data_snapshot_provider=lambda: {"snapshot_id": "snapshot-test"},
        model_snapshot_provider=lambda client: client.get_model_snapshot(),
        api_key_provider=lambda: API_KEY,
    )


def _configure_module(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    workbench: EvaluationWorkbenchService,
) -> None:
    monkeypatch.setattr(module, "_workbench", lambda: workbench)
    monkeypatch.setattr(
        module,
        "_ASSET_ROOT",
        PROJECT_ROOT / "openwebui_functions",
    )


def _register_on_startup(module: ModuleType, app: FastAPI) -> Any:
    event_function = module.Event()
    asyncio.run(
        event_function.event(
            {},
            __event_name__="system.startup.completed",
            __app__=app,
        )
    )
    return event_function


def _app_with_spa_mount(static_dir: Path) -> FastAPI:
    static_dir.mkdir(parents=True, exist_ok=True)
    (static_dir / "index.html").write_text("SPA fallback", encoding="utf-8")
    app = FastAPI()

    @app.get("/health", name="existing_health_route")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.mount("/", StaticFiles(directory=static_dir, html=True), name="spa")
    return app


def _routes_by_name(app: FastAPI, module: ModuleType) -> dict[str, Any]:
    return {
        route.name: route
        for route in app.router.routes
        if getattr(route, "name", None) in module._OWN_ROUTE_NAMES
    }


def test_lifecycle_registration_is_ordered_idempotent_and_restartable(
    event_module: tuple[ModuleType, Any],
    monkeypatch: pytest.MonkeyPatch,
    workbench: EvaluationWorkbenchService,
    tmp_path: Path,
) -> None:
    module, _admin_dependency = event_module
    _configure_module(module, monkeypatch, workbench)
    app = _app_with_spa_mount(tmp_path / "static")
    original_routes = list(app.router.routes)
    function = _register_on_startup(module, app)

    routes = _routes_by_name(app, module)
    assert set(routes) == module._OWN_ROUTE_NAMES
    route_names = [route.name for route in app.router.routes]
    spa_index = route_names.index("spa")
    assert route_names.index("existing_health_route") < spa_index
    assert all(route_names.index(name) < spa_index for name in routes)
    assert [route for route in app.router.routes if route not in routes.values()] == (
        original_routes
    )

    # 重複 startup/enable 不應增加重複路由；其他 Function 的事件不應影響本頁。
    _register_on_startup(module, app)
    asyncio.run(
        function.event(
            {"subject": {"type": "function", "id": FUNCTION_ID}},
            __event_name__="function.enable_started",
            __id__=FUNCTION_ID,
            __app__=app,
        )
    )
    assert set(_routes_by_name(app, module)) == module._OWN_ROUTE_NAMES

    asyncio.run(
        function.event(
            {"subject": {"type": "function", "id": "another-function"}},
            __event_name__="function.disable_started",
            __id__=FUNCTION_ID,
            __app__=app,
        )
    )
    assert set(_routes_by_name(app, module)) == module._OWN_ROUTE_NAMES

    asyncio.run(
        function.event(
            {"subject": {"type": "function", "id": FUNCTION_ID}},
            __event_name__="function.disable_started",
            __id__=FUNCTION_ID,
            __app__=app,
        )
    )
    assert _routes_by_name(app, module) == {}
    assert isinstance(app.router.routes[-1], Mount)
    with TestClient(app) as client:
        assert client.get(PAGE_PATH).status_code == 404

    restarted_app = _app_with_spa_mount(tmp_path / "restarted-static")
    _register_on_startup(module, restarted_app)
    assert set(_routes_by_name(restarted_app, module)) == module._OWN_ROUTE_NAMES
    restarted_names = [route.name for route in restarted_app.router.routes]
    assert restarted_names.index(module._PAGE_ROUTE_NAME) < restarted_names.index("spa")


def test_page_assets_and_every_api_route_use_real_admin_dependency(
    event_module: tuple[ModuleType, Any],
    monkeypatch: pytest.MonkeyPatch,
    workbench: EvaluationWorkbenchService,
    tmp_path: Path,
) -> None:
    module, admin_dependency = event_module
    _configure_module(module, monkeypatch, workbench)
    app = _app_with_spa_mount(tmp_path / "static")
    _register_on_startup(module, app)

    routes = _routes_by_name(app, module)
    assert set(routes) == module._OWN_ROUTE_NAMES
    for route in routes.values():
        assert any(
            dependency.call is admin_dependency
            for dependency in route.dependant.dependencies
        )

    with TestClient(app) as client:
        for route in routes.values():
            if "GET" not in route.methods:
                continue
            path = route.path.replace("{run_id}", "a" * 32)
            assert client.get(path).status_code == 401
            assert (
                client.get(
                    path, headers={"Authorization": "Bearer test-user"}
                ).status_code
                == 403
            )

        page = client.get(PAGE_PATH, headers={"Authorization": "Bearer test-admin"})
        new_page = client.get(
            NEW_PAGE_PATH,
            headers={"Authorization": "Bearer test-admin"},
        )
        history_page = client.get(
            HISTORY_PAGE_PATH,
            headers={"Authorization": "Bearer test-admin"},
        )
        css = client.get(
            "/badmintonai/evaluation/assets/workbench.css",
            headers={"Authorization": "Bearer test-admin"},
        )
        js = client.get(
            "/badmintonai/evaluation/assets/workbench.js",
            headers={"Authorization": "Bearer test-admin"},
        )
        status = client.get(
            ROUTES["badmintonai_evaluation_status"],
            headers={"Authorization": "Bearer test-admin"},
        )
        sources = client.get(
            ROUTES["badmintonai_evaluation_sources"],
            headers={"Authorization": "Bearer test-admin"},
        )
        missing_origin = client.post(
            ROUTES["badmintonai_evaluation_stop"],
            headers={"Authorization": "Bearer test-admin"},
        )
        foreign_origin = client.post(
            ROUTES["badmintonai_evaluation_stop"],
            headers={
                "Authorization": "Bearer test-admin",
                "Origin": "https://attacker.example",
            },
        )

    assert (
        page.status_code
        == new_page.status_code
        == history_page.status_code
        == css.status_code
        == js.status_code
        == 200
    )
    assert '<html lang="zh-Hant">' in page.text
    assert 'href="#main-content"' in page.text
    assert '<main class="workbench" id="main-content">' in page.text
    assert '<p class="eyebrow">' not in page.text
    assert "3.25rem" not in css.text
    assert ".grid {" not in css.text
    assert css.text.count(".panel {") == 1
    assert '.top-nav a[aria-current="page"]' in css.text
    assert ".page-view[hidden] { display: none; }" in css.text
    assert 'data-page-view="progress"' in page.text
    assert 'data-page-view="new"' in page.text
    assert 'data-page-view="history"' in page.text
    assert 'id="page-title"' in page.text
    assert 'id="page-lede"' in page.text
    assert f'href="{NEW_PAGE_PATH}"' in page.text
    assert f'href="{HISTORY_PAGE_PATH}"' in page.text
    assert 'id="historical-run-detail"' in page.text
    assert 'id="selected-count"' in page.text
    assert 'id="select-all-button"' in page.text
    assert 'id="clear-selection-button"' in page.text
    assert 'id="range-input"' in page.text
    assert 'id="question-matrix"' in page.text
    assert 'id="historical-question-matrix"' in page.text
    assert 'id="question-detail-dialog"' in page.text
    assert 'aria-modal="true"' in page.text
    assert 'aria-labelledby="question-detail-title"' in page.text
    assert 'id="status-legend"' in page.text
    assert all(
        f'class="state-swatch state-{status}"' in page.text
        for status in (
            "pending",
            "running",
            "awaiting_clarification",
            "needs_review",
            "completed",
            "failed",
        )
    )
    assert "凍結 checkpoint 的原始狀態與回合不會被改寫" in page.text
    assert "server-only-test-secret" not in page.text
    assert "server-only-test-secret" not in js.text
    assert "innerHTML" not in js.text
    assert "insertAdjacentHTML" not in js.text
    assert (
        "function renderQuestionMatrix(container, questions, runId, showAnnotationTools)"
        in js.text
    )
    assert (
        "function openQuestionDetails(question, runId, showAnnotationTools, sourceElement)"
        in js.text
    )
    assert "function renderClarificationForm(question)" in js.text
    assert "const questionStatuses = [" in js.text
    assert '["總題數", String(questions.length)]' in js.text
    assert '["總處理耗時（不含等待補答）", elapsedSummary]' in js.text
    assert ".map((question) => processingElapsedMs(question))" in js.text
    assert "question.elapsed_ms - question.user_wait_ms" in js.text
    assert "const counts = Object.fromEntries(questionStatuses.map(" in js.text
    assert 'needs_review: "需要人工複核"' in js.text
    assert "classification-annotation" in js.text
    assert "人工分類註記已另存；原始 checkpoint 未改寫" in js.text
    assert (
        "renderQuestionMatrix(historicalQuestionMatrix, payload.run.questions, payload.run_id, true)"
        in js.text
    )
    assert 'button.setAttribute("aria-haspopup", "dialog")' in js.text
    assert 'button.setAttribute("aria-controls", "question-detail-dialog")' in js.text
    assert "detailDialog.showModal()" in js.text
    assert 'detailDialog.addEventListener("close"' in js.text
    assert "function clearConversationPreview()" in js.text
    assert "async function loadConversationPreview(" in js.text
    assert 'credentials: "same-origin"' in js.text
    assert 'cache: "no-store"' in js.text
    assert "conversation.append(prompt, answer)" in js.text
    assert "clearConversationPreview();" in js.text
    assert 'frame.setAttribute("sandbox", "allow-scripts")' in js.text
    assert 'frame.setAttribute("referrerpolicy", "no-referrer")' in js.text
    assert 'detailConversationFrame.removeAttribute("src")' in js.text
    assert 'frame.setAttribute("loading", "eager")' in js.text
    assert "`${conversationPath}#chart-0`" in js.text
    assert "展開完整對話與互動圖表" not in js.text
    assert "完整 Markdown 與互動圖表會顯示於下方" in js.text
    assert "原對話目前無法讀取；以下提供安全的題目與回答備援。" in js.text
    assert (
        "questions/${encodeURIComponent(String(question.id))}/conversation.html"
        in js.text
    )
    assert 'event.key === "ArrowUp"' in js.text
    assert "replacement?.focus({ preventScroll: true })" in js.text
    assert 'createText("h4", "圖表")' not in js.text
    assert "已讀回 ${question.chart_count}" not in js.text
    assert 'appendDetailValue(metrics, "人工等待補答"' not in js.text
    assert 'appendDetailValue(metrics, "端到端耗時（含等待）"' not in js.text
    assert 'appendDetailValue(metrics, "處理耗時（不含人工等待）"' in js.text
    assert "item.append(head, conversation, metrics)" in js.text
    assert "item.append(head, prompt, answer, metrics, conversation)" in js.text
    assert 'stopping: "停止中"' in js.text
    assert '"X-Selected-Question-Ids": JSON.stringify(questionIds)' in js.text
    assert "question_ids: questionIds" in js.text
    assert 'window.location.pathname === "/badmintonai/evaluation/new"' in js.text
    assert 'conversationLink.textContent = "開啟原對話"' in js.text
    assert (
        "conversationLink.href = `/c/${encodeURIComponent(question.conversation_id)}`"
        in js.text
    )
    assert '"下載離線 HTML 報告", "report.html", false' in js.text
    assert '"列印／另存 PDF", "report/print", true' in js.text
    assert "`${apiBase}/runs/${run.run_id}/report.html`" in js.text
    assert "`${apiBase}/runs/${run.run_id}/report/print`" in js.text
    assert "工作台不提供直接 PDF API" in js.text
    assert "@media (prefers-color-scheme: dark)" in css.text
    assert ".question-matrix > .empty { grid-column: 1 / -1; }" in css.text
    assert ".question-tile:focus-visible" in css.text
    assert ".question-detail-dialog::backdrop" in css.text
    assert ".conversation-frame" in css.text
    assert ".conversation-link:focus-visible" in css.text
    assert "embed" not in js.text.casefold()
    assert "server-only-test-secret" not in status.text
    assert status.json()["run_status"] == "idle"
    assert sources.json()["sources"][0]["available"] is True
    assert "SPA fallback" not in page.text + status.text + css.text + js.text
    assert new_page.text == page.text == history_page.text
    assert missing_origin.status_code == foreign_origin.status_code == 403


def test_browser_bootstrap_requests_status_and_renders_empty_history(
    event_module: tuple[ModuleType, Any],
    monkeypatch: pytest.MonkeyPatch,
    workbench: EvaluationWorkbenchService,
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("前端初始化回歸測試需要 Node.js")

    module, _admin_dependency = event_module
    _configure_module(module, monkeypatch, workbench)
    app = _app_with_spa_mount(tmp_path / "static")
    _register_on_startup(module, app)

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer test-admin"}
        page = client.get(PAGE_PATH, headers=headers)
        script = client.get(
            "/badmintonai/evaluation/assets/workbench.js",
            headers=headers,
        )

    assert page.status_code == script.status_code == 200
    result = subprocess.run(
        [node, str(BOOTSTRAP_HARNESS)],
        input=json.dumps({"html": page.text, "script": script.text}),
        capture_output=True,
        check=False,
        encoding="utf-8",
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    output = json.loads(result.stdout)
    assert output["current"] == "initialized"
    assert output["staleMarkup"] == "initialization error intercepted"
    assert output["status"] == "尚無評測紀錄"
    assert "尚無歷史 run" in output["recentText"]


def test_new_page_selection_posts_only_selected_ids_for_builtin_and_upload(
    event_module: tuple[ModuleType, Any],
    monkeypatch: pytest.MonkeyPatch,
    workbench: EvaluationWorkbenchService,
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("選題互動測試需要 Node.js")

    module, _admin_dependency = event_module
    _configure_module(module, monkeypatch, workbench)
    app = _app_with_spa_mount(tmp_path / "static")
    _register_on_startup(module, app)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer test-admin"}
        page = client.get(NEW_PAGE_PATH, headers=headers)
        script = client.get(
            "/badmintonai/evaluation/assets/workbench.js",
            headers=headers,
        )

    assert page.status_code == script.status_code == 200
    result = subprocess.run(
        [node, str(SELECTION_HARNESS)],
        input=json.dumps({"html": page.text, "script": script.text}),
        capture_output=True,
        check=False,
        encoding="utf-8",
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    output = json.loads(result.stdout)
    assert output["activePage"] == "new"
    assert output["builtinQuestionIds"] == ["4", "10"]
    assert output["uploadQuestionIds"] == ["1", "4", "10"]
    assert output["conversationQuestionIds"] == ["3", "4"]


def test_post_route_requires_same_origin_after_admin_authentication(
    event_module: tuple[ModuleType, Any],
    monkeypatch: pytest.MonkeyPatch,
    workbench: EvaluationWorkbenchService,
    tmp_path: Path,
) -> None:
    module, _admin_dependency = event_module
    _configure_module(module, monkeypatch, workbench)
    app = _app_with_spa_mount(tmp_path / "static")
    _register_on_startup(module, app)

    with TestClient(app) as client:
        missing_origin = client.post(
            ROUTES["badmintonai_evaluation_preview"],
            json={"source": "v2-natural-100"},
            headers={"Authorization": "Bearer test-admin"},
        )
        valid_origin = client.post(
            ROUTES["badmintonai_evaluation_preview"],
            json={"source": "v2-natural-100"},
            headers={
                "Authorization": "Bearer test-admin",
                "Origin": "http://testserver",
            },
        )
        unauthorized = client.post(
            ROUTES["badmintonai_evaluation_preview"],
            json={"source": "v2-natural-100"},
            headers={"Origin": "http://testserver"},
        )

    assert missing_origin.status_code == 403
    assert valid_origin.status_code == 200
    assert valid_origin.json()["questions"][0]["prompt"] == "原始題目？"
    assert unauthorized.status_code == 401
