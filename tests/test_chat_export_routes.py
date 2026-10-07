"""驗證原生下載路由使用完整對話分支與登入者權限。"""

from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from scripts.evaluation_workbench_service import ROUTES, create_api_router
from scripts.html_to_pdf import PDFRenderError


def _chat_payload() -> dict[str, Any]:
    chart = {
        "charts": [
            {
                "title": "第二輪圖表",
                "figure": {
                    "data": [{"type": "bar", "x": ["A"], "y": [1]}],
                    "layout": {},
                },
            }
        ]
    }
    embed = (
        '<script id="plotly-figure-data" type="application/json">'
        + json.dumps(chart, ensure_ascii=False)
        + "</script>"
    )
    return {
        "history": {
            "currentId": "a2",
            "messages": {
                "u1": {
                    "id": "u1",
                    "parentId": None,
                    "role": "user",
                    "content": "第一輪問題 **保留粗體**。",
                },
                "a1": {
                    "id": "a1",
                    "parentId": "u1",
                    "role": "assistant",
                    "content": "第一輪答案。",
                },
                "u2": {
                    "id": "u2",
                    "parentId": "a1",
                    "role": "user",
                    "content": "第二輪問題，含表格。",
                },
                "a2": {
                    "id": "a2",
                    "parentId": "u2",
                    "role": "assistant",
                    "content": "| 項目 | 值 |\n|---|---:|\n| 得分 | 1 |",
                    "embeds": [embed],
                },
                "a-old": {
                    "id": "a-old",
                    "parentId": "u1",
                    "role": "assistant",
                    "content": "不在目前分支的舊答案。",
                },
            },
        }
    }


async def _admin_user(request: Request) -> dict[str, str]:
    if request.headers.get("authorization") == "Bearer admin":
        return {"id": "admin", "role": "admin"}
    if request.headers.get("authorization") == "Bearer alice":
        raise HTTPException(status_code=403, detail="Admin access required")
    raise HTTPException(status_code=401, detail="Not authenticated")


async def _current_user(request: Request) -> dict[str, str]:
    authorization = request.headers.get("authorization")
    cookie_token = request.cookies.get("token")
    if authorization == "Bearer alice" or cookie_token == "alice":
        return {"id": "alice", "role": "user"}
    if authorization == "Bearer bob" or cookie_token == "bob":
        return {"id": "bob", "role": "user"}
    if authorization == "Bearer admin" or cookie_token == "admin":
        return {"id": "admin", "role": "admin"}
    raise HTTPException(status_code=401, detail="Not authenticated")


class FakeChats:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.chat = _chat_payload()

    async def get_chat_by_id_for_user(self, chat_id: str, user: dict[str, str]) -> Any:
        self.calls.append((chat_id, user["id"]))
        if (chat_id == "chat-alice-1" and user["id"] == "alice") or (
            chat_id == "chat-admin-1" and user["id"] == "admin"
        ):
            return SimpleNamespace(chat=self.chat)
        return None


async def _allow_chat_export(_user: Any) -> bool:
    return True


def _app(
    chats: FakeChats,
    *,
    renderer: Any = None,
    permission_checker: Any = _allow_chat_export,
) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_api_router(
            object(),
            _admin_user,
            user_dependency=_current_user,
            chat_provider=chats,
            chat_export_permission_checker=permission_checker,
            pdf_renderer=renderer or _fake_pdf_renderer,
            plotly_javascript_provider=lambda: "window.Plotly = {};",
        )
    )
    return app


async def _fake_pdf_renderer(document: str) -> bytes:
    del document
    return b"%PDF-1.7\nfixture"


