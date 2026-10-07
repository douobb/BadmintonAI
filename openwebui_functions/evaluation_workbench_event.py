"""
title: BadmintonAI Evaluation Workbench
author: BadmintonAI
version: 0.2.0
description: 註冊管理員專用評測工作台，並依登入者權限提供聊天匯出路由。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import Depends
from fastapi.responses import FileResponse, HTMLResponse
from open_webui.utils.auth import get_admin_user, get_current_user
from starlette.routing import Mount

_RUNTIME_ROOT = Path("/opt/badmintonai-evaluation")
_ASSET_ROOT = _RUNTIME_ROOT / "ui"
sys.path.insert(0, str(_RUNTIME_ROOT))

import scripts.evaluation_workbench_service as _workbench_module  # noqa: E402

EvaluationWorkbenchService = _workbench_module.EvaluationWorkbenchService
_API_ROUTE_NAMES = _workbench_module.ROUTE_NAMES
_API_ROUTES = _workbench_module.ROUTES
create_api_router = _workbench_module.create_api_router

_PAGE_PATH = "/badmintonai/evaluation"
_NEW_PAGE_PATH = "/badmintonai/evaluation/new"
_HISTORY_PAGE_PATH = "/badmintonai/evaluation/history"
_CSS_PATH = "/badmintonai/evaluation/assets/workbench.css"
_JS_PATH = "/badmintonai/evaluation/assets/workbench.js"
_PAGE_ROUTE_NAME = "badmintonai_evaluation_page"
_NEW_PAGE_ROUTE_NAME = "badmintonai_evaluation_new_page"
_HISTORY_PAGE_ROUTE_NAME = "badmintonai_evaluation_history_page"
_CSS_ROUTE_NAME = "badmintonai_evaluation_css"
_JS_ROUTE_NAME = "badmintonai_evaluation_js"
_OWN_ROUTE_NAMES = {
    _PAGE_ROUTE_NAME,
    _NEW_PAGE_ROUTE_NAME,
    _HISTORY_PAGE_ROUTE_NAME,
    _CSS_ROUTE_NAME,
    _JS_ROUTE_NAME,
    *_API_ROUTE_NAMES,
}
_ROUTE_PATHS = {
    _PAGE_ROUTE_NAME: _PAGE_PATH,
    _NEW_PAGE_ROUTE_NAME: _NEW_PAGE_PATH,
    _HISTORY_PAGE_ROUTE_NAME: _HISTORY_PAGE_PATH,
    _CSS_ROUTE_NAME: _CSS_PATH,
    _JS_ROUTE_NAME: _JS_PATH,
    **_API_ROUTES,
}

_PAGE_HTML = """<!doctype html>
<html lang="zh-Hant">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="color-scheme" content="light dark">
    <title>評測工作台 | BadmintonAI</title>
    <link rel="icon" type="image/png" href="/static/favicon.png">
    <link rel="stylesheet" href="/badmintonai/evaluation/assets/workbench.css">
    <script src="/badmintonai/evaluation/assets/workbench.js" defer></script>
  </head>
  <body>
    <a class="skip-link" href="#main-content">跳至主要內容</a>
    <div class="shell">
      <header>
        <div>
          <h1 id="page-title">評測工作台</h1>
          <p id="page-lede" class="lede">逐題執行、查看結果與延續未完成回合。</p>
        </div>
        <a href="/">返回 Open WebUI</a>
      </header>

      <nav class="top-nav" aria-label="評測工作台頁面">
        <a data-page-link="progress" href="/badmintonai/evaluation">執行進度</a>
        <a data-page-link="new" href="/badmintonai/evaluation/new">預覽與選題</a>
        <a data-page-link="history" href="/badmintonai/evaluation/history">近期紀錄</a>
      </nav>
      <p id="notice" class="notice" role="status" aria-live="polite"></p>
      <main class="workbench" id="main-content">
        <section class="page-view" data-page-view="progress" aria-labelledby="progress-title">
          <div class="page-heading">
            <div>
              <h2 id="progress-title">執行進度</h2>
              <p class="help">查看目前狀態、逐題結果，或延續未完成回合。</p>
            </div>
            <span id="run-status" class="status-pill" role="status">讀取中</span>
          </div>
          <p id="selected-run-notice" class="help" hidden></p>
          <div class="actions">
            <button id="stop-button" class="danger" type="button" disabled>停止</button>
            <button id="resume-button" class="secondary" type="button" disabled>續跑</button>
          </div>
          <div id="downloads" class="actions" hidden></div>
          <div id="run-summary"></div>
          <section class="matrix-section" aria-labelledby="matrix-title">
            <div class="matrix-heading">
              <div>
                <h3 id="matrix-title">題目狀態矩陣</h3>
                <p class="help">每格代表一題；選取後按 Enter／空白鍵查看詳情。</p>
              </div>
            </div>
            <ul id="status-legend" class="status-legend" aria-label="題目狀態圖例">
              <li><span class="state-swatch state-queued" aria-hidden="true"></span>排隊中</li>
              <li><span class="state-swatch state-running" aria-hidden="true"></span>執行中</li>
              <li><span class="state-swatch state-awaiting_clarification" aria-hidden="true"></span>待補答</li>
              <li><span class="state-swatch state-completed" aria-hidden="true"></span>完成</li>
              <li><span class="state-swatch state-failed" aria-hidden="true"></span>失敗</li>
              <li><span class="state-swatch state-needs_review" aria-hidden="true"></span>需複核</li>
            </ul>
            <ol id="question-matrix" class="question-matrix" aria-label="目前 run 題目狀態矩陣；使用 Tab 聚焦題目，按 Enter 或空白鍵開啟詳情"></ol>
          </section>
          <section class="subsection" aria-labelledby="clarification-title">
            <h3 id="clarification-title">待補答工作</h3>
            <p class="help">選取題目以查看追問、可用選項並送回同一對話。</p>
            <ul id="clarification-list" class="question-list"></ul>
          </section>
        </section>

        <section class="page-view" data-page-view="new" aria-labelledby="new-title" hidden>
          <div class="page-heading">
            <div>
              <h2 id="new-title">預覽與選題</h2>
              <p class="help">先預覽原題，再選擇本輪要執行的題目；預設全選。未完成舊輪會保留紀錄；若仍在執行，開始前會先安全停止並等待目前回合結束。</p>
            </div>
          </div>
          <section class="panel" aria-labelledby="source-title">
            <h2 id="source-title">題目來源</h2>
            <form id="source-form">
              <fieldset>
                <legend>選擇核准來源</legend>
                <div class="radio-row">
                  <input type="radio" id="source-official" name="source" value="v2-natural-100" checked>
                  <label for="source-official">v2 自然語句 100 題（唯讀核准檔）</label>
                </div>
                <div class="radio-row">
                  <input type="radio" id="source-upload" name="source" value="upload">
                  <label for="source-upload">上傳 UTF-8 .txt</label>
                </div>
              </fieldset>
              <div class="field">
                <label for="question-file">題目文字檔</label>
                <input id="question-file" name="question-file" type="file" accept=".txt,text/plain" disabled>
                <span class="help">每行格式為「題號: 原題」；預覽保留原文，伺服器會限制檔案大小與題數。</span>
              </div>
              <div class="actions">
                <button id="preview-button" type="button">預覽題目</button>
              </div>
            </form>
            <section id="preview-box" hidden aria-labelledby="preview-title">
              <div class="question-head">
                <h3 id="preview-title">題目預覽</h3>
                <span id="selected-count" class="selection-count" role="status" aria-live="polite">尚未預覽題目</span>
              </div>
              <div class="selection-toolbar" aria-label="選取題目工具">
                <button id="select-all-button" class="secondary" type="button" disabled>全選</button>
                <button id="clear-selection-button" class="secondary" type="button" disabled>清除</button>
                <label class="range-control" for="range-input">
                  題號範圍
                  <input id="range-input" type="text" inputmode="numeric" pattern="[0-9]+-[0-9]+" placeholder="1-10" aria-label="題號範圍，例如 1-10" disabled>
                </label>
                <button id="apply-range-button" class="secondary" type="button" disabled>套用範圍</button>
              </div>
              <p id="selection-message" class="help selection-message" role="status" aria-live="polite"></p>
              <ol id="preview-list" class="preview-list"></ol>
              <div class="check-row">
                <input id="confirm-run" type="checkbox">
                <label for="confirm-run">我確認開始後會以既有模型實際執行選取題目並建立對話記錄。</label>
              </div>
              <div class="actions">
                <button id="start-button" type="button" disabled>開始新評測</button>
              </div>
            </section>
          </section>
        </section>

        <section class="page-view" data-page-view="history" aria-labelledby="recent-title" hidden>
          <div class="page-heading">
            <div>
              <h2 id="recent-title">近期紀錄</h2>
              <p class="help">選擇 run 進度檢視摘要矩陣、逐題詳情與人工複核註記；註記另存，不改寫凍結 checkpoint，也可下載離線報告。</p>
            </div>
          </div>
          <section class="panel">
            <ul id="recent-runs" class="run-list"></ul>
          </section>
        </section>
      </main>
      <dialog id="question-detail-dialog" class="question-detail-dialog" aria-modal="true" aria-labelledby="question-detail-title" aria-describedby="question-detail-hint">
        <div class="dialog-header">
          <h2 id="question-detail-title">題目詳情</h2>
          <button id="close-question-dialog" class="secondary" type="button" aria-label="關閉題目詳情" autofocus>關閉</button>
        </div>
        <p id="question-detail-hint" class="help">按 Esc 或關閉按鈕返回題目矩陣。</p>
        <div id="question-detail-content"></div>
      </dialog>
    </div>
  </body>
