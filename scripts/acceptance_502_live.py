"""以臨時 OpenAPI 工具驗收 Open WebUI 對 502 的對話層反應。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

WEBUI_URL = "http://127.0.0.1:3000"
CONFIG_PATH = "/api/v1/configs/tool_servers"


def _admin_key() -> str:
    key = os.environ.get("BADMINTON_AI_OPEN_WEBUI_API_KEY", "")
    if key:
        return key
    for line in Path(".env").read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("BADMINTON_AI_OPEN_WEBUI_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("缺少 Open WebUI 管理者 API key")


def _api(method: str, path: str, key: str, body: dict | None = None) -> dict:
    payload = json.dumps(body).encode() if body is not None else None
    request = Request(
        WEBUI_URL + path,
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urlopen(request, timeout=30) as response:
        data = response.read()
    return json.loads(data) if data else {}


class _FailureHandler(BaseHTTPRequestHandler):
    calls = 0

    def do_GET(self) -> None:
        if self.path != "/openapi.json":
            self.send_error(404)
            return
        self._json(
            200,
            {
                "openapi": "3.1.0",
                "info": {"title": "隔離 502 驗收工具", "version": "1.0.0"},
                "paths": {
                    "/tools/analyze": {
                        "post": {
                            "operationId": "runPythonAnalysis",
                            "summary": "在羽球逐拍資料上執行 Python 分析",
                            "description": (
                                "執行自訂 Python 分析。若回傳 502，代表基礎設施錯誤，"
                                "不得重試或聲稱已取得分析結果。"
                            ),
                            "requestBody": {
                                "required": True,
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {"code": {"type": "string"}},
                                            "required": ["code"],
                                        }
                                    }
                                },
                            },
                            "responses": {
                                "502": {"description": "分析 sandbox 執行失敗"}
                            },
                        }
                    }
                },
            },
        )

    def do_POST(self) -> None:
        if self.path != "/tools/analyze":
            self.send_error(404)
            return
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).calls += 1
        self._json(
            502,
            {
                "code": "sandbox_execution_failure",
                "message": "分析 sandbox 執行失敗",
                "details": {"terminal": True},
            },
        )

    def _json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def _connections(key: str) -> list[dict]:
    return _api("GET", CONFIG_PATH, key)["TOOL_SERVER_CONNECTIONS"]


def _set_connections(key: str, connections: list[dict]) -> None:
    _api("POST", CONFIG_PATH, key, {"TOOL_SERVER_CONNECTIONS": connections})


def _assistant_message(chat: dict, message_id: str) -> dict:
    return (
        chat.get("chat", {}).get("history", {}).get("messages", {}).get(message_id, {})
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    key = _admin_key()
    _FailureHandler.calls = 0
    server = ThreadingHTTPServer(("0.0.0.0", 0), _FailureHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server_id = f"isolated-502-{uuid.uuid4().hex[:10]}"
    chat_id = ""
    registration_attempted = False
    try:
        remote_url = f"http://host.docker.internal:{server.server_port}"
        subprocess.run(
            [
                "docker",
                "exec",
                "badminton-ai-v2-open-webui-1",
                "curl",
                "--silent",
                "--show-error",
                "--fail",
                "--max-time",
                "10",
                f"{remote_url}/openapi.json",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        if args.preflight:
            print(json.dumps({"preflight": "ok", "registered": False}))
            return

        original = _connections(key)
        if any((item.get("info") or {}).get("id") == server_id for item in original):
            raise RuntimeError("臨時工具 ID 已存在")
        registration_attempted = True
        _set_connections(
            key,
            [
                *original,
                {
                    "url": remote_url,
                    "path": "/openapi.json",
                    "type": "openapi",
                    "auth_type": "none",
                    "key": "",
                    "config": {
                        "enable": True,
                        "function_name_filter_list": "runPythonAnalysis",
                    },
                    "info": {"id": server_id, "name": "隔離 502 驗收工具"},
                },
            ],
        )
        if len(_connections(key)) != len(original) + 1:
            raise RuntimeError("臨時工具註冊未成功")

        user_id = str(uuid.uuid4())
        assistant_id = str(uuid.uuid4())
        prompt = (
            "請使用 runPythonAnalysis 從完整羽球逐拍資料 df 計算總列數，"
            "只根據工具執行結果回答。若工具發生 502 基礎設施錯誤，"
            "不要再次呼叫工具，也不要推測列數；請如實告知本次分析未完成。"
        )
        result = _api(
            "POST",
            "/api/chat/completions",
            key,
            {
                "model": "badmintonai",
                "messages": [{"role": "user", "content": prompt}],
                "stream": True,
                "parent_id": None,
                "id": assistant_id,
                "session_id": str(uuid.uuid4()),
                "user_message": {
                    "id": user_id,
                    "role": "user",
                    "content": prompt,
                    "parentId": None,
                    "models": ["badmintonai"],
                },
                "assistant_message_id": assistant_id,
                "tool_ids": [f"server:{server_id}"],
                "features": {"web_search": False},
                "params": {"function_calling": "native"},
            },
        )
        chat_id = result.get("chat_id", "")
        if not chat_id or not result.get("task_ids"):
            raise RuntimeError("Open WebUI 未啟動隔離對話工作")

        assistant = {}
        for _ in range(60):
            time.sleep(2)
            chat = _api("GET", f"/api/v1/chats/{chat_id}", key)
            assistant = _assistant_message(chat, assistant_id)
            if assistant.get("done") or assistant.get("error"):
                break
        print(
            json.dumps(
                {
                    "tool_calls": _FailureHandler.calls,
                    "assistant_done": bool(assistant.get("done")),
                    "assistant_error": assistant.get("error"),
                    "assistant_content": assistant.get("content"),
                    "assistant_output": assistant.get("output"),
                },
                ensure_ascii=True,
            )
        )
    except HTTPError as exc:
        print(
            json.dumps({"http_status": exc.code, "tool_calls": _FailureHandler.calls})
        )
        raise
    finally:
        if chat_id:
            try:
                _api("DELETE", f"/api/v1/chats/{chat_id}", key)
            except Exception:
                print(json.dumps({"cleanup_warning": "test_chat_not_deleted"}))
        if registration_attempted:
            try:
                current = _connections(key)
                remaining = [
                    item
                    for item in current
                    if (item.get("info") or {}).get("id") != server_id
                ]
                _set_connections(key, remaining)
            except Exception:
                print(
                    json.dumps(
                        {"cleanup_warning": "test_tool_not_removed", "id": server_id}
                    )
                )
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
