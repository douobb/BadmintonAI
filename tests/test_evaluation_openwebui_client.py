"""Open WebUI v0.11.3 adapter mock HTTP regression tests."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.evaluation_openwebui_client import (
    OpenWebUIClientError,
    OpenWebUIEvaluationClient,
    OpenWebUIStateUncertain,
    _turn_result,
)
from scripts.evaluation_runner import EvaluationRunner
from scripts.evaluation_workbench_service import _redact_secret_values


@pytest.mark.parametrize(
    "payload", [{"task_ids": ["native-task"]}, {}, {"task_ids": None}]
)
def test_manual_retry_rejects_active_or_unknown_native_task(payload: Any) -> None:
    client = object.__new__(OpenWebUIEvaluationClient)
    calls = []

    def request(method: str, path: str) -> Any:
        calls.append((method, path))
        return payload

    client._request_json = request
    with pytest.raises(OpenWebUIStateUncertain):
        client.assert_retry_idle("chat-1")
    assert calls == [("GET", "/api/tasks/chat/chat-1")]


def test_manual_retry_requires_confirmed_empty_native_task_list() -> None:
    client = object.__new__(OpenWebUIEvaluationClient)
    client._request_json = lambda method, path: {"task_ids": []}
    client.assert_retry_idle("chat-1")

    def unavailable(method: str, path: str) -> Any:
        raise ConnectionError("離線")

    client._request_json = unavailable
    with pytest.raises(OpenWebUIStateUncertain, match="無法確認"):
        client.assert_retry_idle("chat-1")


def _run_isolated_adapter_import(
    tmp_path: Path, mode: str
) -> subprocess.CompletedProcess[str]:
    project_root = Path(__file__).resolve().parents[1]
    adapter_path = project_root / "scripts" / "evaluation_openwebui_client.py"
    script = textwrap.dedent(
        r"""
        import importlib.util
        import sys
        import types
        from pathlib import Path

        project_root = Path(sys.argv[1]).resolve()
        adapter_path = Path(sys.argv[2]).resolve()
        mode = sys.argv[3]
        kept_paths = []
        for entry in sys.path:
            resolved = Path(entry or ".").resolve()
            try:
                resolved.relative_to(project_root)
            except ValueError:
                kept_paths.append(entry)
        sys.path[:] = kept_paths
        assert importlib.util.find_spec("openwebui_patch") is None

        scripts = types.ModuleType("scripts")
        scripts.__path__ = []
        runner = types.ModuleType("scripts.evaluation_runner")
        runner.TurnResult = type("TurnResult", (), {})
        sys.modules["scripts"] = scripts
        sys.modules["scripts.evaluation_runner"] = runner

        open_webui = types.ModuleType("open_webui")
        open_webui.__path__ = []
        sys.modules["open_webui"] = open_webui

        if mode == "dependency-error":
            class MissingRuntimeDependencyFinder:
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "open_webui.evaluation_observability":
                        raise ModuleNotFoundError(
                            "runtime helper dependency is missing",
                            name="missing_runtime_dependency",
                        )
                    return None

            sys.meta_path.insert(0, MissingRuntimeDependencyFinder())
            try:
                spec = importlib.util.spec_from_file_location(
                    "scripts.evaluation_openwebui_client", adapter_path
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except ModuleNotFoundError as exc:
                assert exc.name == "missing_runtime_dependency"
            else:
                raise AssertionError("helper 內部依賴錯誤不應被吞掉")
            raise SystemExit(0)

        contract = types.ModuleType("open_webui.evaluation_observability")
        required = (
            "decode_tool_payload",
            "decode_tool_result_payload",
            "decode_tool_error_payload",
            "is_unexecuted_tool_output",
        )
        for name in required:
            if mode != "old-helper" or name != "decode_tool_error_payload":
                setattr(contract, name, lambda value=None, *_args, **_kwargs: value)
        open_webui.evaluation_observability = contract
        sys.modules[contract.__name__] = contract

        spec = importlib.util.spec_from_file_location(
            "scripts.evaluation_openwebui_client", adapter_path
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        if mode == "old-helper":
            try:
                spec.loader.exec_module(module)
            except RuntimeError as exc:
                message = str(exc)
                assert "版本不相容" in message
                assert "decode_tool_error_payload" in message
            else:
                raise AssertionError("舊版 helper 必須明確拒絕")
            raise SystemExit(0)

        spec.loader.exec_module(module)
        assert module._tool_event_contract is contract
        assert module._tool_event_contract.decode_tool_payload("native payload") == "native payload"
        """
    )
    return subprocess.run(
        [sys.executable, "-c", script, str(project_root), str(adapter_path), mode],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def test_adapter_loads_installed_runtime_helper_without_repo_patch_path(
    tmp_path: Path,
) -> None:
    result = _run_isolated_adapter_import(tmp_path, "runtime-helper")
    assert result.returncode == 0, result.stderr


def test_adapter_rejects_runtime_helper_missing_shared_contract_api(
    tmp_path: Path,
) -> None:
    result = _run_isolated_adapter_import(tmp_path, "old-helper")
    assert result.returncode == 0, result.stderr


def test_adapter_does_not_hide_runtime_helper_dependency_error(tmp_path: Path) -> None:
    result = _run_isolated_adapter_import(tmp_path, "dependency-error")
    assert result.returncode == 0, result.stderr


def test_adapter_uses_local_shared_contract_during_repository_development() -> None:
    from scripts import evaluation_openwebui_client as adapter

    assert (
        adapter._tool_event_contract.__name__
        == "openwebui_patch.evaluation_observability"
    )
    assert adapter._tool_event_contract.decode_tool_payload(
        '{"analysis_runs_remaining":0}'
    ) == {"analysis_runs_remaining": 0}


class _Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]


class _MockOpenWebUI:
    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.chats: dict[str, dict[str, Any]] = {}
        self.folders: dict[str, dict[str, Any]] = {}
        self.completion_requests: list[dict[str, Any]] = []
        self.model_tool_ids = ["server:badminton-ai"]
        self.search_count = 0
        self.search_visible_after = 0
        self.timeout_after_create = False
        self.timeout_after_completion = False
        self.completion_status: int | None = None
        self.folder_list_status: int | None = None
        self.folder_create_status: int | None = None
        self.timeout_after_folder_create = False
        self.chat_get_error = False
        self.next_assistant: dict[str, Any] = {
            "content": "分析完成",
            "done": True,
            "output": [
                {
                    "type": "function_call",
                    "name": "runPythonAnalysis",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "tool-1",
                    "output": "rows=3",
                },
            ],
            "usage": {
                "input_tokens": 14,
                "output_tokens": 8,
                "total_tokens": 22,
            },
            "embeds": [
                '<meta name="badmintonai-chart-fingerprint" content="sha256:'
                + "a" * 64
                + '"><div>real chart</div>'
            ],
        }

    def urlopen(self, request: Any, *, timeout: float) -> _Response:
        del timeout
        self.requests.append(request)
        parsed = urlsplit(request.full_url)
        path = parsed.path
        body = json.loads(request.data) if request.data else None

        if request.method == "GET" and path == "/api/models":
            return _json_response(
                {
                    "data": [
                        {
                            "id": "badmintonai",
                            "name": "BadmintonAI",
                            "info": {
                                "updated_at": "2026-09-25T12:00:00Z",
                                "meta": {"toolIds": self.model_tool_ids},
                            },
                        }
                    ]
                }
            )

        if request.method == "GET" and path == "/api/v1/folders/":
            if self.folder_list_status is not None:
                return _json_response(
                    {"detail": "folder list unavailable"},
                    status=self.folder_list_status,
                )
            return _json_response(
                [
                    {
                        "id": folder_id,
                        "name": folder["name"],
                        "parent_id": folder["parent_id"],
                        "meta": {"icon": "folder"},
                    }
                    for folder_id, folder in self.folders.items()
                ]
            )

        if request.method == "GET" and path.startswith("/api/v1/folders/"):
            folder_id = path.rsplit("/", 1)[-1]
            folder = self.folders.get(folder_id)
            if folder is None:
                return _json_response({"detail": "not found"}, status=404)
            return _json_response({"id": folder_id, **folder})

        if request.method == "POST" and path == "/api/v1/folders/":
            if self.folder_create_status is not None:
                return _json_response(
                    {"detail": "folder create unavailable"},
                    status=self.folder_create_status,
                )
            folder_id = f"folder-{len(self.folders) + 1}"
            self.folders[folder_id] = {
                "name": body["name"],
                "parent_id": body["parent_id"],
                "meta": body["meta"],
            }
            if self.timeout_after_folder_create:
                self.timeout_after_folder_create = False
                raise TimeoutError("simulated folder create timeout")
            return _json_response({"id": folder_id, **self.folders[folder_id]})

        if request.method == "GET" and path == "/api/v1/chats/search":
            self.search_count += 1
            query = parse_qs(parsed.query)["text"][0]
            page = int(parse_qs(parsed.query).get("page", ["1"])[0])
            matches = [
                {"id": chat_id, "title": chat["chat"]["title"]}
                for chat_id, chat in self.chats.items()
                if chat["chat"]["title"] == query
                and self.search_count >= self.search_visible_after
            ]
            return _json_response(matches if page == 1 else [])

        if request.method == "POST" and path == "/api/v1/chats/new":
            chat_id = f"chat-{len(self.chats) + 1}"
            self.chats[chat_id] = {
                "id": chat_id,
                "user_id": "test-user",
                "folder_id": body.get("folder_id"),
                "chat": {
                    **body["chat"],
                    "history": {"currentId": None, "messages": {}},
                },
            }
            if self.timeout_after_create:
                self.timeout_after_create = False
                raise TimeoutError("simulated create timeout")
            return _json_response(self.chats[chat_id])

        if request.method == "GET" and path.startswith("/api/v1/chats/"):
            if self.chat_get_error:
                self.chat_get_error = False
                raise TimeoutError("simulated chat read timeout")
            chat_id = path.rsplit("/", 1)[-1]
            if chat_id not in self.chats:
                return _json_response({"detail": "not found"}, status=404)
            return _json_response(self.chats[chat_id])

        if request.method == "POST" and path == "/api/chat/completions":
            self.completion_requests.append(body)
            if self.completion_status is not None:
                return _json_response(
                    {"detail": "provider rate limit org-id-987"},
                    status=self.completion_status,
                )
            chat_id = body["chat_id"]
            history = self.chats[chat_id]["chat"]["history"]
            user_message = dict(body["user_message"])
            assistant_id = body["id"]
            user_message["childrenIds"] = [assistant_id]
            history["messages"][user_message["id"]] = user_message
            assistant = {
                "id": assistant_id,
                "role": "assistant",
                "parentId": user_message["id"],
                "childrenIds": [],
                **self.next_assistant,
            }
            history["messages"][assistant_id] = assistant
            history["currentId"] = assistant_id
            if self.timeout_after_completion:
                self.timeout_after_completion = False
                assistant["done"] = False
                raise TimeoutError("simulated completion timeout")
            return _json_response(
                {"status": True, "chat_id": chat_id, "task_ids": ["task-1"]}
            )

        raise AssertionError(f"unexpected request: {request.method} {path}")

    def finish_pending_assistants(self) -> None:
        for chat in self.chats.values():
            messages = chat["chat"]["history"]["messages"]
            for message in messages.values():
                if message.get("role") == "assistant":
                    message["done"] = True


def _json_response(payload: Any, status: int = 200) -> _Response:
    return _Response(json.dumps(payload, ensure_ascii=False).encode("utf-8"), status)


def _client(
    mock: _MockOpenWebUI, *, max_wait_seconds: float = 0
) -> OpenWebUIEvaluationClient:
    return OpenWebUIEvaluationClient(
        base_url="http://open-webui:8080",
        api_key="secret-test-key",
        model_id="badmintonai",
        timeout_seconds=1,
        poll_interval_seconds=0,
        max_wait_seconds=max_wait_seconds,
        urlopen=mock.urlopen,
        sleep=lambda _seconds: None,
    )


def _create_chat(client: OpenWebUIEvaluationClient) -> str:
    return client.create_conversation(
        "run-1", "1", "run-1:1:conversation", {"model": "badmintonai"}
    )


def test_turns_use_same_chat_and_actual_assistant_chart_tool_and_usage() -> None:
    mock = _MockOpenWebUI()
    client = _client(mock)
    chat_id = _create_chat(client)

    first = client.send_turn(chat_id, "原始題目？", "operation-one")
    second = client.send_turn(chat_id, "使用者補答", "operation-two")

    assert chat_id == "chat-1"
    assert first.messages[0]["content"] == "原始題目？"
    assert second.messages[0]["content"] == "使用者補答"
    assert first.usage == {
        "input_tokens": 14,
        "output_tokens": 8,
        "total_tokens": 22,
    }
    assert first.tool_calls == mock.next_assistant["output"][:1]
    assert first.messages[1]["output"] == mock.next_assistant["output"]
    assert first.charts[0]["mime_type"] == "text/html"
    assert first.charts[0]["fingerprint"] == "a" * 64
    assert first.messages[1]["embeds"] == mock.next_assistant["embeds"]
    assert first.awaiting_clarification is False

    first_body, second_body = mock.completion_requests
    assert first_body["parent_id"] is None
    assert second_body["parent_id"] == first_body["id"]
    assert second_body["chat_id"] == chat_id
    assert "assistant_message_id" not in first_body
    assert first_body["badmintonai_evaluation"] is True
    assert first_body["badmintonai_operation_id"] == "operation-one"
    assert second_body["messages"] == [{"role": "user", "content": "使用者補答"}]
    assert first_body["model"] == second_body["model"] == "badmintonai"
    assert first_body["tool_ids"] == second_body["tool_ids"] == ["server:badminton-ai"]
    assert "params" not in first_body
    assert "features" not in first_body
    assert all(
        request.get_header("Authorization") == "Bearer secret-test-key"
        for request in mock.requests
    )


def test_persisted_timeout_is_terminal_failure_without_resending_completion() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "",
        "done": True,
        "error": {"content": "模型服務等待逾時；本輪已記錄失敗，不會自動重送。"},
    }
    client = _client(mock)
    chat_id = _create_chat(client)

    result = client.send_turn(chat_id, "一題長分析", "timeout-operation")

    assert result.error is not None
    assert result.error["retryable"] is False
    assert "逾時" in result.error["message"]
    assert len(mock.completion_requests) == 1


def test_conversation_export_keeps_q4_multi_turn_active_branch_order(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "scripts.export_chat_html._plotly_javascript", lambda: "/* bundled */"
    )
    mock = _MockOpenWebUI()
    mock.chats["chat-q4"] = {
        "id": "chat-q4",
        "user_id": "test-user",
        "chat": {
            "history": {
                "currentId": "a2",
                "messages": {
                    "u1": {
                        "id": "u1",
                        "parentId": None,
                        "role": "user",
                        "content": "Q4 原始題目。",
                    },
                    "a1": {
                        "id": "a1",
                        "parentId": "u1",
                        "role": "assistant",
                        "content": "首輪追問。",
                    },
                    "u2": {
                        "id": "u2",
                        "parentId": "a1",
                        "role": "user",
                        "content": "補答內容。",
                    },
                    "a2": {
                        "id": "a2",
                        "parentId": "u2",
                        "role": "assistant",
                        "content": "最終回答 **完成**。",
                    },
                    "a1-branch": {
                        "id": "a1-branch",
                        "parentId": "u1",
                        "role": "assistant",
                        "content": "非目前分支，不得顯示。",
                    },
                },
            }
        },
    }

    document = _client(mock).export_chat_html("chat-q4", theme="auto")

    positions = [
        document.index(content)
        for content in ("Q4 原始題目。", "首輪追問。", "補答內容。", "最終回答")
    ]
    assert positions == sorted(positions)
    assert "非目前分支" not in document
    assert "<strong>完成</strong>" in document
    assert '<html lang="zh-Hant" data-theme="auto">' in document


def test_workbench_preview_exports_saved_structural_plotly_figure(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.export_chat_html._plotly_javascript",
        lambda: (_ for _ in ()).throw(AssertionError("不應使用本機 Plotly")),
    )
    monkeypatch.setattr(
        "scripts.evaluation_report_html.fetch_plotly_javascript",
        lambda: "/*! plotly.js v3.4.0 */ window.Plotly = {};",
    )
    mock = _MockOpenWebUI()
    embed_payload = {
        "charts": [
            {
                "title": "Q3 已保存圖表",
                "figure": {
                    "data": [
                        {
                            "type": "bar",
                            "x": ["A"],
                            "y": [2],
                            "future_trace_option": True,
                        }
                    ],
                    "layout": {},
                },
            }
        ]
    }
    embed = (
        '<script id="plotly-figure-data" type="application/json">'
        + json.dumps(embed_payload, ensure_ascii=False, separators=(",", ":"))
        + "</script>"
    )
    mock.chats["chat-q3"] = {
        "id": "chat-q3",
        "user_id": "test-user",
        "chat": {
            "history": {
                "currentId": "a3",
                "messages": {
                    "u3": {
                        "id": "u3",
                        "parentId": None,
                        "role": "user",
                        "content": "Q3 原題。",
                    },
                    "a3": {
                        "id": "a3",
                        "parentId": "u3",
                        "role": "assistant",
                        "content": "圖表分析結果。",
                        "embeds": [embed],
                    },
                },
            }
        },
    }

    document = _client(mock).export_chat_html("chat-q3")

    assert 'class="chart-card"' in document
    assert "Q3 已保存圖表" in document
    assert "future_trace_option" in document
    assert "plotly.js v3.4.0" in document
    assert "Plotly.newPlot" in document
    assert "無法匯出" not in document


def test_workbench_preview_redacts_chat_before_embedding_plotly(monkeypatch) -> None:
    bundle = (
        "/*! plotly.js v3.4.0 */\n"
        'window.Plotly = {mapboxAccessToken:{valType:"string",dflt:null}};'
    )
    monkeypatch.setattr(
        "scripts.evaluation_report_html.fetch_plotly_javascript", lambda: bundle
    )
    mock = _MockOpenWebUI()
    embed = (
        '<script id="plotly-figure-data" type="application/json">'
        '{"charts":[{"title":"測試圖","figure":{"data":'
        '[{"type":"bar","x":["A"],"y":[1]}],"layout":{}}}]}'
        "</script>"
    )
    mock.chats["chat-chart"] = {
        "id": "chat-chart",
        "chat": {
            "history": {
                "currentId": "a1",
                "messages": {
                    "u1": {
                        "id": "u1",
                        "parentId": None,
                        "role": "user",
                        "content": "Authorization: test-key",
                    },
                    "a1": {
                        "id": "a1",
                        "parentId": "u1",
                        "role": "assistant",
                        "content": "圖表結果。",
                        "embeds": [embed],
                    },
                },
            }
        },
    }

    document = _client(mock).export_chat_html(
        "chat-chart",
        redact_value=lambda value: _redact_secret_values(value, "test-key"),
    )

    assert "test-key" not in document
    assert "Authorization: [REDACTED]" in document
    assert bundle in document
    assert 'mapboxAccessToken:{valType:"string",dflt:null}' in document


def test_folder_chat_creation_is_idempotent_and_uses_taipei_run_folder() -> None:
    mock = _MockOpenWebUI()
    client = _client(mock)
    kwargs = {
        "created_at": "2026-09-28T02:34:00+00:00",
        "question_count": 12,
    }

    chat_id = client.create_conversation_in_folder(
        "1234567890abcdef1234567890abcdef",
        "7",
        "stable-key",
        {"model": "badmintonai"},
        **kwargs,
    )
    repeated_id = client.create_conversation_in_folder(
        "1234567890abcdef1234567890abcdef",
        "7",
        "stable-key",
        {"model": "badmintonai"},
        **kwargs,
    )

    assert chat_id == repeated_id == "chat-1"
    assert len(mock.folders) == 2
    root = next(folder for folder in mock.folders.values() if folder["name"] == "評測")
    run = next(folder for folder in mock.folders.values() if folder["parent_id"])
    assert run["parent_id"] == "folder-1"
    assert run["name"] == "2026-09-28 1034（12題）-1234567890ab"
    assert mock.chats[chat_id]["chat"]["title"] == "Q7"
    assert mock.chats[chat_id]["folder_id"] == "folder-2"
    created_chat_request = next(
        request
        for request in mock.requests
        if request.method == "POST"
        and urlsplit(request.full_url).path == "/api/v1/chats/new"
    )
    assert json.loads(created_chat_request.data)["folder_id"] == "folder-2"
    assert root["parent_id"] is None


def test_folder_api_failure_never_creates_an_unfiled_chat() -> None:
    mock = _MockOpenWebUI()
    mock.folder_list_status = 403
    client = _client(mock)

    with pytest.raises(OpenWebUIClientError, match="HTTP 403"):
        client.create_conversation_in_folder(
            "run-1",
            "1",
            "stable-key",
            {"model": "badmintonai"},
            created_at="2026-09-28T02:34:00+00:00",
            question_count=1,
        )

    assert not mock.chats
    assert not any(
        request.method == "POST"
        and urlsplit(request.full_url).path == "/api/v1/chats/new"
        for request in mock.requests
    )


def test_folder_creation_rejection_never_falls_back_to_unfiled_chat() -> None:
    mock = _MockOpenWebUI()
    mock.folder_create_status = 403
    client = _client(mock)

    with pytest.raises(OpenWebUIClientError, match="HTTP 403"):
        client.create_conversation_in_folder(
            "run-1",
            "1",
            "stable-key",
            {"model": "badmintonai"},
            created_at="2026-09-28T02:34:00+00:00",
            question_count=1,
        )

    assert not mock.chats
    assert mock.folders == {}


def test_uncertain_folder_create_is_reconciled_before_chat_creation() -> None:
    mock = _MockOpenWebUI()
    mock.timeout_after_folder_create = True
    client = _client(mock)

    chat_id = client.create_conversation_in_folder(
        "run-1",
        "1",
        "stable-key",
        {"model": "badmintonai"},
        created_at="2026-09-28T02:34:00+00:00",
        question_count=1,
    )

    assert chat_id == "chat-1"
    assert len(mock.folders) == 2  # root and run folders; root timeout was reconciled
    assert mock.chats[chat_id]["folder_id"] is not None


def test_clarification_turns_stay_in_the_same_run_folder_chat() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "請補充比賽日期。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "name": "requestClarification",
                "status": "completed",
                "arguments": {"question": "比賽日期為何？"},
            }
        ],
    }
    client = _client(mock)
    chat_id = client.create_conversation_in_folder(
        "run-clarify",
        "1",
        "stable-key",
        {"model": "badmintonai"},
        created_at="2026-09-28T02:34:00+00:00",
        question_count=1,
    )

    first = client.send_turn(chat_id, "分析比賽", "clarify-question")
    mock.next_assistant = {"content": "已依補充資料分析。", "done": True, "output": []}
    second = client.send_turn(chat_id, "比賽日期是 9 月 1 日", "clarify-answer")

    assert first.awaiting_clarification is True
    assert second.messages[0]["content"] == "比賽日期是 9 月 1 日"
    assert {item["chat_id"] for item in mock.completion_requests} == {chat_id}
    assert mock.chats[chat_id]["folder_id"] == "folder-2"
    assert len(mock.folders) == 2


def test_same_question_number_in_different_runs_uses_separate_folders() -> None:
    mock = _MockOpenWebUI()
    client = _client(mock)
    created_at = "2026-09-28T02:34:00+00:00"

    first = client.create_conversation_in_folder(
        "run-first",
        "1",
        "first-key",
        {"model": "badmintonai"},
        created_at=created_at,
        question_count=1,
    )
    second = client.create_conversation_in_folder(
        "run-second",
        "1",
        "second-key",
        {"model": "badmintonai"},
        created_at=created_at,
        question_count=1,
    )

    assert first != second
    assert mock.chats[first]["chat"]["title"] == "Q1"
    assert mock.chats[second]["chat"]["title"] == "Q1"
    assert mock.chats[first]["folder_id"] != mock.chats[second]["folder_id"]
    assert len(mock.folders) == 3


def test_new_run_recovers_chat_in_its_folder_after_unknown_create_outcome(
    tmp_path: Path,
) -> None:
    mock = _MockOpenWebUI()
    mock.timeout_after_create = True
    mock.search_visible_after = 3
    client = _client(mock)
    question_file = tmp_path / "questions.txt"
    question_file.write_text("1: 原始題目？\n", encoding="utf-8")
    store_path = tmp_path / "run.json"
    runner = EvaluationRunner.start(
        client,
        question_file=question_file,
        store_path=store_path,
        model_snapshot={"model": "badmintonai"},
        data_snapshot={"version": "dataset-v1"},
    )

    interrupted = runner.run_pending()
    assert interrupted["questions"][0]["status"] == "running"
    assert interrupted["questions"][0]["conversation_id"] is None

    resumed = EvaluationRunner.resume(client, store_path=store_path)
    completed = resumed.run_pending()

    question = completed["questions"][0]
    assert question["status"] == "completed"
    assert question["creation_attempts"][0]["recovered"] is True
    assert len(mock.chats) == 1
    assert len(mock.folders) == 2
    assert mock.chats[question["conversation_id"]]["folder_id"] == "folder-2"
    assert (
        sum(
            request.method == "POST"
            and urlsplit(request.full_url).path == "/api/v1/chats/new"
            for request in mock.requests
        )
        == 1
    )


def test_completion_uses_current_model_tool_binding() -> None:
    mock = _MockOpenWebUI()
    client = _client(mock)
    chat_id = _create_chat(client)
    mock.model_tool_ids = ["server:updated-tool"]

    client.send_turn(chat_id, "原題", "current-tools")

    assert mock.completion_requests[0]["tool_ids"] == ["server:updated-tool"]


def test_model_snapshot_is_minimal_and_does_not_include_credentials() -> None:
    mock = _MockOpenWebUI()
    snapshot = _client(mock).get_model_snapshot()

    assert snapshot["model_id"] == "badmintonai"
    assert snapshot["model_name"] == "BadmintonAI"
    assert snapshot["updated_at"] == "2026-09-25T12:00:00Z"
    assert snapshot["tool_ids"] == ["server:badminton-ai"]
    assert isinstance(snapshot["captured_at"], str)
    assert "secret-test-key" not in json.dumps(snapshot)
    assert len(mock.requests) == 1
    assert mock.requests[0].method == "GET"


def test_clarification_prompt_is_detected_and_missing_usage_stays_null() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "您指的是哪一場比賽？",
        "done": True,
        "output": [],
    }
    client = _client(mock)

    result = client.send_turn(_create_chat(client), "請分析", "clarification-test")

    assert result.awaiting_clarification is True
    assert result.clarification_signal == "text_candidate"
    assert result.usage is None


def test_completed_answer_without_persisted_usage_remains_null() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "完成。",
        "done": True,
        "output": [],
    }
    client = _client(mock)

    result = client.send_turn(_create_chat(client), "一般題目", "no-usage")

    assert result.messages[-1]["content"] == "完成。"
    assert result.usage is None


def test_late_clarification_with_choices_citation_and_followup_note_is_detected() -> (
    None
):
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": (
            "從防守轉攻擊得分不是資料中可直接辨識的欄位。你希望採哪種口徑？\n"
            "1. 按球種序列判定。\n"
            "2. 按場區序列判定。\n"
            "3. 使用自訂判準。\n\n"
            "確認口徑後，我會再依 proxy 進行分析。[1]"
        ),
        "done": True,
        "output": [],
    }
    client = _client(mock)

    result = client.send_turn(_create_chat(client), "分析 Q85", "q85-clarification")

    assert result.awaiting_clarification is True
    assert result.clarification_signal == "text_candidate"
    assert result.usage is None


def test_quoted_and_rhetorical_questions_in_an_answer_are_not_clarifications() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": (
            "一般回答會引用「你希望採哪種口徑？」作為例子，但本文使用固定判準。\n"
            "1. 觀察攻守轉換。\n"
            "2. 統計該回合得分。\n"
            "這項差異是否值得討論？以上是結果摘要。"
        ),
        "done": True,
        "output": [],
    }
    client = _client(mock)

    result = client.send_turn(
        _create_chat(client), "請分析結果", "ordinary-quoted-question"
    )

    assert result.awaiting_clarification is False


def test_explicit_choice_list_without_question_mark_is_detected() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "請選擇一種口徑：\n1. 按球種。\n2. 按場區。",
        "done": True,
        "output": [],
    }
    client = _client(mock)

    result = client.send_turn(_create_chat(client), "分析結果", "explicit-choice-list")

    assert result.awaiting_clarification is True
    assert result.clarification_signal == "text_candidate"


def test_completed_clarification_tool_event_takes_priority_over_wording() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "先確認口徑，之後再分析。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "name": "requestClarification",
                "status": "completed",
                "arguments": '{"question":"採哪個門檻？","options":["10 拍","12 拍"]}',
            }
        ],
    }
    result = _client(mock).send_turn(_create_chat(_client(mock)), "請分析", "marked")

    assert result.awaiting_clarification is True
    assert result.clarification_signal == "event"


def test_completed_clarification_event_can_have_empty_assistant_text() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "name": "requestClarification",
                "status": "completed",
                "arguments": '{"question":"採哪個門檻？"}',
            }
        ],
    }

    result = _client(mock).send_turn(
        _create_chat(_client(mock)), "請分析", "empty-clarification"
    )

    assert result.clarification_signal == "event"
    assert result.error is None


def test_empty_answer_after_unfinished_tool_call_is_a_failure() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "name": "runPythonAnalysis",
                "status": "completed",
                "call_id": "analysis-without-output",
            }
        ],
    }

    result = _client(mock).send_turn(
        _create_chat(_client(mock)), "請分析", "empty-answer"
    )

    assert result.clarification_signal == "none"
    assert result.error is not None
    assert result.error["retryable"] is False


def test_responses_output_message_counts_as_visible_answer() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "",
        "done": True,
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "已有結果"}],
            }
        ],
    }

    result = _client(mock).send_turn(
        _create_chat(_client(mock)), "請分析", "output-answer"
    )

    assert result.error is None


def test_failed_clarification_tool_is_not_a_waiting_signal() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "工具未完成，無法判定。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "name": "requestClarification",
                "status": "failed",
            }
        ],
    }
    result = _client(mock).send_turn(
        _create_chat(_client(mock)), "請分析", "failed-mark"
    )

    assert result.awaiting_clarification is False
    assert result.clarification_signal == "tool_failed"


def test_answer_with_analysis_and_optional_followup_is_not_waiting() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "統計結果為 31.8%。請確認是否也要看其他場次？",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "name": "runPythonAnalysis",
                "status": "completed",
            }
        ],
    }
    result = _client(mock).send_turn(_create_chat(_client(mock)), "請分析", "answered")

    assert result.awaiting_clarification is False
    assert result.clarification_signal == "none"


def test_stdout_only_probe_is_not_reported_as_failed_analysis() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "欄位型別已確認，接下來才會保存正式統計。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "id": "probe-only",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "probe-only",
                "output": [
                    {
                        "type": "input_text",
                        "text": '{"result_id":null,"artifacts":[],"stdout_preview":"landing_area 是 object"}',
                    }
                ],
            },
        ],
    }

    client = _client(mock)
    result = client.send_turn(_create_chat(client), "先探查欄位", "stdout-probe")

    assert result.error is None


def test_failed_analysis_is_not_mislabeled_as_completed() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "分析程式失敗，因此無法提供數值。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "id": "analysis-1",
                "name": "runPythonAnalysis",
                "status": "failed",
            },
            {
                "type": "function_call_output",
                "call_id": "analysis-1",
                "status": "failed",
                "output": [{"type": "input_text", "text": '{"error":"HTTP 422"}'}],
            },
        ],
    }

    result = _client(mock).send_turn(
        _create_chat(_client(mock)), "請分析", "failed-analysis"
    )

    assert result.error == {
        "message": "Python 分析工具未成功完成；回答需要人工複核",
        "retryable": False,
    }


def test_stdout_probe_after_value_error_does_not_clear_analysis_failure() -> None:
    mock = _MockOpenWebUI()
    answer = "反手後對手下一拍直接終局得分：179／1,028（17.4%）。"
    mock.next_assistant = {
        "content": answer,
        "done": True,
        "output": [
            {
                "type": "function_call",
                "id": "analysis-value-error",
                "name": "runPythonAnalysis",
                "status": "failed",
            },
            {
                "type": "function_call_output",
                "call_id": "analysis-value-error",
                "status": "failed",
                "output": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {"error": "ValueError while converting ball_round"}
                        ),
                    }
                ],
            },
            {
                "type": "function_call",
                "id": "stdout-probe-after-error",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "stdout-probe-after-error",
                "status": "completed",
                "output": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {
                                "result_id": None,
                                "artifacts": [],
                                "stdout_preview": answer,
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
            },
        ],
    }

    client = _client(mock)
    result = client.send_turn(_create_chat(client), "分析反手後得分", "q55-probe")

    assert result.error == {
        "message": "Python 分析工具未成功完成；回答需要人工複核",
        "retryable": False,
    }
    assert result.messages[-1]["content"] == answer


def test_formal_analysis_requires_saved_file_manifest() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "已完成分析。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "id": "analysis-with-empty-manifest",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "analysis-with-empty-manifest",
                "status": "completed",
                "output": [
                    {
                        "type": "input_text",
                        "text": json.dumps({"result_id": "a" * 48, "artifacts": []}),
                    }
                ],
            },
        ],
    }

    client = _client(mock)
    result = client.send_turn(_create_chat(client), "請保存分析", "empty-manifest")

    assert result.error == {
        "message": "Python 分析工具未成功完成；回答需要人工複核",
        "retryable": False,
    }


def test_probe_success_does_not_hide_terminal_analysis_error_when_chart_exists() -> (
    None
):
    mock = _MockOpenWebUI()
    retry_limit = {
        "code": "analysis_retry_limit",
        "details": {"terminal": True},
    }
    mock.next_assistant = {
        "content": "已完成輸出。",
        "done": True,
        "embeds": ["<div>existing chart</div>"],
        "output": [
            {
                "type": "function_call",
                "id": "probe-1",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "probe-1",
                "output": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {
                                "result_id": None,
                                "artifacts": [],
                                "stdout_preview": "欄位探查完成",
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
            },
            {
                "type": "function_call",
                "id": "analysis-terminal",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "analysis-terminal",
                "output": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {"error": "HTTP error 429: " + json.dumps(retry_limit)}
                        ),
                    }
                ],
            },
        ],
    }

    client = _client(mock)
    result = client.send_turn(
        _create_chat(client), "請探查後分析", "probe-then-terminal"
    )

    assert result.error == {
        "code": "analysis_retry_limit",
        "message": "分析修正次數已達上限；本輪已停止，未完成部分不視為已驗證結論。",
        "retryable": False,
    }


def test_analysis_message_limit_precedes_terminally_skipped_render_failure() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "已完成部分分析。",
        "done": True,
        "embeds": ["<div>existing chart</div>"],
        "output": [
            {
                "type": "function_call",
                "id": "analysis-12",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "analysis-12",
                "status": "completed",
                "output": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {
                                "result_id": "a" * 48,
                                "result_fingerprint": "f" * 64,
                                "analysis_runs_remaining": 0,
                                "artifacts": [{"relative_path": "summary.json"}],
                            }
                        ),
                    }
                ],
            },
            {
                "type": "function_call",
                "id": "analysis-over-budget",
                "name": "runPythonAnalysis",
                "status": "failed",
            },
            {
                "type": "function_call_output",
                "call_id": "analysis-over-budget",
                "status": "failed",
                "output": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {
                                "error": "HTTP error 429: "
                                + json.dumps(
                                    {
                                        "code": "analysis_message_limit",
                                        "details": {"terminal": True},
                                    }
                                )
                            }
                        ),
                    }
                ],
            },
            {
                "type": "function_call",
                "id": "render-skipped",
                "name": "renderAnalysisChart",
                "status": "failed",
            },
            {
                "type": "function_call_output",
                "call_id": "render-skipped",
                "status": "failed",
                "output": [
                    {
                        "type": "input_text",
                        "text": "本輪已因工具終止狀態停止；工具呼叫未執行。",
                    }
                ],
            },
        ],
    }

    client = _client(mock)
    result = client.send_turn(
        _create_chat(client), "分析並畫圖", "analysis-cap-skipped-render"
    )

    assert result.error == {
        "code": "analysis_message_limit",
        "message": "分析執行次數已達上限；本輪已停止，未完成部分不視為已驗證結論。",
        "retryable": False,
    }
    assert result.charts, "已發布圖表仍保留"
    assert any("已完成部分分析。" in str(message) for message in result.messages)


def test_budget_exhausted_fixed_failure_is_not_misclassified_as_completed() -> None:
    mock = _MockOpenWebUI()
    failure_text = (
        "本則回答的分析執行次數已用完，且仍有未解錯誤或沒有可驗證的保存結果；"
        "分析未完成，不提供未驗證數值。"
    )
    mock.next_assistant = {
        "content": failure_text,
        "done": True,
        "output": [
            {
                "type": "function_call",
                "id": "last-probe",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "last-probe",
                "status": "completed",
                "output": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {
                                "result_id": None,
                                "artifacts": [],
                                "stdout_preview": "僅探查，沒有正式產物",
                                "analysis_runs_remaining": 0,
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
            },
            {
                "type": "message",
                "id": "budget-failure-message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": failure_text}],
            },
        ],
    }

    client = _client(mock)
    result = client.send_turn(
        _create_chat(client), "請分析", "budget-probe-no-evidence"
    )

    assert result.error == {
        "code": "analysis_message_limit",
        "message": "分析執行次數已達上限，且沒有可驗證的完整結果；未完成部分不視為已驗證結論。",
        "retryable": False,
    }


def test_executed_render_failure_still_wins_over_existing_chart() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "部分分析完成。",
        "done": True,
        "embeds": ["<div>existing chart</div>"],
        "output": [
            {
                "type": "message",
                "id": "budget-terminal",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": (
                            "本則回答的分析執行次數已用完，且仍有未解錯誤或沒有可驗證的保存結果；"
                            "分析未完成，不提供未驗證數值。"
                        ),
                    }
                ],
            },
            {
                "type": "function_call",
                "id": "analysis-limit",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "analysis-limit",
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
            {
                "type": "function_call",
                "id": "render-executed-fail",
                "name": "renderAnalysisChart",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "render-executed-fail",
                "status": "completed",
                "output": [
                    {"type": "input_text", "text": '{"code":"render_code_error"}'}
                ],
            },
        ],
    }

    client = _client(mock)
    result = client.send_turn(
        _create_chat(client), "分析並畫圖", "analysis-cap-real-render-failure"
    )

    assert result.error is not None
    assert result.error.get("code") == "render_failed"


def test_later_saved_analysis_result_recovers_from_prior_repairable_error() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "修正後已完成分析。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "id": "analysis-failed",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "analysis-failed",
                "output": [
                    {
                        "type": "input_text",
                        "text": '{"code":"analysis_code_error"}',
                    }
                ],
            },
            {
                "type": "function_call",
                "id": "analysis-success",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "analysis-success",
                "output": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {
                                "result_id": "a" * 48,
                                "artifacts": [{"relative_path": "summary.json"}],
                            }
                        ),
                    }
                ],
            },
        ],
    }

    client = _client(mock)
    result = client.send_turn(_create_chat(client), "請分析", "analysis-repaired")

    assert result.error is None


def test_failed_render_tool_is_not_mislabeled_as_completed() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "資料已整理，但互動圖沒有產生。",
        "done": True,
        "embeds": ["<div>earlier chart</div>"],
        "output": [
            {
                "type": "function_call",
                "id": "analysis-1",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call",
                "id": "render-1",
                "name": "renderAnalysisChart",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "render-1",
                "status": "completed",
                "output": [
                    {
                        "type": "input_text",
                        "text": '{"code":"render_invalid_spec","message":"safe"}',
                    }
                ],
            },
        ],
    }

    client = _client(mock)
    result = client.send_turn(_create_chat(client), "請分析並繪圖", "failed-render")

    assert result.error == {
        "code": "render_failed",
        "message": "互動圖表未能完成發布；已停止自動修正，請查看原對話中的既有結果。",
        "retryable": False,
    }


def test_render_only_failure_is_not_mislabeled_as_completed() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "前次分析已完成，但這次改圖失敗。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "id": "render-only-1",
                "name": "renderAnalysisChart",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "render-only-1",
                "status": "completed",
                "output": [
                    {
                        "type": "input_text",
                        "text": '{"code":"render_invalid_spec","message":"safe"}',
                    }
                ],
            },
        ],
    }

    client = _client(mock)
    result = client.send_turn(_create_chat(client), "改成熱圖", "render-only-failure")

    assert result.error == {
        "code": "render_failed",
        "message": "互動圖表未能完成發布；已停止自動修正，請查看原對話中的既有結果。",
        "retryable": False,
    }


def test_http_wrapped_render_failure_is_known_even_when_chat_has_a_chart() -> None:
    mock = _MockOpenWebUI()
    error_body = json.dumps(
        {"code": "render_code_error", "message": "安全錯誤"},
        ensure_ascii=False,
    )
    mock.next_assistant = {
        "content": "已完成。",
        "done": True,
        "embeds": ["<div>existing chart</div>"],
        "output": [
            {
                "type": "function_call",
                "id": "render-http-1",
                "name": "renderAnalysisChart",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "render-http-1",
                "output": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {"error": f"HTTP error 422: {error_body}"},
                            ensure_ascii=False,
                        ),
                    }
                ],
            },
        ],
    }

    client = _client(mock)
    result = client.send_turn(_create_chat(client), "改成熱圖", "render-http-fail")

    assert result.error == {
        "code": "render_failed",
        "message": "互動圖表未能完成發布；已停止自動修正，請查看原對話中的既有結果。",
        "retryable": False,
    }


def test_render_only_success_is_not_mislabeled_as_missing_analysis() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "已依前次分析資料更新圖表。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "id": "render-only-2",
                "name": "renderAnalysisChart",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "render-only-2",
                "status": "completed",
                "output": [
                    {
                        "type": "input_text",
                        "text": '{"status":"embedded","result_id":"result-1","chart_count":1}',
                    }
                ],
            },
        ],
    }

    client = _client(mock)
    result = client.send_turn(_create_chat(client), "改成熱圖", "render-only-success")

    assert result.error is None


def test_duplicate_suppressed_render_keeps_published_result_identity() -> None:
    mock = _MockOpenWebUI()
    result_id = "b" * 48
    mock.next_assistant = {
        "content": "圖表已在本則回答發布。",
        "done": True,
        "embeds": ["<div>published chart</div>"],
        "output": [
            {
                "type": "function_call",
                "id": "render-duplicate",
                "name": "renderAnalysisChart",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "render-duplicate",
                "status": "completed",
                "output": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {
                                "status": "duplicate_suppressed",
                                "result_id": result_id,
                                "chart_count": 1,
                            }
                        ),
                    }
                ],
            },
        ],
    }

    client = _client(mock)
    result = client.send_turn(
        _create_chat(client), "確認剛發布的圖表", "duplicate-render"
    )

    assert result.error is None
    assert len(result.charts) == 1
    serialized_messages = json.dumps(result.messages, ensure_ascii=False)
    assert "duplicate_suppressed" in serialized_messages
    assert result_id in serialized_messages


def test_repaired_render_failure_followed_by_success_is_completed() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "已修正圖表格式並完成附加。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "id": "render-repair-1",
                "name": "renderAnalysisChart",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "render-repair-1",
                "status": "completed",
                "output": [
                    {
                        "type": "input_text",
                        "text": '{"code":"render_invalid_spec","message":"safe"}',
                    }
                ],
            },
            {
                "type": "function_call",
                "id": "render-repair-2",
                "name": "renderAnalysisChart",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "render-repair-2",
                "status": "completed",
                "output": [
                    {
                        "type": "input_text",
                        "text": '{"status":"embedded","result_id":"result-1","chart_count":1}',
                    }
                ],
            },
        ],
    }

    client = _client(mock)
    result = client.send_turn(_create_chat(client), "修正後改成熱圖", "render-repaired")

    assert result.error is None


def test_unknown_render_state_is_not_cleared_by_later_success() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "圖表最後看似完成，但先前狀態無法確認。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "id": "render-unknown-1",
                "name": "renderAnalysisChart",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "render-unknown-1",
                "status": "completed",
                "output": [{"type": "input_text", "text": "not-json"}],
            },
            {
                "type": "function_call",
                "id": "render-unknown-2",
                "name": "renderAnalysisChart",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "render-unknown-2",
                "status": "completed",
                "output": [
                    {
                        "type": "input_text",
                        "text": '{"status":"embedded","result_id":"result-1","chart_count":1}',
                    }
                ],
            },
        ],
    }

    client = _client(mock)
    result = client.send_turn(_create_chat(client), "改成熱圖", "render-unknown")

    assert result.error == {
        "message": "互動圖表工具結果未能確認；回答需要人工複核",
        "retryable": False,
    }
    assert "code" not in result.error


def test_terminal_render_failure_is_not_cleared_by_later_success() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "圖表狀態曾被標示為終止。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "id": "render-terminal-1",
                "name": "renderAnalysisChart",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "render-terminal-1",
                "status": "completed",
                "output": [
                    {
                        "type": "input_text",
                        "text": '{"code":"chart_state_unknown","details":{"terminal":true}}',
                    }
                ],
            },
            {
                "type": "function_call",
                "id": "render-terminal-2",
                "name": "renderAnalysisChart",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "render-terminal-2",
                "status": "completed",
                "output": [
                    {
                        "type": "input_text",
                        "text": '{"status":"embedded","result_id":"result-1","chart_count":1}',
                    }
                ],
            },
        ],
    }

    client = _client(mock)
    result = client.send_turn(_create_chat(client), "改成熱圖", "render-terminal")

    assert result.error == {
        "message": "互動圖表的發布狀態無法確認；請先查看原對話是否已有圖表，避免重複發布。",
        "retryable": False,
    }
    assert "code" not in result.error


def test_analysis_only_is_not_mislabeled_as_missing_chart() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "答案只需文字。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "id": "analysis-1",
                "name": "runPythonAnalysis",
                "status": "completed",
            }
        ],
    }

    client = _client(mock)
    result = client.send_turn(_create_chat(client), "請算平均值", "analysis-only")

    assert result.error is None


def test_unconfirmed_chart_without_saved_embed_requires_review() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "統計已完成，但圓餅圖無法確認。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "id": "analysis-2",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "analysis-2",
                "status": "completed",
                "output": [
                    {
                        "type": "input_text",
                        "text": '{"rich_ui_status":"embed_unknown"}',
                    }
                ],
            },
        ],
    }

    result = _client(mock).send_turn(
        _create_chat(_client(mock)), "請繪製圓餅圖", "unknown-chart"
    )

    assert result.charts == []
    assert result.error == {
        "message": "互動圖表附加狀態不明且未讀到圖表；需要人工複核",
        "retryable": False,
    }


def test_invalid_chart_without_saved_embed_requires_review() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "統計已完成，圖表也已呈現。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "id": "analysis-invalid-chart",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "analysis-invalid-chart",
                "status": "completed",
                "output": [
                    {
                        "type": "input_text",
                        "text": '{"rich_ui_status":"invalid_spec"}',
                    }
                ],
            },
        ],
    }

    result = _client(mock).send_turn(
        _create_chat(_client(mock)), "請繪製圓餅圖", "invalid-chart"
    )

    assert result.charts == []
    assert result.error == {
        "message": "互動圖表規格無效且未讀到圖表；需要人工複核",
        "retryable": False,
    }


@pytest.mark.parametrize(
    "status",
    [
        "bridge_not_configured",
        "chat_context_missing",
        "identity_mismatch",
        "embed_failed",
    ],
)
def test_known_chart_embed_failure_without_saved_embed_requires_review(
    status: str,
) -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "分析已完成，圖表如上。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "id": "analysis-chart-failure",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "analysis-chart-failure",
                "status": "completed",
                "output": [
                    {
                        "type": "input_text",
                        "text": json.dumps({"rich_ui_status": status}),
                    }
                ],
            },
        ],
    }

    result = _client(mock).send_turn(
        _create_chat(_client(mock)), "請繪製圖表", f"failed-{status}"
    )

    assert result.charts == []
    assert result.error == {
        "message": "互動圖表未成功附加且未讀到圖表；需要人工複核",
        "retryable": False,
    }


def test_earlier_invalid_chart_is_not_failure_after_embed() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "修正後圖表已呈現。",
        "done": True,
        "embeds": ["<div>chart</div>"],
        "output": [
            {
                "type": "function_call",
                "id": "analysis-invalid-first",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "analysis-invalid-first",
                "status": "completed",
                "output": [
                    {
                        "type": "input_text",
                        "text": '{"rich_ui_status":"invalid_spec"}',
                    }
                ],
            },
        ],
    }

    result = _client(mock).send_turn(
        _create_chat(_client(mock)), "請繪製圓餅圖", "repaired-chart"
    )

    assert len(result.charts) == 1
    assert result.error is None


def test_runner_records_failed_analysis_as_failed_question(tmp_path: Path) -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "分析工具失敗，沒有可驗證的統計結果。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "name": "runPythonAnalysis",
                "status": "failed",
            }
        ],
    }
    question_file = tmp_path / "questions.txt"
    question_file.write_text("42: 請分析比分階段。\n", encoding="utf-8")
    runner = EvaluationRunner.start(
        _client(mock),
        question_file=question_file,
        store_path=tmp_path / "run.json",
        model_snapshot={"model": "badmintonai"},
        data_snapshot={"version": "dataset-v1"},
    )

    state = runner.run_pending()

    assert state["questions"][0]["status"] == "failed"
    assert state["questions"][0]["turns"][0]["result"]["error"]["retryable"] is False


def test_successful_retry_is_not_failed_by_earlier_analysis_error() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "重試後算出 31%。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "id": "analysis-failed",
                "call_id": "analysis-failed",
                "name": "runPythonAnalysis",
                "status": "failed",
            },
            {
                "type": "function_call_output",
                "call_id": "analysis-failed",
                "status": "failed",
                "output": [
                    {"type": "input_text", "text": '{"code":"analysis_code_error"}'}
                ],
            },
            {
                "type": "function_call",
                "id": "analysis-success",
                "call_id": "analysis-success",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "analysis-success",
                "status": "completed",
                "output": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {
                                "result_id": "a" * 48,
                                "artifacts": [{"relative_path": "summary.json"}],
                            }
                        ),
                    }
                ],
            },
        ],
    }

    result = _client(mock).send_turn(
        _create_chat(_client(mock)), "請分析", "successful-retry"
    )

    assert result.error is None


def test_completed_clarification_after_analysis_waits_for_answer() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "請先確認門檻。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call",
                "name": "requestClarification",
                "status": "completed",
                "arguments": '{"question":"請先確認門檻？"}',
            },
        ],
    }
    result = _client(mock).send_turn(_create_chat(_client(mock)), "請分析", "conflict")

    assert result.awaiting_clarification is True
    assert result.clarification_signal == "event"
    assert result.error is None


def test_failed_analysis_before_clarification_preserves_error_while_waiting() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "請先確認角落定義。",
        "done": True,
        "output": [
            {"type": "function_call", "name": "runPythonAnalysis", "status": "failed"},
            {
                "type": "function_call",
                "name": "requestClarification",
                "status": "completed",
                "arguments": '{"question":"角落如何定義？","options":["擊球站位","移動終點"]}',
            },
        ],
    }

    result = _client(mock).send_turn(
        _create_chat(_client(mock)), "分析角落轉換", "failed-analysis-clarification"
    )

    assert result.clarification_signal == "event"
    assert result.awaiting_clarification is True
    assert result.error == {
        "message": "Python 分析工具未成功完成；回答需要人工複核",
        "retryable": False,
    }


def test_completed_clarification_after_chart_still_waits_for_answer() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "圖表已產生，先確認比較範圍。",
        "done": True,
        "embeds": ["<div>chart</div>"],
        "output": [
            {
                "type": "function_call",
                "name": "requestClarification",
                "status": "completed",
                "arguments": '{"question":"要比較哪幾場？"}',
            }
        ],
    }

    result = _client(mock).send_turn(
        _create_chat(_client(mock)), "比較表現", "chart-first"
    )

    assert result.charts
    assert result.clarification_signal == "event"
    assert result.awaiting_clarification is True


def test_usage_is_read_from_actual_assistant_info_without_estimation() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "完成。",
        "done": True,
        "output": [],
        "info": {"usage": {"input_tokens": 2}},
    }

    result = _client(mock).send_turn(_create_chat(_client(mock)), "題目", "info-usage")

    assert result.usage == {"input_tokens": 2}
    assert "total_tokens" not in result.usage


def test_done_message_with_unfinished_tool_is_not_marked_completed() -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = {
        "content": "已找到部分資料。",
        "done": True,
        "output": [
            {
                "type": "function_call",
                "name": "listColumnCatalog",
                "status": "in_progress",
            }
        ],
    }

    result = _client(mock).send_turn(
        _create_chat(_client(mock)), "請分析", "unfinished-tool"
    )

    assert result.error == {
        "message": "Open WebUI 回合已結束但仍有未完成的工具呼叫；需檢查原始對話",
        "retryable": False,
    }


def test_unknown_create_timeout_is_recovered_without_duplicate_chat() -> None:
    mock = _MockOpenWebUI()
    mock.timeout_after_create = True
    mock.search_visible_after = 3
    client = _client(mock)

    with pytest.raises(OpenWebUIStateUncertain):
        client.create_conversation(
            "run-timeout", "4", "stable-key", {"model": "badmintonai"}
        )

    recovered = client.recover_conversation("run-timeout", "4", "stable-key")

    assert recovered == "chat-1"
    assert (
        sum(request.full_url.endswith("/api/v1/chats/new") for request in mock.requests)
        == 1
    )


def test_runner_keeps_unknown_completion_pending_and_recovers_without_resend(
    tmp_path: Path,
) -> None:
    mock = _MockOpenWebUI()
    mock.timeout_after_completion = True
    client = _client(mock)
    question_file = tmp_path / "questions.txt"
    question_file.write_text("1: 原始題目？\n", encoding="utf-8")
    store_path = tmp_path / "run.json"
    runner = EvaluationRunner.start(
        client,
        question_file=question_file,
        store_path=store_path,
        model_snapshot={"model": "badmintonai", "version": "model-v1"},
        data_snapshot={"version": "dataset-v1"},
    )

    state = runner.run_pending()
    question = state["questions"][0]

    assert question["status"] == "running"
    assert question["pending_turn"] is not None
    assert len(mock.completion_requests) == 1

    mock.finish_pending_assistants()
    resumed = EvaluationRunner.resume(client, store_path=store_path)
    state = resumed.run_pending()

    assert state["questions"][0]["status"] == "completed"
    assert state["questions"][0]["turns"][0]["attempts"][0]["recovered"] is True
    assert len(mock.completion_requests) == 1
    assert "secret-test-key" not in store_path.read_text(encoding="utf-8")


def test_rate_limited_completion_stays_pending_without_blind_resend(
    tmp_path: Path,
) -> None:
    mock = _MockOpenWebUI()
    mock.completion_status = 429
    client = _client(mock)
    question_file = tmp_path / "questions.txt"
    question_file.write_text("1: 原始題目？\n", encoding="utf-8")
    store_path = tmp_path / "run.json"
    runner = EvaluationRunner.start(
        client,
        question_file=question_file,
        store_path=store_path,
        model_snapshot={"model": "badmintonai"},
        data_snapshot={"version": "dataset-v1"},
    )

    state = runner.run_pending()
    question = state["questions"][0]
    assert question["status"] == "running"
    assert question["pending_turn"] is not None
    assert len(mock.completion_requests) == 1

    state = runner.run_pending()

    assert state["questions"][0]["status"] == "running"
    assert len(mock.completion_requests) == 1
    assert "org-id-987" not in store_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "assistant",
    [
        {"content": "", "done": True, "output": []},
        {
            "content": "工具循環中斷，沒有最終答案。",
            "done": True,
            "output": [
                {
                    "type": "function_call",
                    "id": f"pending-{index}",
                    "name": "listColumnCatalog",
                    "status": "in_progress",
                }
                for index in range(16)
            ],
        },
        {
            "content": "Python 分析重試後仍失敗。",
            "done": True,
            "output": [
                {
                    "type": "function_call",
                    "id": f"python-{index}",
                    "name": "runPythonAnalysis",
                    "status": "failed",
                }
                for index in range(5)
            ],
        },
        {
            "content": "圖表如上。",
            "done": True,
            "output": [
                {
                    "type": "function_call",
                    "id": "analysis-chart",
                    "name": "runPythonAnalysis",
                    "status": "completed",
                },
                {
                    "type": "function_call_output",
                    "call_id": "analysis-chart",
                    "status": "completed",
                    "output": [
                        {
                            "type": "input_text",
                            "text": '{"rich_ui_status":"embed_unknown"}',
                        }
                    ],
                },
            ],
        },
    ],
    ids=["blank", "tool-loop-limit", "python-retries-failed", "chart-not-attached"],
)
def test_terminal_failure_classes_are_not_completed_or_resent(
    tmp_path: Path, assistant: dict[str, Any]
) -> None:
    mock = _MockOpenWebUI()
    mock.next_assistant = assistant
    client = _client(mock)
    question_file = tmp_path / "questions.txt"
    question_file.write_text("1: 原始題目？\n", encoding="utf-8")
    runner = EvaluationRunner.start(
        client,
        question_file=question_file,
        store_path=tmp_path / "run.json",
        model_snapshot={"model": "badmintonai"},
        data_snapshot={"version": "dataset-v1"},
    )

    question = runner.run_pending()["questions"][0]

    assert question["status"] == "failed"
    assert question["pending_turn"] is None
    assert len(question["turns"][0]["attempts"]) == 1
    assert len(mock.completion_requests) == 1


def test_runner_recovers_unknown_chat_creation_without_duplicate_post(
    tmp_path: Path,
) -> None:
    mock = _MockOpenWebUI()
    mock.timeout_after_create = True
    mock.search_visible_after = 3
    client = _client(mock)
    question_file = tmp_path / "questions.txt"
    question_file.write_text("1: 原始題目？\n", encoding="utf-8")
    store_path = tmp_path / "run.json"
    runner = EvaluationRunner.start(
        client,
        question_file=question_file,
        store_path=store_path,
        model_snapshot={"model": "badmintonai", "version": "model-v1"},
        data_snapshot={"version": "dataset-v1"},
    )

    state = runner.run_pending()

    assert state["questions"][0]["status"] == "running"
    assert state["questions"][0]["conversation_id"] is None
    assert state["questions"][0]["creation_attempts"][0]["finished_at"] is None
    assert len(mock.chats) == 1

    state = runner.run_pending()

    assert state["questions"][0]["status"] == "completed"
    assert state["questions"][0]["conversation_id"] == "chat-1"
    assert (
        sum(request.full_url.endswith("/api/v1/chats/new") for request in mock.requests)
        == 1
    )


def test_model_snapshot_mismatch_fails_before_http_mutation() -> None:
    mock = _MockOpenWebUI()
    client = _client(mock)

    with pytest.raises(ValueError, match="model ID 不一致"):
        client.create_conversation("run-1", "1", "key", {"model": "another-model"})

    assert mock.requests == []


def test_client_does_not_read_dotenv_or_environment_for_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BADMINTON_AI_OPEN_WEBUI_API_KEY", "ambient-secret")

    with pytest.raises(ValueError, match="明確提供"):
        OpenWebUIEvaluationClient(
            base_url="http://open-webui:8080",
            api_key="",
            model_id="badmintonai",
        )


def test_persisted_stream_failure_is_terminal_and_keeps_partial_answer() -> None:
    response = {
        "chat": {
            "history": {
                "messages": {
                    "user-1": {"id": "user-1", "role": "user", "content": "題目"},
                    "assistant-1": {
                        "id": "assistant-1",
                        "role": "assistant",
                        "done": True,
                        "content": "已輸出的部分文字",
                        "error": {
                            "content": {
                                "code": "badmintonai_stream_no_progress_timeout",
                                "message": "模型服務串流未持續產生回答或工具參數；本輪已停止。",
                            }
                        },
                        "output": [
                            {
                                "type": "function_call",
                                "id": "call-1",
                                "call_id": "call-1",
                                "name": "runPythonAnalysis",
                                "status": "failed",
                            },
                            {
                                "type": "function_call_output",
                                "id": "output-1",
                                "call_id": "call-1",
                                "status": "failed",
                                "evaluation_event": {
                                    "type": "tool_not_executed",
                                    "reason": "stream_failure",
                                },
                                "output": [
                                    {
                                        "type": "input_text",
                                        "text": "模型服務串流未持續產生回答或工具參數；本輪已停止，未完成部分不視為已驗證。",
                                    }
                                ],
                            },
                        ],
                    },
                }
            }
        }
    }

    result = _turn_result(response, "user-1", "assistant-1", lambda _message: False)

    assert result is not None
    assert result.error == {
        "code": "badmintonai_stream_no_progress_timeout",
        "message": "模型服務串流未持續產生回答或工具參數；回合已停止，未完成部分不視為已驗證。",
        "retryable": False,
    }
    assert result.messages[-1]["content"] == "已輸出的部分文字"
    assert result.tool_calls[0]["status"] == "failed"