def test_chat_exports_use_full_saved_branch_and_current_user_access() -> None:
    chats = FakeChats()
    rendered_documents: list[str] = []

    async def record_pdf_renderer(document: str) -> bytes:
        rendered_documents.append(document)
        return b"%PDF-1.7\nfixture"

    app = _app(chats, renderer=record_pdf_renderer)
    html_path = ROUTES["badmintonai_chat_export_html"].replace(
        "{chat_id}", "chat-alice-1"
    )
    pdf_path = ROUTES["badmintonai_chat_export_pdf"].replace(
        "{chat_id}", "chat-alice-1"
    )

    with TestClient(app) as client:
        html_response = client.get(html_path, headers={"Authorization": "Bearer alice"})
        pdf_response = client.get(pdf_path, headers={"Authorization": "Bearer alice"})
        anonymous = client.get(html_path)
        another_user = client.get(pdf_path, headers={"Authorization": "Bearer bob"})
        client.cookies.set("token", "alice")
        cookie_authenticated = client.get(html_path)
        invalid_id = client.get(
            ROUTES["badmintonai_chat_export_html"].replace("{chat_id}", "bad.id"),
            headers={"Authorization": "Bearer alice"},
        )
        admin_only_run_pdf = client.get(
            ROUTES["badmintonai_evaluation_download_pdf"].replace("{run_id}", "a" * 32),
            headers={"Authorization": "Bearer alice"},
        )

    assert html_response.status_code == 200
    assert html_response.headers["content-disposition"].startswith("attachment;")
    assert html_response.headers["cache-control"] == "no-store"
    assert html_response.headers["x-content-type-options"] == "nosniff"
    assert html_response.headers["x-frame-options"] == "DENY"
    assert "connect-src 'none'" in html_response.headers["content-security-policy"]
    assert "第一輪問題 <strong>保留粗體</strong>。" in html_response.text
    assert "第二輪問題，含表格。" in html_response.text
    assert "不在目前分支的舊答案" not in html_response.text
    assert "第二輪圖表" in html_response.text
    assert 'data-theme="auto"' in html_response.text
    assert (
        'window.__BADMINTON_PDF_RENDER__ = {status: "pending", error: null}'
        in html_response.text
    )
    assert "only the last message" not in html_response.text.casefold()

    assert pdf_response.status_code == 200
    assert pdf_response.content.startswith(b"%PDF-")
    assert pdf_response.headers["content-type"] == "application/pdf"
    assert pdf_response.headers["content-disposition"].startswith("attachment;")
    assert rendered_documents and 'data-theme="light"' in rendered_documents[0]
    assert "第一輪問題 <strong>保留粗體</strong>。" in rendered_documents[0]
    assert "第二輪圖表" in rendered_documents[0]
    assert chats.calls == [
        ("chat-alice-1", "alice"),
        ("chat-alice-1", "alice"),
        ("chat-alice-1", "bob"),
        ("chat-alice-1", "alice"),
    ]
    assert anonymous.status_code == 401
    assert another_user.status_code == 404
    assert cookie_authenticated.status_code == 200
    assert cookie_authenticated.headers["content-disposition"].startswith("attachment;")
    assert invalid_id.status_code == 404
    assert admin_only_run_pdf.status_code == 403


def test_chat_pdf_renderer_failure_is_reported_without_falling_back_to_partial_pdf() -> (
    None
):
    chats = FakeChats()

    async def fail_renderer(_document: str) -> bytes:
        raise PDFRenderError("圖表無法完成繪製，未產生 PDF", status_code=422)

    app = _app(chats, renderer=fail_renderer)
    pdf_path = ROUTES["badmintonai_chat_export_pdf"].replace(
        "{chat_id}", "chat-alice-1"
    )

    with TestClient(app) as client:
        response = client.get(pdf_path, headers={"Authorization": "Bearer alice"})

    assert response.status_code == 422
    assert "圖表無法完成繪製" in response.json()["detail"]
    assert not response.content.startswith(b"%PDF-")


def test_chat_export_requires_export_permission_before_loading_chat() -> None:
    chats = FakeChats()

    async def deny_chat_export(_user: Any) -> bool:
        return False

    app = _app(chats, permission_checker=deny_chat_export)
    html_path = ROUTES["badmintonai_chat_export_html"].replace(
        "{chat_id}", "chat-alice-1"
    )

    with TestClient(app) as client:
        response = client.get(html_path, headers={"Authorization": "Bearer alice"})

    assert response.status_code == 403
    assert response.json()["detail"] == "沒有匯出此對話的權限"
    assert chats.calls == []


def test_chat_export_uses_open_webui_group_and_default_permission_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    open_webui = ModuleType("open_webui")
    open_webui.__path__ = []  # type: ignore[attr-defined]
    config_module = ModuleType("open_webui.config")

    class FakeConfig:
        @staticmethod
        async def get(key: str) -> dict[str, object]:
            calls.append(("config", key))
            return {"chat": {"export": True}}

    config_module.Config = FakeConfig  # type: ignore[attr-defined]
    utils_module = ModuleType("open_webui.utils")
    utils_module.__path__ = []  # type: ignore[attr-defined]
    access_control_module = ModuleType("open_webui.utils.access_control")

    async def has_permission(user_id: str, permission: str, defaults: Any) -> bool:
        calls.append(("permission", user_id, permission, defaults))
        return True

    access_control_module.has_permission = has_permission  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "open_webui", open_webui)
    monkeypatch.setitem(sys.modules, "open_webui.config", config_module)
    monkeypatch.setitem(sys.modules, "open_webui.utils", utils_module)
    monkeypatch.setitem(
        sys.modules, "open_webui.utils.access_control", access_control_module
    )

    chats = FakeChats()
    app = _app(chats, permission_checker=None)
    html_path = ROUTES["badmintonai_chat_export_html"].replace(
        "{chat_id}", "chat-alice-1"
    )

    with TestClient(app) as client:
        response = client.get(html_path, headers={"Authorization": "Bearer alice"})

    assert response.status_code == 200
    assert calls == [
        ("config", "user.permissions"),
        ("permission", "alice", "chat.export", {"chat": {"export": True}}),
    ]


def test_chat_export_admin_bypasses_user_permission_checker() -> None:
    chats = FakeChats()

    async def should_not_check(_user: Any) -> bool:
        raise AssertionError("admin should follow Open WebUI's native bypass")

    app = _app(chats, permission_checker=should_not_check)
    pdf_path = ROUTES["badmintonai_chat_export_pdf"].replace(
        "{chat_id}", "chat-admin-1"
    )

    with TestClient(app) as client:
        response = client.get(pdf_path, headers={"Authorization": "Bearer admin"})

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-")