</html>
"""

_WORKBENCH: EvaluationWorkbenchService | None = None


def _workbench() -> EvaluationWorkbenchService:
    global _WORKBENCH
    if _WORKBENCH is None:
        _WORKBENCH = EvaluationWorkbenchService.from_environment()
    return _WORKBENCH


def _move_owned_routes_before_root_mount(app: Any) -> None:
    routes = app.router.routes
    owned_routes = [
        route for route in routes if getattr(route, "name", None) in _OWN_ROUTE_NAMES
    ]
    if not owned_routes:
        return
    other_routes = [
        route
        for route in routes
        if getattr(route, "name", None) not in _OWN_ROUTE_NAMES
    ]
    root_mount_index = next(
        (
            index
            for index, route in enumerate(other_routes)
            if isinstance(route, Mount) and getattr(route, "path", None) in {"", "/"}
        ),
        None,
    )
    if root_mount_index is not None:
        other_routes[root_mount_index:root_mount_index] = owned_routes
        routes[:] = other_routes


def _register_routes(app: Any) -> None:
    current_routes = list(app.router.routes)
    by_name = {
        getattr(route, "name", None): route
        for route in current_routes
        if getattr(route, "name", None) in _OWN_ROUTE_NAMES
    }
    expected = dict(_ROUTE_PATHS)
    for route_name, route_path in expected.items():
        route = by_name.get(route_name)
        if route is not None and getattr(route, "path", None) != route_path:
            raise RuntimeError("評測工作台路由名稱衝突")
        if route is None and any(
            getattr(item, "path", None) == route_path for item in current_routes
        ):
            raise RuntimeError("評測工作台路徑已被其他 endpoint 使用")

    if by_name and set(by_name) != _OWN_ROUTE_NAMES:
        raise RuntimeError("評測工作台路由註冊不完整；拒絕重複掛載")
    if not by_name:

        async def evaluation_page(
            user: Any = Depends(get_admin_user),
        ) -> HTMLResponse:
            del user
            return HTMLResponse(_PAGE_HTML, headers={"Cache-Control": "no-store"})

        async def evaluation_new_page(
            user: Any = Depends(get_admin_user),
        ) -> HTMLResponse:
            del user
            return HTMLResponse(_PAGE_HTML, headers={"Cache-Control": "no-store"})

        async def evaluation_history_page(
            user: Any = Depends(get_admin_user),
        ) -> HTMLResponse:
            del user
            return HTMLResponse(_PAGE_HTML, headers={"Cache-Control": "no-store"})

        async def evaluation_css(
            user: Any = Depends(get_admin_user),
        ) -> FileResponse:
            del user
            path = _ASSET_ROOT / "evaluation_workbench.css"
            if not path.is_file() or path.is_symlink():
                raise RuntimeError("評測工作台樣式資產不可用")
            return FileResponse(
                path,
                media_type="text/css",
                headers={
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-store",
                },
            )

        async def evaluation_js(
            user: Any = Depends(get_admin_user),
        ) -> FileResponse:
            del user
            path = _ASSET_ROOT / "evaluation_workbench.js"
            if not path.is_file() or path.is_symlink():
                raise RuntimeError("評測工作台腳本資產不可用")
            return FileResponse(
                path,
                media_type="text/javascript",
                headers={
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-store",
                },
            )

        app.add_api_route(
            _PAGE_PATH,
            evaluation_page,
            methods=["GET"],
            response_class=HTMLResponse,
            name=_PAGE_ROUTE_NAME,
            include_in_schema=False,
        )
        app.add_api_route(
            _NEW_PAGE_PATH,
            evaluation_new_page,
            methods=["GET"],
            response_class=HTMLResponse,
            name=_NEW_PAGE_ROUTE_NAME,
            include_in_schema=False,
        )
        app.add_api_route(
            _HISTORY_PAGE_PATH,
            evaluation_history_page,
            methods=["GET"],
            response_class=HTMLResponse,
            name=_HISTORY_PAGE_ROUTE_NAME,
            include_in_schema=False,
        )
        app.add_api_route(
            _CSS_PATH,
            evaluation_css,
            methods=["GET"],
            name=_CSS_ROUTE_NAME,
            include_in_schema=False,
        )
        app.add_api_route(
            _JS_PATH,
            evaluation_js,
            methods=["GET"],
            name=_JS_ROUTE_NAME,
            include_in_schema=False,
        )
        app.include_router(
            create_api_router(
                _workbench(), get_admin_user, user_dependency=get_current_user
            )
        )
    _move_owned_routes_before_root_mount(app)


def _unregister_routes(app: Any) -> None:
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if getattr(route, "name", None) not in _OWN_ROUTE_NAMES
    ]


class Event:
    """在 Open WebUI startup/enable 掛載管理員工作台與使用者權限聊天匯出 API。"""

    async def event(
        self,
        event: dict[str, Any],
        __event_name__: str | None = None,
        __id__: str | None = None,
        __app__: Any = None,
        **_kwargs: Any,
    ) -> None:
        if __app__ is None:
            return
        subject = event.get("subject") if isinstance(event, dict) else None
        is_this_function = isinstance(subject, dict) and subject.get("id") == __id__
        if __event_name__ == "system.startup.completed":
            _register_routes(__app__)
            _workbench().recover_after_restart()
        elif __event_name__ == "function.enable_started" and is_this_function:
            _register_routes(__app__)
            _workbench().recover_after_restart()
        elif __event_name__ == "system.shutdown.started":
            _workbench().shutdown()
            _unregister_routes(__app__)
        elif __event_name__ == "function.disable_started" and is_this_function:
            _workbench().shutdown()
            _unregister_routes(__app__)
