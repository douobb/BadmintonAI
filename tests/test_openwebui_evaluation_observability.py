"""評測專用 Open WebUI 原生串流、錯誤遮蔽與安全階段紀錄測試。"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import logging
import sys
import textwrap
from pathlib import Path

import pytest

from scripts.evaluation_openwebui_client import (
    _analysis_completion_error,
    _decode_tool_result_payload,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PATCH_PATH = PROJECT_ROOT / "openwebui_patch" / "apply_evaluation_observability.py"
OBSERVABILITY_PATH = PROJECT_ROOT / "openwebui_patch" / "evaluation_observability.py"
SAFE_PROVIDER_PATCH_PATH = (
    PROJECT_ROOT / "openwebui_patch" / "apply_safe_provider_errors.py"
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _chat_source() -> str:
    patch = _load_module("evaluation_observability_patch_chat", PATCH_PATH)
    return "\n".join(
        [
            patch.CHAT_IMPORT_ANCHOR,
            patch.CHAT_HANDLER_ANCHOR,
            "\t\tif (event.chat_id === $chatId) {",
            "\t\t\tlet message = history.messages[event.message_id];",
            "",
            "\t\t\tif (message) {",
        ]
    )


def test_chat_patch_hydrates_unknown_message_once_and_keeps_native_handler() -> None:
    patch = _load_module("evaluation_observability_patch_chat_once", PATCH_PATH)
    original = _chat_source()
    updated = patch.patch_chat_source(original)

    assert "createEvaluationMessageSynchronizer" in updated
    assert "getChatById(localStorage.token, activeChatId)" in updated
    assert "history = { ...history, messages };" in updated
    handoff = "if (await evaluationMessageSynchronizer(event, cb)) return;"
    assert updated.count(handoff) == 1, (
        "未知訊息交由同步器重播後，原 handler 必須立即結束"
    )
    assert updated.index(handoff) < updated.index(
        "message = history.messages[event.message_id];", updated.index(handoff)
    )
    assert (
        "dispatchEvent: (replayedEvent, callback) => chatEventHandler(replayedEvent, callback)"
        in updated
    )
    assert updated.count("const chatEventHandler = async (event, cb) => {") == 1
    assert patch.patch_chat_source(updated) == updated


def _openai_source() -> str:
    return "\n".join(
        [
            "import re",
            "from typing import Optional",
            "from pydantic import BaseModel, ConfigDict",
            "from sqlalchemy.ext.asyncio import AsyncSession",
            "@router.post('/chat/completions')",
            "async def generate_chat_completion(request, form_data, user):",
            "    payload = {**form_data}",
            "    metadata = payload.pop('metadata', None)",
            "    is_streaming_request = bool(payload.get('stream', False))",
            "    r = None",
            "    streaming = False",
            "    try:",
            "        session = await get_session()",
            "",
            "        r = await session.request(",
            "            method='POST',",
            "            url=request_url,",
            "            data=payload,",
            "            headers=headers,",
            "            cookies=cookies,",
            "            ssl=AIOHTTP_CLIENT_SESSION_SSL,",
            "            timeout=get_client_timeout(stream=is_streaming_request),",
            "        )",
            "",
            "        # Check if response is SSE",
            "        if 'text/event-stream' in r.headers.get('Content-Type', ''):",
            "            streaming = True",
            "            return StreamingResponse(",
            "                stream_wrapper(r),",
            "                status_code=r.status,",
            "            )",
            "    except Exception as e:",
            "        log.exception(e)",
            "",
            "        raise HTTPException(",
            "            status_code=r.status if r else 500,",
            "            detail=ERROR_MESSAGES.SERVER_CONNECTION_ERROR,",
            "        )",
            "    finally:",
            "        if not streaming:",
            "            await cleanup_response(r)",
            "",
            "async def embeddings(request, form_data, user):",
            "    return None",
        ]
    )


def test_openai_patch_keeps_native_sse_and_sanitizes_eval_upstream_failures() -> None:
    patch = _load_module("evaluation_observability_patch_openai", PATCH_PATH)
    source = _openai_source()
    updated = patch.patch_openai_source(source)
    ast.parse(updated)

    assert (
        "logged_evaluation_stream(r, metadata, upstream_started_at, stream_wrapper, log)"
        in updated
    )
    assert "'upstream_start'" in updated
    assert "'upstream_headers'" in updated
    assert "'upstream_error'" in updated
    assert "safe_http_error_message(r.status)" in updated
    assert "detail=safe_evaluation_error(e)" in updated
    assert "unexpected_content_type" in updated
    assert (
        "badmintonai_stream_guard = bool(is_streaming_request and badmintonai_model_request)"
        in updated
    )
    assert (
        "timeout=apply_badmintonai_stream_timeout(get_client_timeout(stream=True), badmintonai_timeouts) if badmintonai_stream_guard"
        in updated
    )
    assert (
        "guarded_badmintonai_stream(r, metadata, badmintonai_stream_started_at"
        in updated
    )
    assert "else stream_wrapper(r)" in updated
    assert (
        "if badmintonai_stream_guard and isinstance(e, HTTPException) and not evaluation_request:"
        in updated
    )
    assert "not isinstance(e, HTTPException)" in updated
    assert "form_data.get('model'), metadata" in updated
    assert "error_body" not in updated
    assert patch.patch_openai_source(updated) == updated


def test_patched_openai_route_guards_only_badmintonai_sse() -> None:
    patch = _load_module("evaluation_observability_openai_behavior_patch", PATCH_PATH)
    observability = _load_module(
        "evaluation_observability_openai_behavior_helper", OBSERVABILITY_PATH
    )
    updated = patch.patch_openai_source(_openai_source())
    tree = ast.parse(updated)
    route = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "generate_chat_completion"
    )
    route.decorator_list = []

    class FakeTimeout:
        def __init__(
            self,
            *,
            total,
            connect=None,
            sock_connect=None,
            sock_read=None,
            ceil_threshold=5,
        ):
            self.total = total
            self.connect = connect
            self.sock_connect = sock_connect
            self.sock_read = sock_read
            self.ceil_threshold = ceil_threshold

    class FakeResponse:
        def __init__(self, content_type: str, chunks: list[bytes] | None = None):
            self.status = 200
            self.headers = {"Content-Type": content_type}
            self.chunks = chunks or []

    class FakeSession:
        def __init__(self, response):
            self.response = response
            self.request_options = None

        async def request(self, **options):
            self.request_options = options
            if isinstance(self.response, BaseException):
                raise self.response
            return self.response

    class FakeHTTPException(Exception):
        def __init__(self, status_code: int):
            super().__init__(f"status={status_code}")
            self.status_code = status_code

    class FakeStreamingResponse:
        def __init__(self, body, *, status_code, media_type=None):
            self.body = body
            self.status_code = status_code
            self.media_type = media_type

    async def invoke(model_id: str, response: FakeResponse):
        session = FakeSession(response)

        async def get_session():
            return session

        async def native_stream_wrapper(current_response):
            for chunk in current_response.chunks:
                yield chunk

        namespace = {
            "time": __import__("time"),
            "asyncio": asyncio,
            "HTTPException": FakeHTTPException,
            "StreamingResponse": FakeStreamingResponse,
            "JSONResponse": lambda **kwargs: kwargs,
            "log": logging.getLogger("test.openai-route-stream-scope"),
            "get_session": get_session,
            "get_client_timeout": lambda stream=False: FakeTimeout(
                total=1200 if stream else 300,
                connect=10,
                sock_connect=10,
                sock_read=120 if stream else None,
            ),
            "AIOHTTP_CLIENT_SESSION_SSL": None,
            "request_url": "https://upstream.invalid/v1/chat/completions",
            "headers": {},
            "cookies": None,
            "cleanup_response": lambda _response: asyncio.sleep(0),
            "stream_wrapper": native_stream_wrapper,
            "is_evaluation_metadata": observability.is_evaluation_metadata,
            "is_badmintonai_stream_request": observability.is_badmintonai_stream_request,
            "badmintonai_stream_timeouts": observability.badmintonai_stream_timeouts,
            "apply_badmintonai_stream_timeout": observability.apply_badmintonai_stream_timeout,
            "guarded_badmintonai_stream": observability.guarded_badmintonai_stream,
            "badmintonai_stream_failure_body": observability.badmintonai_stream_failure_body,
            "mark_badmintonai_stream_failure": observability.mark_badmintonai_stream_failure,
            "classify_badmintonai_request_failure": observability.classify_badmintonai_request_failure,
            "log_badmintonai_stream_failure": observability.log_badmintonai_stream_failure,
            "logged_evaluation_stream": observability.logged_evaluation_stream,
            "log_evaluation_stage": observability.log_evaluation_stage,
            "safe_evaluation_error": observability.safe_evaluation_error,
            "safe_http_error_message": observability.safe_http_error_message,
            "ERROR_MESSAGES": type(
                "Errors", (), {"SERVER_CONNECTION_ERROR": "connection failed"}
            ),
        }
        module = ast.Module(body=[route], type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, "<patched-openai-route>", "exec"), namespace)
        metadata = {"chat_id": "chat-1", "message_id": "message-1"}
        form_data = {"model": model_id, "stream": True, "metadata": metadata}
        try:
            returned = await namespace["generate_chat_completion"](
                None, form_data, None
            )
        except FakeHTTPException as exc:
            return exc.status_code, metadata, session.request_options
        return returned, metadata, session.request_options

    data_event = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
    done_event = b"data: [DONE]\n\n"

    async def run():
        bad_response, bad_metadata, bad_request = await invoke(
            "badmintonai", FakeResponse("application/json")
        )
        other_response, other_metadata, other_request = await invoke(
            "other-model", FakeResponse("text/event-stream", [data_event, done_event])
        )
        rate_limit_response, rate_limit_metadata, rate_limit_request = await invoke(
            "badmintonai", FakeHTTPException(429)
        )
        return (
            bad_response,
            bad_metadata,
            bad_request,
            other_response,
            other_metadata,
            other_request,
            rate_limit_response,
            rate_limit_metadata,
            rate_limit_request,
        )

    (
        bad_response,
        bad_metadata,
        bad_request,
        other_response,
        other_metadata,
        other_request,
        rate_limit_response,
        rate_limit_metadata,
        rate_limit_request,
    ) = asyncio.run(run())
    assert isinstance(bad_response, FakeStreamingResponse)
    assert bad_response.media_type == "text/event-stream"
    bad_chunks = asyncio.run(_collect_chunks(bad_response.body))
    assert json.loads(bad_chunks[0].decode().split(": ", 1)[1])["error"]["code"] == (
        "badmintonai_stream_upstream_error"
    )
    assert (
        observability.badmintonai_stream_failure_code(bad_metadata)
        == "stream_upstream_error"
    )
    assert bad_request["timeout"].total == 180
    assert bad_request["timeout"].sock_read == 60

    assert isinstance(other_response, FakeStreamingResponse)
    assert asyncio.run(_collect_chunks(other_response.body)) == [
        data_event,
        done_event,
    ]
    assert other_metadata.get("_badmintonai_stream_failure") is None
    assert other_request["timeout"].total == 1200
    assert other_request["timeout"].sock_read == 120
    assert rate_limit_response == 429, "既有 HTTP 429 狀態與處理策略維持原樣"
    assert observability.badmintonai_stream_failure_code(rate_limit_metadata) is None
    assert rate_limit_request["timeout"].total == 180


async def _collect_chunks(stream):
    return [chunk async for chunk in stream]


def test_badmintonai_stream_scope_timeouts_and_client_timeout_preservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observability = _load_module(
        "evaluation_observability_stream_scope", OBSERVABILITY_PATH
    )
    monkeypatch.delenv("BADMINTON_AI_STREAM_MODEL_IDS", raising=False)
    monkeypatch.delenv("BADMINTON_AI_STREAM_TOTAL_SECONDS", raising=False)
    monkeypatch.delenv("BADMINTON_AI_STREAM_NO_DATA_SECONDS", raising=False)
    monkeypatch.delenv("BADMINTON_AI_STREAM_NO_PROGRESS_SECONDS", raising=False)
    assert observability.is_badmintonai_stream_request("badmintonai", None)
    assert not observability.is_badmintonai_stream_request("other-model", None)
    assert observability.is_badmintonai_stream_request(
        "other-model",
        {
            "badmintonai_evaluation": True,
            "badmintonai_operation_id": "op-1",
        },
    )
    monkeypatch.setenv("BADMINTON_AI_STREAM_MODEL_IDS", "badmintonai, badmintonai-lab")
    assert observability.is_badmintonai_stream_request("badmintonai-lab", None)
    assert not observability.is_badmintonai_stream_request("unlisted-model", None)

    timeouts = observability.badmintonai_stream_timeouts()
    assert timeouts.total_seconds == 180
    assert timeouts.no_data_seconds == 60
    assert timeouts.no_progress_seconds == 90
    monkeypatch.setenv("BADMINTON_AI_STREAM_TOTAL_SECONDS", "200")
    monkeypatch.setenv("BADMINTON_AI_STREAM_NO_DATA_SECONDS", "70")
    monkeypatch.setenv("BADMINTON_AI_STREAM_NO_PROGRESS_SECONDS", "100")
    configured = observability.badmintonai_stream_timeouts()
    assert (
        configured.total_seconds,
        configured.no_data_seconds,
        configured.no_progress_seconds,
    ) == (
        200,
        70,
        100,
    )
    monkeypatch.setenv("BADMINTON_AI_STREAM_TOTAL_SECONDS", "nan")
    monkeypatch.setenv("BADMINTON_AI_STREAM_NO_DATA_SECONDS", "0")
    monkeypatch.setenv("BADMINTON_AI_STREAM_NO_PROGRESS_SECONDS", "10000")
    safe_defaults = observability.badmintonai_stream_timeouts()
    assert (
        safe_defaults.total_seconds,
        safe_defaults.no_data_seconds,
        safe_defaults.no_progress_seconds,
    ) == (
        180,
        60,
        90,
    )

    class FakeTimeout:
        def __init__(
            self,
            *,
            total,
            connect=None,
            sock_connect=None,
            sock_read=None,
            ceil_threshold=5,
        ):
            self.total = total
            self.connect = connect
            self.sock_connect = sock_connect
            self.sock_read = sock_read
            self.ceil_threshold = ceil_threshold

    native_timeout = FakeTimeout(
        total=600,
        connect=5,
        sock_connect=4,
        sock_read=30,
        ceil_threshold=7,
    )
    patched_timeout = observability.apply_badmintonai_stream_timeout(
        native_timeout, configured
    )
    assert (patched_timeout.total, patched_timeout.sock_read) == (200, 70)
    assert (patched_timeout.connect, patched_timeout.sock_connect) == (5, 4)
    assert patched_timeout.ceil_threshold == 7


def test_guarded_badmintonai_stream_preserves_chunked_sse_and_utf8(caplog) -> None:
    observability = _load_module(
        "evaluation_observability_stream_transparent", OBSERVABILITY_PATH
    )
    now = [10.0]
    metadata = {"chat_id": "chat-1", "message_id": "message-1"}
    timeouts = observability.BadmintonAIStreamTimeouts(180, 60, 90)

    class TimedStream:
        def __init__(self, plan):
            self.plan = list(plan)
            self.allowed = 0.0
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.plan:
                raise StopAsyncIteration
            delay, result = self.plan.pop(0)
            if delay > self.allowed:
                now[0] += self.allowed
                raise asyncio.TimeoutError
            now[0] += delay
            if isinstance(result, BaseException):
                raise result
            return result

        async def aclose(self):
            self.closed = True

    async def collect(plan):
        source = TimedStream(plan)

        async def fake_wait(awaitable, *, timeout):
            source.allowed = timeout
            return await awaitable

        stream = observability.guarded_badmintonai_stream(
            None,
            metadata,
            10.0,
            lambda _response: source,
            logging.getLogger("test.badmintonai-stream"),
            timeouts,
            clock=lambda: now[0],
            wait_for=fake_wait,
        )
        chunks = [chunk async for chunk in stream]
        return chunks, source

    text_event = 'data: {"choices":[{"delta":{"content":"羽"}}]}\n\n'.encode()
    split_at = text_event.index("羽".encode()) + 1
    tool_event = b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"{\\"x\\":1}"}}]}}]}\n\n'
    usage_event = b'data: {"choices":[],"usage":{"total_tokens":4}}\n\n'
    done_event = b"data: [DONE]\n\n"
    chunks, source = asyncio.run(
        collect(
            [
                (1, text_event[:split_at]),
                (0, text_event[split_at:]),
                (1, tool_event),
                (1, usage_event),
                (0, done_event),
            ]
        )
    )
    assert chunks == [
        text_event[:split_at],
        text_event[split_at:],
        tool_event,
        usage_event,
        done_event,
    ]
    assert source.closed
    assert observability.badmintonai_stream_failure_code(metadata) is None

    large_event = (
        "data: "
        + json.dumps({"choices": [{"delta": {"content": "x" * 70_000}}]})
        + "\n\n"
    ).encode("utf-8")
    large_chunks, _ = asyncio.run(collect([(1, large_event), (0, done_event)]))
    assert large_chunks == [large_event, done_event]
    assert observability.badmintonai_stream_failure_code(metadata) is None


@pytest.mark.parametrize(
    ("plan_factory", "expected_code"),
    [
        (lambda: [(20, b": heartbeat\n\n"), (10_000, b"")], "stream_no_data_timeout"),
        (
            lambda: [
                (40, b": heartbeat\n\n"),
                (40, b'data: {"id":"metadata-only"}\n\n'),
                (10, b'data: {"choices":[{"delta":{"content":"   "}}]}\n\n'),
                (10_000, b""),
            ],
            "stream_no_progress_timeout",
        ),
        (
            lambda: [
                (40, b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'),
                (40, b'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'),
                (40, b'data: {"choices":[{"delta":{"content":"c"}}]}\n\n'),
                (40, b'data: {"choices":[{"delta":{"content":"d"}}]}\n\n'),
                (10_000, b""),
            ],
            "stream_total_timeout",
        ),
    ],
)
def test_guarded_badmintonai_stream_times_out_without_wall_clock_wait(
    caplog, plan_factory, expected_code
) -> None:
    observability = _load_module(
        f"evaluation_observability_stream_{expected_code}", OBSERVABILITY_PATH
    )
    now = [0.0]
    metadata = {
        "chat_id": "chat-1",
        "message_id": "message-1",
        "badmintonai_evaluation": True,
        "badmintonai_operation_id": "operation-1",
    }
    timeouts = observability.BadmintonAIStreamTimeouts(180, 60, 90)

    class TimedStream:
        def __init__(self, plan):
            self.plan = list(plan)
            self.allowed = 0.0
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.plan:
                raise StopAsyncIteration
            delay, chunk = self.plan.pop(0)
            if delay > self.allowed:
                now[0] += self.allowed
                raise asyncio.TimeoutError
            now[0] += delay
            return chunk

        async def aclose(self):
            self.closed = True

    source = TimedStream(plan_factory())

    async def fake_wait(awaitable, *, timeout):
        source.allowed = timeout
        return await awaitable

    async def collect():
        return [
            chunk
            async for chunk in observability.guarded_badmintonai_stream(
                None,
                metadata,
                0.0,
                lambda _response: source,
                logging.getLogger("test.badmintonai-stream-timeout"),
                timeouts,
                clock=lambda: now[0],
                wait_for=fake_wait,
            )
        ]

    caplog.set_level(logging.INFO)
    chunks = asyncio.run(collect())
    assert source.closed
    assert observability.badmintonai_stream_failure_code(metadata) == expected_code
    error_payload = json.loads(chunks[-2].decode().split(": ", 1)[1])
    assert error_payload["error"]["code"] == f"badmintonai_{expected_code}"
    assert chunks[-1] == b"data: [DONE]\n\n"
    assert "api_key" not in caplog.text
    assert "prompt secret" not in caplog.text
    assert expected_code in caplog.text


def test_guarded_badmintonai_stream_cancels_cleanly_and_sanitizes_upstream_exception(
    caplog,
) -> None:
    observability = _load_module(
        "evaluation_observability_stream_cancel_error", OBSERVABILITY_PATH
    )
    now = [1.0]
    timeouts = observability.BadmintonAIStreamTimeouts(180, 60, 90)

    class Source:
        def __init__(self, raises=False):
            self.raises = raises
            self.allowed = 0.0
            self.closed = False
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.raises:
                self.raises = False
                raise ValueError("provider https://secret.invalid/?api_key=hidden")
            if not self.sent:
                self.sent = True
                return b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            now[0] += self.allowed
            raise asyncio.TimeoutError

        async def aclose(self):
            self.closed = True

    async def cancel_case():
        source = Source()

        async def fake_wait(awaitable, *, timeout):
            source.allowed = timeout
            return await awaitable

        stream = observability.guarded_badmintonai_stream(
            None,
            {},
            1.0,
            lambda _response: source,
            logging.getLogger("test.badmintonai-stream-cancel"),
            timeouts,
            clock=lambda: now[0],
            wait_for=fake_wait,
        )
        first = await stream.__anext__()
        await stream.aclose()
        return first, source

    first, cancelled_source = asyncio.run(cancel_case())
    assert b"partial" in first
    assert cancelled_source.closed

    error_metadata = {"chat_id": "chat-2", "message_id": "message-2"}
    error_source = Source(raises=True)

    async def error_wait(awaitable, *, timeout):
        return await awaitable

    async def error_case():
        return [
            chunk
            async for chunk in observability.guarded_badmintonai_stream(
                None,
                error_metadata,
                now[0],
                lambda _response: error_source,
                logging.getLogger("test.badmintonai-stream-upstream-error"),
                timeouts,
                clock=lambda: now[0],
                wait_for=error_wait,
            )
        ]

    caplog.set_level(logging.WARNING)
    chunks = asyncio.run(error_case())
    assert error_source.closed
    assert (
        observability.badmintonai_stream_failure_code(error_metadata)
        == "stream_upstream_error"
    )
    assert (
        json.loads(chunks[-2].decode().split(": ", 1)[1])["error"]["code"]
        == "badmintonai_stream_upstream_error"
    )
    assert "secret.invalid" not in caplog.text
    assert "api_key=hidden" not in caplog.text

    provider_error_metadata = {"chat_id": "chat-3", "message_id": "message-3"}
    provider_error_text = "provider raw https://secret.invalid/?api_key=hidden"

    class ErrorEventSource:
        def __init__(self):
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.closed:
                raise StopAsyncIteration
            self.closed = True
            return (
                "data: "
                + json.dumps({"error": {"message": provider_error_text}})
                + "\n\n"
            ).encode()

        async def aclose(self):
            self.closed = True

    provider_event_source = ErrorEventSource()

    async def provider_error_wait(awaitable, *, timeout):
        return await awaitable

    async def provider_error_case():
        return [
            chunk
            async for chunk in observability.guarded_badmintonai_stream(
                None,
                provider_error_metadata,
                now[0],
                lambda _response: provider_event_source,
                logging.getLogger("test.badmintonai-stream-provider-error"),
                timeouts,
                clock=lambda: now[0],
                wait_for=provider_error_wait,
            )
        ]

    provider_chunks = asyncio.run(provider_error_case())
    assert provider_event_source.closed
    assert provider_error_text.encode() not in provider_chunks
    assert "secret.invalid" not in caplog.text
    assert "api_key=hidden" not in caplog.text


def _main_source() -> str:
    lines = [
        "from open_webui.utils.misc import get_response_error_detail, merge_model_params",
        "def outer():",
        "    def build_metadata(form_data):",
        "        return {",
        "            'assistant_message_id': form_data.pop('assistant_message_id', None),",
        "        }",
        "    async def process_chat(request, form_data, user, metadata, model, tasks=None):",
        "        try:",
        "            form_data, metadata, events = await process_chat_payload(request, form_data, user, metadata, model)",
        "            if isinstance(response, JSONResponse) and response.status_code >= 400:",
        "                raise Exception(get_response_error_detail(response))",
        "            return await process_chat_response(response, ctx)",
        "        except asyncio.CancelledError:",
        "            raise",
        "        except Exception as e:",
        "            error_detail = e.detail if isinstance(e, HTTPException) else str(e)",
        "            log.error('Error processing chat payload: %s', error_detail)",
        "            if metadata.get('chat_id') and metadata.get('message_id'):",
        "                try:",
        "                    if is_saved_chat_id(metadata.get('chat_id')):",
        "                        await Chats.upsert_message_to_chat_by_id_and_message_id(",
        "                            metadata['chat_id'],",
        "                            metadata['message_id'],",
        "                            {",
        "                                'error': {'content': error_detail},",
        "                            },",
        "                        )",
        "",
        "                    event_emitter = await get_event_emitter(metadata)",
        "                    if event_emitter:",
        "                        await event_emitter(",
        "                            {",
        "                                'type': 'chat:message:error',",
        "                                'data': {'error': {'content': error_detail}},",
        "                            }",
        "                        )",
        "                        await event_emitter(",
        "                            {'type': 'chat:tasks:cancel'},",
        "                        )",
        "                except Exception:",
        "                    pass",
        "        finally:",
        "            pass",
        "    # Fan out: one task per model",
        "    return None",
    ]
    return "\n".join(lines)


def test_main_patch_persists_evaluation_error_as_terminal_and_sanitized() -> None:
    patch = _load_module("evaluation_observability_patch_main", PATCH_PATH)
    updated = patch.patch_main_source(_main_source())
    ast.parse(updated)

    assert "'preprocessing_start'" in updated
    assert "'preprocessing_done'" in updated
    assert "'chat_persistence_complete'" in updated
    assert "safe_evaluation_error(e)" in updated
    assert "status_code=response.status_code" in updated
    assert "**({'done': True} if evaluation_request else {})" in updated
    assert "'type': 'chat:completion'" in updated
    assert "'done': True," in updated
    assert "'error': {'content': error_detail}" in updated
    assert (
        "'badmintonai_evaluation': form_data.pop('badmintonai_evaluation', False) is True"
        in updated
    )
    assert (
        "'badmintonai_operation_id': form_data.pop('badmintonai_operation_id', None)"
        in updated
    )
    assert "evaluation_failure_visible_content(" in updated
    assert "await Chats.get_message_by_id_and_message_id(" in updated
    assert "completion_data['output']" in updated
    assert patch.patch_main_source(updated) == updated


def test_initial_evaluation_http_failure_persists_and_emits_safe_visible_text() -> None:
    patch = _load_module("evaluation_observability_patch_main_behavior", PATCH_PATH)
    observability = _load_module(
        "evaluation_observability_behavior_helper", OBSERVABILITY_PATH
    )
    updated = patch.patch_main_source(_main_source())
    tree = ast.parse(updated)
    outer = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "outer"
    )
    outer.body[-1] = ast.Return(value=ast.Name(id="process_chat", ctx=ast.Load()))
    behavior_module = ast.Module(body=[outer], type_ignores=[])
    ast.fix_missing_locations(behavior_module)

    class FakeHTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class FakeResponse:
        status_code = 400

    async def run_case(
        existing_content: str | None,
        *,
        read_fails: bool = False,
        existing_output: bool = False,
    ):
        target_id = "assistant-message-1"
        # 請求 body 只有 user message；既有 assistant 內容僅由持久化訊息讀取。
        form_data = {
            "messages": [{"id": "user-message-1", "role": "user", "content": "原題"}]
        }
        existing_message = {
            "id": target_id,
            "role": "assistant",
            "content": "" if existing_output else existing_content or "",
        }
        if existing_output and existing_content:
            existing_message["output"] = [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": existing_content}],
                }
            ]
        metadata = {
            "badmintonai_evaluation": True,
            "badmintonai_operation_id": "operation-1",
            "chat_id": "chat-1",
            "message_id": target_id,
            "assistant_message_id": target_id,
        }
        persisted = []
        emitted = []

        async def process_chat_payload(request, payload, user, current_metadata, model):
            return payload, current_metadata, []

        async def process_chat_response(response, ctx):
            raise AssertionError("上游 400 不應進入成功處理")

        async def upsert(chat_id, message_id, update):
            persisted.append((chat_id, message_id, update))

        async def get_message_by_id_and_message_id(chat_id, message_id):
            assert chat_id == "chat-1"
            assert message_id == target_id
            if read_fails:
                raise RuntimeError("不要將讀取錯誤文字送入回應")
            return existing_message

        async def get_event_emitter(current_metadata):
            async def emit(event):
                emitted.append(event)

            return emit

        class FakeChats:
            upsert_message_to_chat_by_id_and_message_id = staticmethod(upsert)

        FakeChats.get_message_by_id_and_message_id = staticmethod(
            get_message_by_id_and_message_id
        )

        namespace = {
            "asyncio": asyncio,
            "time": __import__("time"),
            "HTTPException": FakeHTTPException,
            "Response": FakeResponse,
            "response": FakeResponse(),
            "process_chat_payload": process_chat_payload,
            "process_chat_response": process_chat_response,
            "get_response_error_detail": lambda response: (
                "https://api.openai.com/v1?api_key=raw-provider-secret"
            ),
            "merge_model_params": lambda *args, **kwargs: None,
            "is_evaluation_metadata": observability.is_evaluation_metadata,
            "evaluation_failure_visible_content": observability.evaluation_failure_visible_content,
            "log_evaluation_stage": observability.log_evaluation_stage,
            "safe_evaluation_error": observability.safe_evaluation_error,
            "log": logging.getLogger("test.initial-evaluation-provider-error"),
            "is_saved_chat_id": lambda chat_id: True,
            "Chats": FakeChats,
            "get_event_emitter": get_event_emitter,
        }
        exec(
            compile(behavior_module, "<patched-main-provider-failure>", "exec"),
            namespace,
        )
        process_chat = namespace["outer"]()
        await process_chat(None, form_data, None, metadata, None)
        return persisted[0][2], emitted

    empty_update, empty_events = asyncio.run(run_case(None))
    expected = "模型服務拒絕本次請求；本輪已記錄失敗，不會自動重送。"
    assert empty_update["done"] is True
    assert empty_update["error"]["content"] == expected
    assert empty_update["content"] == expected
    completion = next(
        event for event in empty_events if event.get("type") == "chat:completion"
    )
    assert completion["data"]["done"] is True
    assert completion["data"]["error"]["content"] == expected
    assert completion["data"]["output"][0]["id"] == "assistant-message-1"
    assert completion["data"]["output"][0]["content"] == [
        {"type": "output_text", "text": expected}
    ]
    assert "api.openai.com" not in repr((empty_update, empty_events))
    assert "raw-provider-secret" not in repr((empty_update, empty_events))

    partial_update, partial_events = asyncio.run(run_case("部分內容先前已可見"))
    assert partial_update["done"] is True
    assert partial_update["error"]["content"] == expected
    assert "content" not in partial_update
    partial_completion = next(
        event for event in partial_events if event.get("type") == "chat:completion"
    )
    assert partial_completion["data"]["done"] is True
    assert "output" not in partial_completion["data"]
    assert "api.openai.com" not in repr((partial_update, partial_events))
    assert "raw-provider-secret" not in repr((partial_update, partial_events))

    output_update, output_events = asyncio.run(
        run_case("先前保存的 output 片段", existing_output=True)
    )
    assert "content" not in output_update
    output_completion = next(
        event for event in output_events if event.get("type") == "chat:completion"
    )
    assert "output" not in output_completion["data"]

    unreadable_update, unreadable_events = asyncio.run(run_case(None, read_fails=True))
    assert unreadable_update["done"] is True
    assert unreadable_update["error"]["content"] == expected
    assert "content" not in unreadable_update
    unreadable_completion = next(
        event for event in unreadable_events if event.get("type") == "chat:completion"
    )
    assert "output" not in unreadable_completion["data"]


def test_safe_provider_patch_then_evaluation_patch_composes_and_compiles(
    tmp_path: Path,
) -> None:
    safe_patch = _load_module(
        "safe_provider_patch_composition", SAFE_PROVIDER_PATCH_PATH
    )
    evaluation_patch = _load_module(
        "evaluation_observability_patch_composition", PATCH_PATH
    )
    backend = tmp_path
    utils = backend / "utils"
    utils.mkdir()
    main = backend / "main.py"
    misc = utils / "misc.py"
    main.write_text(_main_source(), encoding="utf-8")
    misc.write_text(safe_patch.ORIGINAL_DETAIL, encoding="utf-8")

    # 重現 Dockerfile：先完整套用供應商錯誤遮蔽，再套評測觀測 patch。
    safe_patch.apply_patch(backend)
    safe_main = main.read_text(encoding="utf-8")
    updated_main = evaluation_patch.patch_main_source(safe_main)
    updated_misc = misc.read_text(encoding="utf-8")
    compile(updated_main, "main.py", "exec")
    compile(updated_misc, "misc.py", "exec")

    assert (
        "if isinstance(response, Response) and response.status_code >= 400:"
        in updated_main
    )
    assert "status_code=response.status_code" in updated_main
    assert "detail=get_response_error_detail(response)" in updated_main
    assert safe_patch.SAFE_DETAIL in updated_misc
    assert evaluation_patch.patch_main_source(updated_main) == updated_main


def _middleware_source() -> str:
    return "\n".join(
        [
            "from open_webui.utils.ask_user import stage_ask_user_tool_calls",
            "async def process_chat_response():",
            "            response_stream_task_id = metadata.get('task_id') or metadata.get('message_id')",
            "            async def emit_message_error(error_content):",
            "                if save_to_chat:",
            "                    saved_errors.append(error_content)",
            "                await event_emitter({'type': 'chat:message:error', 'data': {'error': {'content': error_content}}})",
            "                await event_emitter({'type': 'chat:tasks:cancel'})",
            "            try:",
            "                while tool_calls and (",
            "                    max_tool_call_iterations is None or tool_call_iterations < max_tool_call_iterations",
            "                    ):",
            "                    tool_call_iterations += 1",
            "",
            "                    response_tool_calls = tool_calls.pop(0)",
            "                    async def execute_tool_call(tool_call):",
            "                        name = tool_call.get('function', {}).get('name', '')",
            "                        params = parse_tool_params(tool_call)",
            "                        result = await function(**params)",
            "                        return params, result, tool, tool_type, direct_tool",
            "                    for tool_call in response_tool_calls:",
            "                        tool_call_id = tool_call.get('id', '')",
            "                        tool_function_name = tool_call.get('function', {}).get('name', '')",
            "                        tool_function_params, tool_result, tool, tool_type, direct_tool = tool_results[id(tool_call)]",
            "                        tool_result, tool_result_files, tool_result_embeds = await process_tool_result(",
            "                            request,",
            "                            tool_function_name,",
            "                            tool_result,",
            "                            tool_type,",
            "                            direct_tool,",
            "                            metadata,",
            "                            user,",
            "                        )",
            "                        await terminal_event_handler(",
            "                            tool_function_name, tool_function_params, tool_result, event_emitter",
            "                        )",
            "                    frontend_output = []",
            "                    for item in full_output():",
            "                        frontend_output.append(item)",
            "                    await event_emitter(",
            "                        {",
            "                            'type': 'chat:completion',",
            "                            'data': {",
            "                                'output': frontend_output,",
            "                            },",
            "                        }",
            "                    )",
            "",
            "                    try:",
            "                        new_form_data = {",
            "                        }",
            "                        new_form_data = normalize_messages_for_model(new_form_data)",
            "",
            "                        res = await generate_chat_completion(",
            "                            request, new_form_data, user)",
            "                        if isinstance(res, StreamingResponse):",
            "                            prior_output = list(full_output())",
            "                            output = []",
            "                            await stream_body_handler(res, new_form_data)",
            "                            output[:0] = prior_output",
            "                            prior_output = []",
            "                        elif getattr(res, 'status_code', 200) >= 400:",
            "                            await emit_message_error(get_message_error_content(get_response_error_detail(res)))",
            "                            break",
            "                        else:",
            "                            break",
            "                    except Exception as e:",
            "                        await emit_message_error(get_message_error_content(e))",
            "                    except Exception:",
            "                        pass",
            "                if (",
            "                    max_tool_call_iterations is not None",
            "                    and tool_calls",
            "                    and tool_call_iterations >= max_tool_call_iterations",
            "                ):",
            "                    log.warning('Tool-call iteration limit reached (%s)', max_tool_call_iterations)",
            "                    error_content = f'Tool-call limit reached ({max_tool_call_iterations} iterations).'",
            "                    await emit_message_error(error_content)",
            "                if DETECT_CODE_INTERPRETER:",
            "                    MAX_RETRIES = 5",
            "                    await fake_llm_continuation()",
            "                # Mark all in-progress items as completed",
            "                for item in output:",
            "                    if item.get('status') == 'in_progress':",
            "                        item['status'] = 'completed'",
            "                current_output = full_output()",
            "                data = {'done': True, 'output': current_output}",
            "                await event_emitter({'type': 'chat:completion', 'data': data})",
            "            except Exception:",
            "                pass",
        ]
    )


def test_middleware_patch_short_circuits_only_successful_clarification() -> None:
    patch = _load_module("evaluation_observability_patch_middleware", PATCH_PATH)
    updated = patch.patch_middleware_source(_middleware_source())
    ast.parse(updated)

    assert "EvaluationToolBatchGate" in updated
    execute_function = updated.index("async def execute_tool_call(tool_call):")
    gate_check = updated.index(
        "tool_batch_gate.should_execute(tool_call)", execute_function
    )
    invocation = updated.index("result = await function(**params)", execute_function)
    observe = updated.index(
        "tool_batch_gate.observe_result(name, result)", execute_function
    )
    execute_return = updated.index(
        "return params, result, tool, tool_type, direct_tool", execute_function
    )
    assert gate_check < invocation < observe < execute_return
    assert "tool_batch_gate.was_unexecuted(tool_call)" in updated
    assert "finish_terminal_tool_turn(" in updated
    assert "if terminal_tool_message is not None:" in updated
    assert "if clarification_finished or terminal_finished:" in updated
    assert "break" in updated
    assert "terminalize_pending_tool_calls(" in updated
    assert "tool_iteration_limit_reached = False" in updated
    assert "finish_tool_iteration_limit_turn(" in updated
    assert (
        "if DETECT_CODE_INTERPRETER and not tool_iteration_limit_reached and not tool_turn_failed and not no_progress_final_sent:"
        in updated
    )
    assert (
        "item['status'] = 'failed' if tool_iteration_limit_reached or tool_turn_failed else 'completed'"
        in updated
    )
    error_handler = updated.index("async def emit_message_error(error_content):")
    error_terminalizer = updated.index("terminalize_pending_tool_calls(", error_handler)
    assert error_terminalizer < updated.index("if save_to_chat:", error_handler)
    result_loop = updated.index(
        "clarification_message = tool_batch_gate.clarification_message"
    )
    result_processing = updated.index("await process_tool_result(", result_loop)
    clarification_finish = updated.index(
        "clarification_finished = finish_successful_request_clarification("
    )
    assert result_processing < clarification_finish
    assert (
        updated.index("tool_calls.insert(0, unexecuted_terminal_calls)")
        < clarification_finish
    )
    assert updated.index(
        "if clarification_finished or terminal_finished:"
    ) > updated.index("'output': frontend_output")
    assert "result_loop_anchor" not in updated
    assert patch.patch_middleware_source(updated) == updated


def test_patched_middleware_stream_failure_pairs_pending_call_without_execution() -> (
    None
):
    patch = _load_module("evaluation_observability_stream_gate_patch", PATCH_PATH)
    observability = _load_module(
        "evaluation_observability_stream_gate_behavior", OBSERVABILITY_PATH
    )
    updated = patch.patch_middleware_source(_middleware_source())
    tree = ast.parse(updated)
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "handle_badmintonai_stream_failure"
    )
    source_loop = updated.index("await handle_badmintonai_stream_failure()")
    native_loop = updated.index("while tool_calls and (")
    assert source_loop < native_loop, "串流中斷 gate 必須先於原生工具執行迴圈"

    code = "\n".join(
        [
            "async def run_stream_failure_gate():",
            "    metadata = {'_badmintonai_stream_failure': 'stream_no_progress_timeout'}",
            "    output = [",
            "        {'type': 'function_call', 'id': 'done-call', 'call_id': 'done-call', 'name': 'runPythonAnalysis', 'status': 'completed'},",
            "        {'type': 'function_call_output', 'id': 'done-output', 'call_id': 'done-call', 'status': 'completed', 'output': []},",
            "        {'type': 'function_call', 'id': 'call-1', 'call_id': 'call-1', 'name': 'runPythonAnalysis', 'status': 'in_progress'},",
            "    ]",
            "    tool_calls = [[{'id': 'call-1', 'function': {'name': 'runPythonAnalysis', 'arguments': '{}'}}]]",
            "    events = []",
            "    saved_errors = []",
            "    executed = []",
            "    tool_turn_failed = False",
            "    no_progress_final_sent = False",
            "    no_progress_final_failed = False",
            "    def output_id(prefix): return prefix + '-output'",
            "    def full_output(): return output",
            "    async def event_emitter(event): events.append(event)",
            "    async def emit_message_error(message): saved_errors.append(message)",
            textwrap.indent(ast.unparse(handler), "    "),
            "    await handle_badmintonai_stream_failure()",
            "    while tool_calls:",
            "        executed.append('executed')",
            "        tool_calls.pop(0)",
            "    return output, tool_calls, events, saved_errors, executed, tool_turn_failed",
        ]
    )
    namespace = {
        "terminalize_pending_tool_calls": observability.terminalize_pending_tool_calls,
        "is_badmintonai_stream_failure": observability.is_badmintonai_stream_failure,
        "badmintonai_stream_failure_message": observability.badmintonai_stream_failure_message,
    }
    exec(compile(code, "<patched-stream-failure-gate>", "exec"), namespace)
    output, pending, events, saved_errors, executed, failed = asyncio.run(
        namespace["run_stream_failure_gate"]()
    )

    assert pending == []
    assert executed == [], "串流不完整時，不得執行未完成的工具意圖"
    assert failed is True, "串流失敗必須將回合及仍在進行的輸出標記失敗"
    assert output[0]["status"] == "completed"
    assert output[1]["status"] == "completed"
    failed_output = next(
        item
        for item in output
        if item.get("type") == "function_call_output"
        and item.get("call_id") == "call-1"
    )
    assert failed_output["call_id"] == "call-1"
    assert failed_output["status"] == "failed"
    assert failed_output["evaluation_event"] == {
        "type": "tool_not_executed",
        "reason": "stream_failure",
    }
    assert saved_errors == [
        observability.badmintonai_stream_failure_message(
            {"_badmintonai_stream_failure": "stream_no_progress_timeout"}
        )
    ]
    assert events == [], (
        "此 helper 留給 native middleware 的 done:true 尾端完成事件收尾"
    )


def test_patched_middleware_stream_failure_stops_followup_and_persists_error() -> None:
    patch = _load_module("evaluation_observability_stream_flow_patch", PATCH_PATH)
    observability = _load_module(
        "evaluation_observability_stream_flow_behavior", OBSERVABILITY_PATH
    )
    updated = patch.patch_middleware_source(_middleware_source())
    tree = ast.parse(updated)
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "handle_badmintonai_stream_failure"
    )
    emit_handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "emit_message_error"
    )
    post_stream_gate = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "await handle_badmintonai_stream_failure()"
        and any(isinstance(child, ast.Break) for child in ast.walk(node))
    )
    initial_stream_gate = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Await)
        and ast.unparse(node.value) == "await handle_badmintonai_stream_failure()"
    )
    tail_status_loop = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.For)
        and any(
            isinstance(child, ast.Assign)
            and ast.unparse(child.value)
            == "'failed' if tool_iteration_limit_reached or tool_turn_failed else 'completed'"
            for child in ast.walk(node)
        )
    )
    tail_current_output = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "current_output"
            for target in node.targets
        )
    )
    tail_completion_data = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "data"
            for target in node.targets
        )
        and "done" in ast.unparse(node.value)
    )
    tail_completion_emit = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Await)
        and isinstance(node.value.value, ast.Call)
        and node.value.value.args
        and isinstance(node.value.value.args[0], ast.Dict)
        and any(
            isinstance(key, ast.Constant)
            and key.value == "data"
            and isinstance(value, ast.Name)
            and value.id == "data"
            for key, value in zip(
                node.value.value.args[0].keys,
                node.value.value.args[0].values,
            )
        )
    )
    assert updated.index("await handle_badmintonai_stream_failure()") < updated.index(
        "while tool_calls and ("
    )
    assert (
        "await emit_message_error(badmintonai_stream_failure_message(metadata))"
        in ast.unparse(handler)
    )

    code = "\n".join(
        [
            "async def run_case(kind, partial_text, prior_output, pending_call, safe_final=False, fail=True):",
            "    metadata = {}",
            "    output = []",
            "    tool_calls = []",
            "    events = []",
            "    saved_errors = []",
            "    executed = []",
            "    upstream_requests = 0",
            "    tool_turn_failed = False",
            "    tool_iteration_limit_reached = False",
            "    no_progress_final_sent = safe_final",
            "    no_progress_final_failed = False",
            "    def output_id(prefix): return prefix + '-id'",
            "    def full_output(): return prior_output + output",
            "    async def event_emitter(event): events.append(event)",
            "    save_to_chat = True",
            textwrap.indent(ast.unparse(emit_handler), "    "),
            textwrap.indent(ast.unparse(handler), "    "),
            "    if kind == 'initial':",
            "        upstream_requests += 1",
            "        if partial_text:",
            "            output.append({'type': 'message', 'id': 'partial', 'status': 'in_progress', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': partial_text}]})",
            "        if pending_call:",
            "            calls = pending_call if isinstance(pending_call, list) else [pending_call]",
            "            tool_calls.append(calls)",
            "            for call in calls: output.append({'type': 'function_call', 'id': call['id'], 'call_id': call['id'], 'name': call['function']['name'], 'arguments': call['function']['arguments'], 'status': 'in_progress'})",
            "        if fail: metadata['_badmintonai_stream_failure'] = 'stream_no_progress_timeout'",
            "        " + ast.unparse(initial_stream_gate),
            "    else:",
            "        upstream_requests += 2",
            "        output = []",
            "        if partial_text:",
            "            output.append({'type': 'message', 'id': 'partial', 'status': 'in_progress', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': partial_text}]})",
            "        if pending_call:",
            "            calls = pending_call if isinstance(pending_call, list) else [pending_call]",
            "            tool_calls.append(calls)",
            "            for call in calls: output.append({'type': 'function_call', 'id': call['id'], 'call_id': call['id'], 'name': call['function']['name'], 'arguments': call['function']['arguments'], 'status': 'in_progress'})",
            "        if fail: metadata['_badmintonai_stream_failure'] = 'stream_total_timeout'",
            "        while True:",
            textwrap.indent(ast.unparse(post_stream_gate), "            "),
            "            if tool_calls:",
            "                executed.extend(call['id'] for batch in tool_calls for call in batch)",
            "                tool_calls.clear()",
            "                upstream_requests += 1",
            "            break",
            textwrap.indent(ast.unparse(tail_status_loop), "    "),
            textwrap.indent(ast.unparse(tail_current_output), "    "),
            textwrap.indent(ast.unparse(tail_completion_data), "    "),
            textwrap.indent(ast.unparse(tail_completion_emit), "    "),
            "    return upstream_requests, executed, tool_calls, full_output(), saved_errors, events, tool_turn_failed",
        ]
    )
    namespace = {
        "is_badmintonai_stream_failure": observability.is_badmintonai_stream_failure,
        "badmintonai_stream_failure_message": observability.badmintonai_stream_failure_message,
        "terminalize_pending_tool_calls": observability.terminalize_pending_tool_calls,
        "finish_no_progress_final_failure": observability.finish_no_progress_final_failure,
        "NO_PROGRESS_FINAL_FAILURE_MESSAGE": observability.NO_PROGRESS_FINAL_FAILURE_MESSAGE,
        "NO_PROGRESS_FINAL_TOOL_INTENT_MESSAGE": observability.NO_PROGRESS_FINAL_TOOL_INTENT_MESSAGE,
    }
    exec(compile(code, "<native-stream-failure-flow>", "exec"), namespace)

    no_output = asyncio.run(namespace["run_case"]("initial", "", [], None))
    partial = asyncio.run(namespace["run_case"]("initial", "部分內容", [], None))
    pending_call = {
        "id": "call-incomplete",
        "function": {"name": "runPythonAnalysis", "arguments": '{"code":"print('},
    }
    prior_chart = [
        {
            "type": "function_call",
            "id": "already-done",
            "call_id": "already-done",
            "name": "renderAnalysisChart",
            "arguments": "{}",
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "call_id": "already-done",
            "status": "completed",
            "embeds": ["chart-id"],
            "output": [{"type": "input_text", "text": "已發布圖表"}],
        },
    ]
    followup = asyncio.run(
        namespace["run_case"]("followup", "後續部分文字", prior_chart, pending_call)
    )
    safe_final = asyncio.run(
        namespace["run_case"](
            "followup", "安全收尾部分文字", prior_chart, None, safe_final=True
        )
    )
    normal_calls = [
        {"id": "call-a", "function": {"name": "runPythonAnalysis", "arguments": "{}"}},
        {
            "id": "call-b",
            "function": {"name": "renderAnalysisChart", "arguments": "{}"},
        },
    ]
    healthy = asyncio.run(
        namespace["run_case"]("followup", "", [], normal_calls, fail=False)
    )
    for result in (no_output, partial, followup, safe_final):
        requests, executed, pending, output, saved_errors, events, failed = result
        assert saved_errors, "串流超時必須走原生錯誤持久化入口"
        assert (
            events[-1]["type"] == "chat:completion"
            and events[-1]["data"]["done"] is True
        )
        assert failed is True
        assert pending == []
    assert no_output[0:2] == (1, [])
    assert partial[0:2] == (1, [])
    assert partial[3][0]["content"][0]["text"] == "部分內容"
    assert partial[3][0]["status"] == "failed"
    assert followup[0:3] == (2, [], [])
    assert followup[3][0]["call_id"] == "already-done"
    assert followup[3][1]["embeds"] == ["chart-id"], (
        "既有圖表工具結果不得被後續逾時清掉"
    )
    assert followup[3][2]["content"][0]["text"] == "後續部分文字"
    failed_call_output = next(
        item
        for item in followup[3]
        if item.get("type") == "function_call_output"
        and item.get("call_id") == "call-incomplete"
    )
    assert failed_call_output["status"] == "failed"
    assert failed_call_output["evaluation_event"] == {
        "type": "tool_not_executed",
        "reason": "stream_failure",
    }
    assert safe_final[4] == [
        observability.badmintonai_stream_failure_message(
            {"_badmintonai_stream_failure": "stream_total_timeout"}
        )
    ], "safe-final 串流失敗應保存原始串流原因，而非一般無回答訊息"
    assert observability.NO_PROGRESS_FINAL_FAILURE_MESSAGE not in safe_final[4]
    assert healthy[0] == 3 and healthy[1] == ["call-a", "call-b"]
    assert healthy[2] == [] and healthy[4] == [] and healthy[6] is False


def test_progress_guard_consecutive_successful_reads_force_final_once() -> None:
    observability = _load_module("read_segment_guard", OBSERVABILITY_PATH)
    guard = observability.EvaluationToolProgressGuard()
    read = {
        "result_id": "a" * 48,
        "artifacts": [
            {
                "relative_path": "data.csv",
                "size_bytes": 10,
                "text_preview": "abc",
                "preview_offset_bytes": 0,
                "next_offset_bytes": 3,
                "has_more": True,
            }
        ],
    }
    guard.observe_result(
        "readAnalysisResult", (read, {"Content-Type": "application/json"})
    )
    guard.observe_result(
        "renderAnalysisChart",
        {"status": "embedded", "result_id": "a" * 48, "chart_count": 1},
    )
    for index in range(3):
        guard.observe_result("readAnalysisResult", (read, None))
        assert guard.should_force_final() is (index == 2)
    assert (
        observability.decide_tool_batch_completion(
            clarification_message=None,
            terminal_message=None,
            pending_tool_calls=[],
            progress_guard=guard,
            near_iteration_limit=False,
            render_attempted=True,
        )
        == "safe_final"
    )
    guard.mark_final_attempted()
    guard.observe_result("readAnalysisResult", (read, None))
    assert not guard.should_force_final()


def test_progress_guard_reads_allow_changes_errors_and_preserve_failures() -> None:
    observability = _load_module("read_segment_changes_guard", OBSERVABILITY_PATH)
    read = {
        "result_id": "a" * 48,
        "artifacts": [
            {
                "relative_path": "data.csv",
                "size_bytes": 10,
                "text_preview": "abc",
                "preview_offset_bytes": 0,
                "next_offset_bytes": 3,
                "has_more": True,
            }
        ],
    }
    variants = [
        {**read, "result_id": "b" * 48},
        {**read, "artifacts": [{**read["artifacts"][0], "relative_path": "other.csv"}]},
        {
            **read,
            "artifacts": [
                {
                    **read["artifacts"][0],
                    "preview_offset_bytes": 3,
                    "next_offset_bytes": 6,
                }
            ],
        },
        {**read, "artifacts": [{**read["artifacts"][0], "text_preview": "def"}]},
        {**read, "error": "HTTP error 400"},
        {**read, "artifacts": [{**read["artifacts"][0], "next_offset_bytes": 4}]},
    ]
    for changed in variants:
        guard = observability.EvaluationToolProgressGuard()
        guard.observe_result("readAnalysisResult", read)
        guard.observe_result("readAnalysisResult", read)
        guard.observe_result("readAnalysisResult", (changed, None))
        assert not guard.should_force_final()
        guard.observe_result("readAnalysisResult", read)
        assert not guard.should_force_final()
    for failed_tool in ("runPythonAnalysis", "renderAnalysisChart"):
        guard = observability.EvaluationToolProgressGuard()
        guard.observe_result(failed_tool, {"error": "HTTP error 422"})
        for _ in range(3):
            guard.observe_result("readAnalysisResult", (read, None))
        assert guard.analysis_failure_unresolved or guard.render_failure_unresolved
        assert not guard.should_force_final(), "read 不能洗掉未修復失敗"


def test_progress_guard_counts_only_consecutive_duplicate_published_states() -> None:
    observability = _load_module(
        "evaluation_observability_progress_guard_test", OBSERVABILITY_PATH
    )
    guard = observability.EvaluationToolProgressGuard()
    published = {
        "status": "duplicate_suppressed",
        "result_id": "a" * 48,
        "chart_count": 1,
        "requested_result_id": "b" * 48,
    }

    guard.observe_result("renderAnalysisChart", published)
    assert not guard.should_force_final()
    changed_code_same_publish = {
        **published,
        "requested_result_id": "c" * 48,
        "code_hash": "changed-program-does-not-create-output",
    }
    guard.observe_result("renderAnalysisChart", changed_code_same_publish)
    assert guard.should_force_final(), "Q66 式改程式但仍指向同一已發布圖表應收斂"

    guard.mark_final_attempted()
    assert not guard.should_force_final()
    guard = observability.EvaluationToolProgressGuard()
    guard.observe_result(
        "renderAnalysisChart",
        {"status": "embedded", "result_id": "a" * 48, "chart_count": 1},
    )
    guard.observe_result(
        "runPythonAnalysis",
        {"status": "completed", "result_id": "d" * 48},
    )
    guard.observe_result("renderAnalysisChart", published)
    assert not guard.should_force_final(), "新分析應重設先前重複狀態"
    guard.observe_result("renderAnalysisChart", published)
    assert guard.should_force_final(), "重設後仍需連續兩次相同已發布狀態"


def test_progress_guard_uses_exact_saved_fingerprint_and_near_limit_evidence() -> None:
    observability = _load_module(
        "evaluation_observability_saved_fingerprint_guard", OBSERVABILITY_PATH
    )
    fingerprint = "f" * 64

    def formal_result(result_id: str, path: str, result_fingerprint: str = fingerprint):
        return {
            "result_id": result_id,
            "result_fingerprint": result_fingerprint,
            "artifacts": [{"relative_path": path, "text_preview": "small preview"}],
        }

    guard = observability.EvaluationToolProgressGuard()
    guard.observe_result("runPythonAnalysis", formal_result("a" * 48, "summary.json"))
    assert not guard.should_force_final()
    guard.observe_result("runPythonAnalysis", formal_result("b" * 48, "probe.json"))
    assert guard.should_force_final(), (
        "檔名與 result_id 改變但保存 bytes fingerprint 相同應收斂"
    )

    guard = observability.EvaluationToolProgressGuard()
    guard.observe_result("runPythonAnalysis", formal_result("a" * 48, "summary.json"))
    guard.observe_result(
        "runPythonAnalysis",
        formal_result("b" * 48, "summary.json", result_fingerprint="e" * 64),
    )
    assert not guard.should_force_final(), "新的正式結果可繼續多步分析"
    assert guard.should_force_final(near_limit=True), "近上限可保存一次收尾機會"

    guard = observability.EvaluationToolProgressGuard()
    probe = {"result_id": None, "artifacts": [], "stdout_preview": "欄位已探查"}
    guard.observe_result("runPythonAnalysis", probe)
    assert not guard.should_force_final(near_limit=True), (
        "stdout-only 探查不是正式保存結果"
    )

    guard = observability.EvaluationToolProgressGuard()
    invalid_formal_result = {
        "result_id": "a" * 48,
        "result_fingerprint": fingerprint,
        "artifacts": [{"relative_path": "stdout.txt"}],
    }
    guard.observe_result("runPythonAnalysis", invalid_formal_result)
    assert not guard.should_force_final(near_limit=True), "非正式檔案不算可驗證保存結果"

    guard = observability.EvaluationToolProgressGuard()
    guard.observe_result(
        "runPythonAnalysis",
        {"error": 'HTTP error 422: {"code":"analysis_code_error"}'},
    )
    guard.observe_result("runPythonAnalysis", formal_result("a" * 48, "summary.json"))
    assert not guard.should_force_final(), "錯誤後首次正式成功可恢復但不視為重複"
    assert guard.should_force_final(near_limit=True), "近上限可依最新已保存結果收尾"

    guard = observability.EvaluationToolProgressGuard()
    guard.observe_result("runPythonAnalysis", formal_result("a" * 48, "summary.json"))
    guard.observe_result(
        "runPythonAnalysis",
        {"error": 'HTTP error 422: {"code":"analysis_code_error"}'},
    )
    assert not guard.should_force_final(near_limit=True), (
        "成功後仍有未修復錯誤不得收尾成成功"
    )
    assert not observability.EvaluationToolProgressGuard().should_force_final(), (
        "下一個 assistant turn 的 guard 必須重設"
    )


def test_analysis_budget_response_reserves_one_final_only_with_clean_saved_evidence() -> (
    None
):
    observability = _load_module(
        "evaluation_observability_analysis_budget_guard", OBSERVABILITY_PATH
    )
    formal_result = {
        "result_id": "a" * 48,
        "result_fingerprint": "f" * 64,
        "analysis_runs_remaining": 0,
        "artifacts": [{"relative_path": "summary.json", "text_preview": "{}"}],
    }

    guard = observability.EvaluationToolProgressGuard()
    guard.observe_result("runPythonAnalysis", formal_result)
    assert guard.analysis_budget_exhausted
    assert guard.should_force_final(), "server 回報額度耗盡時應保留一次收尾"
    guard.mark_final_attempted()
    assert not guard.should_force_final(), "每個 assistant turn 最多一次收尾"

    guard = observability.EvaluationToolProgressGuard()
    guard.observe_result(
        "runPythonAnalysis",
        {
            "analysis_runs_remaining": 0,
            "result_id": None,
            "artifacts": [],
            "stdout_preview": "probe",
        },
    )
    assert guard.analysis_budget_exhausted
    assert not guard.should_force_final(), "stdout-only probe 不是已保存證據"

    failed_at_budget = observability.EvaluationToolProgressGuard()
    failed_at_budget.observe_result(
        "runPythonAnalysis",
        {
            "code": "analysis_code_error",
            "details": {"analysis_runs_remaining": 0},
        },
    )
    assert failed_at_budget.analysis_budget_exhausted
    assert not failed_at_budget.should_force_final(), (
        "錯誤細節的剩餘額度也須 server 確認"
    )

    guard = observability.EvaluationToolProgressGuard()
    guard.observe_result("runPythonAnalysis", formal_result)
    guard.observe_result(
        "runPythonAnalysis",
        {"error": 'HTTP error 422: {"code":"analysis_code_error"}'},
    )
    assert not guard.should_force_final(), "後續未修復錯誤不得被先前保存結果洗掉"


def test_tool_batch_completion_decision_keeps_stop_precedence_and_budget_failure() -> (
    None
):
    observability = _load_module(
        "evaluation_observability_batch_completion_decision", OBSERVABILITY_PATH
    )
    decide = observability.decide_tool_batch_completion
    guard = observability.EvaluationToolProgressGuard()
    guard.observe_result(
        "runPythonAnalysis",
        {
            "result_id": "a" * 48,
            "result_fingerprint": "f" * 64,
            "analysis_runs_remaining": 0,
            "artifacts": [{"relative_path": "summary.json"}],
        },
    )
    common = {
        "progress_guard": guard,
        "near_iteration_limit": False,
        "render_attempted": False,
    }
    assert (
        decide(
            clarification_message="question",
            terminal_message=None,
            pending_tool_calls=[],
            **common,
        )
        == "clarification"
    )
    assert (
        decide(
            clarification_message=None,
            terminal_message="terminal",
            pending_tool_calls=[],
            **common,
        )
        == "terminal"
    )
    assert (
        decide(
            clarification_message=None,
            terminal_message=None,
            pending_tool_calls=[{"id": "next"}],
            **common,
        )
        == "continue"
    )
    assert (
        decide(
            clarification_message=None,
            terminal_message=None,
            pending_tool_calls=[],
            **common,
        )
        == "safe_final"
    )

    guard.mark_final_attempted()
    assert (
        decide(
            clarification_message=None,
            terminal_message=None,
            pending_tool_calls=[],
            **common,
        )
        == "analysis_budget_failed"
    )

    failed_render_guard = observability.EvaluationToolProgressGuard()
    failed_render_guard.observe_result(
        "runPythonAnalysis",
        {
            "result_id": "b" * 48,
            "result_fingerprint": "e" * 64,
            "analysis_runs_remaining": 0,
            "artifacts": [{"relative_path": "summary.json"}],
        },
    )
    failed_render_guard.observe_result(
        "renderAnalysisChart", {"error": '{"code":"render_code_error"}'}
    )
    assert (
        decide(
            clarification_message=None,
            terminal_message=None,
            pending_tool_calls=[],
            progress_guard=failed_render_guard,
            near_iteration_limit=False,
            render_attempted=True,
        )
        == "continue"
    ), "尚有 renderer 修復機會時不可提早停止"


@pytest.mark.parametrize(
    "error_envelope",
    [
        "native tuple with headers",
        "native tuple without headers",
        "HTTP error string",
        "serialized outer error dict",
        "structured error dict",
    ],
)
def test_native_analysis_error_envelopes_update_remaining_without_success_evidence(
    error_envelope: str,
) -> None:
    observability = _load_module(
        f"evaluation_observability_budget_error_{error_envelope.replace(' ', '_')}",
        OBSERVABILITY_PATH,
    )
    inner_error = {
        "code": "analysis_code_error",
        "details": {"analysis_runs_remaining": 0},
        # 錯誤本文即使有貌似正式結果的欄位，也不是保存成功證據。
        "result_id": "b" * 48,
        "result_fingerprint": "e" * 64,
        "artifacts": [{"relative_path": "summary.json"}],
    }
    http_error = "HTTP error 422: " + json.dumps(inner_error)
    outer_error = {"error": http_error}
    if error_envelope == "native tuple with headers":
        result = (outer_error, {"content-type": "application/json"})
    elif error_envelope == "native tuple without headers":
        result = (outer_error, None)
    elif error_envelope == "HTTP error string":
        result = http_error
    elif error_envelope == "serialized outer error dict":
        result = json.dumps(outer_error)
    else:
        result = inner_error

    guard = observability.EvaluationToolProgressGuard()
    guard.observe_result(
        "runPythonAnalysis",
        (
            {
                "result_id": "a" * 48,
                "result_fingerprint": "f" * 64,
                "analysis_runs_remaining": 1,
                "artifacts": [{"relative_path": "summary.json"}],
            },
            None,
        ),
    )
    guard.observe_result("runPythonAnalysis", result)

    assert guard.analysis_runs_remaining == 0, error_envelope
    assert guard.analysis_budget_exhausted, error_envelope
    assert guard.analysis_failure_unresolved, error_envelope
    assert not guard.should_force_final(), "有未修復錯誤時不可藉先前保存結果成功收尾"

    gate = observability.EvaluationToolBatchGate(guard)
    skipped_analysis = {
        "id": "analysis-after-budget-error",
        "function": {"name": "runPythonAnalysis", "arguments": "{}"},
    }
    assert not gate.should_execute(skipped_analysis), "第 13 次分析必須被額度 gate 擋下"
    assert gate.should_execute({"function": {"name": "renderAnalysisChart"}}), (
        "額度耗盡只跳過後續分析，不阻擋同批 renderer"
    )
    paired_output: list[dict[str, object]] = []
    assert (
        observability.terminalize_pending_tool_calls(
            paired_output,
            [[skipped_analysis]],
            reason=observability.ANALYSIS_BUDGET_RESERVED_MESSAGE,
            id_factory=lambda: "analysis-budget-failed-output",
        )
        == 1
    )
    assert paired_output[-1]["call_id"] == skipped_analysis["id"]
    assert paired_output[-1]["status"] == "failed"

    # 額度錯誤後的 stdout-only 探查不能清除尚未修復的失敗。
    guard.observe_result(
        "runPythonAnalysis",
        (
            {
                "result_id": None,
                "artifacts": [],
                "analysis_runs_remaining": 0,
                "stdout_preview": "probe",
            },
            None,
        ),
    )
    assert guard.analysis_failure_unresolved
    assert not guard.should_force_final(near_limit=True)


@pytest.mark.parametrize("remaining", [True, -1, 13, 999])
def test_native_analysis_error_ignores_invalid_remaining_values(
    remaining: object,
) -> None:
    observability = _load_module(
        f"evaluation_observability_invalid_budget_{str(remaining).replace('-', 'minus_')}",
        OBSERVABILITY_PATH,
    )
    guard = observability.EvaluationToolProgressGuard()
    guard.observe_result(
        "runPythonAnalysis",
        {
            "result_id": "a" * 48,
            "result_fingerprint": "f" * 64,
            "analysis_runs_remaining": 1,
            "artifacts": [{"relative_path": "summary.json"}],
        },
    )
    guard.observe_result(
        "runPythonAnalysis",
        (
            {
                "error": "HTTP error 422: "
                + json.dumps(
                    {
                        "code": "analysis_code_error",
                        "details": {"analysis_runs_remaining": remaining},
                    }
                )
            },
            None,
        ),
    )
    assert guard.analysis_runs_remaining == 1
    assert not guard.analysis_budget_exhausted
    assert guard.analysis_failure_unresolved


def test_middleware_and_adapter_share_bounded_nested_error_decoder() -> None:
    observability = _load_module(
        "evaluation_observability_shared_payload_decoder", OBSERVABILITY_PATH
    )
    error = {
        "code": "analysis_code_error",
        "details": {"analysis_runs_remaining": 0},
        "result_id": "b" * 48,
        "result_fingerprint": "e" * 64,
        "artifacts": [{"relative_path": "summary.json"}],
    }
    wrapped = {
        "error": "HTTP error 422: " + json.dumps(error),
    }
    native_result = (wrapped, {"content-type": "application/json"})
    middleware_payload = observability.decode_tool_error_payload(
        observability._native_tool_response_payload("runPythonAnalysis", native_result)
    )
    adapter_payload = _decode_tool_result_payload(json.dumps(wrapped))

    assert middleware_payload == adapter_payload == error
    guard = observability.EvaluationToolProgressGuard()
    guard.observe_result("runPythonAnalysis", native_result)
    assert guard.analysis_runs_remaining == 0
    assert guard.analysis_failure_unresolved
    assert not guard.has_verified_result, "error body 中的結果欄位不是保存證據"

    oversized = json.dumps({"note": "x" * (64 * 1024)})
    assert observability.decode_tool_error_payload(oversized) is None
    assert _decode_tool_result_payload(oversized) is None


def test_v0113_budget_gate_skips_only_analysis_and_pairs_failed_output() -> None:
    observability = _load_module(
        "evaluation_observability_native_analysis_budget_test", OBSERVABILITY_PATH
    )
    guard = observability.EvaluationToolProgressGuard()
    guard.observe_result(
        "runPythonAnalysis",
        {
            "result_id": "a" * 48,
            "result_fingerprint": "f" * 64,
            "analysis_runs_remaining": 1,
            "artifacts": [{"relative_path": "summary.json"}],
        },
    )
    executed: list[str] = []
    accepted_analysis_calls = 0

    async def analysis():
        nonlocal accepted_analysis_calls
        accepted_analysis_calls += 1
        executed.append("runPythonAnalysis")
        return (
            {
                "result_id": "b" * 48,
                "result_fingerprint": "e" * 64,
                "analysis_runs_remaining": 0,
                "artifacts": [{"relative_path": "summary.json"}],
            },
            None,
        )

    async def render():
        executed.append("renderAnalysisChart")
        return ({"status": "embedded", "result_id": "a" * 48, "chart_count": 1}, None)

    async def ordinary():
        executed.append("ordinaryTool")
        return ({"ok": True}, None)

    accepted_call = {"id": "analysis-12", "function": {"name": "runPythonAnalysis"}}
    analysis_call = {"id": "analysis-late", "function": {"name": "runPythonAnalysis"}}
    render_call = {"id": "render-valid", "function": {"name": "renderAnalysisChart"}}
    ordinary_call = {"id": "ordinary-valid", "function": {"name": "ordinaryTool"}}

    results, gate = asyncio.run(
        _run_v0113_tool_batch(
            observability,
            [accepted_call, analysis_call, render_call, ordinary_call],
            {
                "runPythonAnalysis": analysis,
                "renderAnalysisChart": render,
                "ordinaryTool": ordinary,
            },
            progress_guard=guard,
        )
    )

    assert accepted_analysis_calls == 1, (
        "第 12 次受 server budget 允許，第 13 次不得執行"
    )
    assert executed == ["runPythonAnalysis", "renderAnalysisChart", "ordinaryTool"]
    assert results[id(analysis_call)] is None
    assert gate.budget_unexecuted_calls == [analysis_call]
    assert gate.unexecuted_calls == []
    assert gate.analysis_budget_reserved
    assert gate.executed_render_attempt

    output = [
        {
            "type": "function_call",
            "id": accepted_call["id"],
            "call_id": accepted_call["id"],
            "name": "runPythonAnalysis",
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "id": "analysis-12-output",
            "call_id": accepted_call["id"],
            "status": "completed",
            "output": [{"type": "input_text", "text": "saved result"}],
        },
        {
            "type": "function_call",
            "id": analysis_call["id"],
            "call_id": analysis_call["id"],
            "name": "runPythonAnalysis",
            "status": "in_progress",
        },
        {
            "type": "function_call",
            "id": render_call["id"],
            "call_id": render_call["id"],
            "name": "renderAnalysisChart",
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "id": "render-output",
            "call_id": render_call["id"],
            "status": "completed",
            "output": [{"type": "input_text", "text": "embedded"}],
        },
        {
            "type": "function_call",
            "id": ordinary_call["id"],
            "call_id": ordinary_call["id"],
            "name": "ordinaryTool",
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "id": "ordinary-output",
            "call_id": ordinary_call["id"],
            "status": "completed",
            "output": [{"type": "input_text", "text": "ok"}],
        },
    ]
    observability.terminalize_pending_tool_calls(
        output,
        [gate.budget_unexecuted_calls],
        reason=observability.ANALYSIS_BUDGET_RESERVED_MESSAGE,
        id_factory=lambda: "budget-failed-output",
    )
    paired = next(
        item
        for item in output
        if item.get("type") == "function_call_output"
        and item.get("call_id") == "analysis-late"
    )
    assert paired["status"] == "failed"
    assert paired["output"][0]["text"] == observability.ANALYSIS_BUDGET_RESERVED_MESSAGE
    assert guard.should_force_final(near_limit=guard.analysis_budget_exhausted)


def test_patched_middleware_uses_published_duplicate_state_for_one_shot_final() -> None:
    patch = _load_module("evaluation_observability_no_progress_patch", PATCH_PATH)
    observability = _load_module(
        "evaluation_observability_no_progress_behavior", OBSERVABILITY_PATH
    )
    updated = patch.patch_middleware_source(_middleware_source())
    tree = ast.parse(updated)

    observed_result = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "observe_result"
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "tool_progress_guard"
    )
    decision_names = {
        "tool_batch_action",
        "no_progress_final_requested",
        "near_iteration_limit",
    }
    decision_nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id in decision_names
            for target in node.targets
        )
    ]
    assert {
        target.id
        for node in decision_nodes
        for target in node.targets
        if isinstance(target, ast.Name)
    } >= decision_names
    action_assignment = next(
        node
        for node in decision_nodes
        if any(
            isinstance(target, ast.Name) and target.id == "tool_batch_action"
            for target in node.targets
        )
    )
    assert "pending_tool_calls=tool_calls" in ast.unparse(action_assignment.value)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    parent = parents.get(action_assignment)
    while parent is not None:
        assert not (
            isinstance(parent, ast.For)
            and isinstance(parent.iter, ast.Name)
            and parent.iter.id == "response_tool_calls"
        ), "no-progress 判斷必須等整個原生工具批次完成"
        parent = parents.get(parent)
    payload_guard = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "no_progress_final_requested"
        and any(
            isinstance(child, ast.Assign)
            and any(
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "tool_choice"
                and isinstance(child.value, ast.Constant)
                and child.value.value == "none"
                for target in child.targets
            )
            for child in ast.walk(node)
        )
    )
    forced_tool_guard = min(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == "no_progress_final_sent and tool_calls"
        ),
        key=lambda node: node.lineno,
    )

    code = "\n".join(
        [
            "async def run_patched_progress_flow():",
            "    tool_progress_guard = EvaluationToolProgressGuard()",
            "    max_tool_call_iterations = 16",
            "    tool_call_iterations = 3",
            "    output = []",
            "    tool_calls = []",
            "    requests = []",
            "    def full_output():",
            "        return output",
            "    def normalize_messages_for_model(data):",
            "        return data",
            "    async def generate_chat_completion(request, data, user):",
            "        provider_payload = dict(data)",
            "        # 固定 v0.11.3 apply_model_params_to_body_openai 只補缺少的參數。",
            "        for key, value in {'tool_choice': 'auto', 'tools': [{'name': 'late-injected'}]}.items():",
            "            if key not in provider_payload:",
            "                provider_payload[key] = value",
            "        requests.append(provider_payload)",
            "        assert provider_payload['tools'] == declared_tools",
            "        for request_payload in requests:",
            "            assistant_calls = [call for message in request_payload['messages'] if message.get('role') == 'assistant' for call in message.get('tool_calls', [])]",
            "            tool_outputs = {message.get('tool_call_id') for message in request_payload['messages'] if message.get('role') == 'tool'}",
            "            assert all(call['id'] in tool_outputs for call in assistant_calls), '續接payload必須保留原生tool-call/output配對'",
            "        if len(requests) == 2:",
            "            assert provider_payload['tool_choice'] == 'none'",
            "            assert provider_payload['tools'] == declared_tools",
            "            assert len(provider_payload['tools']) == 3, '不得在收尾時注入其他工具'",
            "        if len(requests) == 1:",
            "            return {'tool_calls': ['next-render']} ",
            "        return {'tool_calls': [forced_tool_call]}",
            "    name = 'renderAnalysisChart'",
            "    result = (duplicate_result, None)",
            "    clarification_message = None",
            "    terminal_tool_message = None",
            "    no_progress_final_requested = False",
            "    tool_batch_gate = EvaluationToolBatchGate(tool_progress_guard)",
            "    " + ast.unparse(observed_result),
            "    tool_calls = []",
            *["    " + ast.unparse(node) for node in decision_nodes],
            "    if no_progress_final_requested:",
            "        tool_progress_guard.mark_final_attempted()",
            "    declared_tools = [{'type': 'function', 'function': {'name': 'runPythonAnalysis'}}, {'type': 'function', 'function': {'name': 'renderAnalysisChart'}}, {'type': 'function', 'function': {'name': 'requestClarification'}}]",
            "    paired_tool_messages = [{'role': 'assistant', 'tool_calls': [{'id': 'render-call-1', 'type': 'function', 'function': {'name': 'renderAnalysisChart', 'arguments': '{}'}}]}, {'role': 'tool', 'tool_call_id': 'render-call-1', 'content': '{\"status\":\"duplicate_suppressed\",\"result_id\":\"' + 'a' * 48 + '\"}'}]",
            "    new_form_data = {'tools': declared_tools, 'tool_choice': 'auto', 'messages': paired_tool_messages.copy()}",
            "    new_form_data = normalize_messages_for_model(new_form_data)",
            "    no_progress_final_sent = False",
            textwrap.indent(ast.unparse(payload_guard), "    "),
            "    first_res = await generate_chat_completion(None, new_form_data, None)",
            "    output.extend([{'type': 'function_call', 'id': 'render-call-item', 'call_id': 'render-call-1', 'name': 'renderAnalysisChart', 'status': 'completed'}, {'type': 'function_call_output', 'id': 'render-output-item', 'call_id': 'render-call-1', 'status': 'completed', 'embeds': ['existing-chart'], 'output': [{'type': 'input_text', 'text': '已發布'}]}])",
            "    output.append({'type': 'message', 'id': 'progress', 'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text', 'text': '我先分析…'}]})",
            "    result = (duplicate_result_changed_code, None)",
            "    tool_batch_gate = EvaluationToolBatchGate(tool_progress_guard)",
            "    " + ast.unparse(observed_result),
            "    tool_calls = []",
            *["    " + ast.unparse(node) for node in decision_nodes],
            "    if no_progress_final_requested:",
            "        tool_progress_guard.mark_final_attempted()",
            "    new_form_data = {'tools': declared_tools, 'tool_choice': 'auto', 'messages': paired_tool_messages.copy()}",
            "    new_form_data = normalize_messages_for_model(new_form_data)",
            "    no_progress_final_sent = False",
            textwrap.indent(ast.unparse(payload_guard), "    "),
            "    second_res = await generate_chat_completion(None, new_form_data, None)",
            "    tool_calls = [[forced_tool_call]]",
            "    output.append({'type': 'function_call', 'id': 'fc-final', 'call_id': 'forced-call', 'name': 'runPythonAnalysis', 'status': 'in_progress'})",
            "    executed = []",
            "    async def event_emitter(event):",
            "        return None",
            "    async def emit_message_error(message):",
            "        return None",
            "    def output_id(prefix):",
            "        return f'{prefix}-no-progress'",
            "    while True:",
            textwrap.indent(ast.unparse(forced_tool_guard), "        "),
            "        executed.append('tool executed')",
            "        break",
            "    return requests, output, tool_calls, executed, first_res, second_res",
        ]
    )
    namespace = {
        "EvaluationToolProgressGuard": observability.EvaluationToolProgressGuard,
        "EvaluationToolBatchGate": observability.EvaluationToolBatchGate,
        "decide_tool_batch_completion": observability.decide_tool_batch_completion,
        "duplicate_result": {
            "status": "duplicate_suppressed",
            "result_id": "a" * 48,
            "requested_result_id": "b" * 48,
            "chart_count": 1,
        },
        "duplicate_result_changed_code": {
            "status": "duplicate_suppressed",
            "result_id": "a" * 48,
            "requested_result_id": "c" * 48,
            "chart_count": 1,
        },
        "forced_tool_call": {
            "id": "forced-call",
            "function": {"name": "runPythonAnalysis", "arguments": "{}"},
        },
        "finish_no_progress_final_failure": observability.finish_no_progress_final_failure,
        "NO_PROGRESS_FINAL_TOOL_INTENT_MESSAGE": observability.NO_PROGRESS_FINAL_TOOL_INTENT_MESSAGE,
        "NO_PROGRESS_FINAL_FAILURE_MESSAGE": observability.NO_PROGRESS_FINAL_FAILURE_MESSAGE,
        "terminalize_pending_tool_calls": observability.terminalize_pending_tool_calls,
        "NO_PROGRESS_FINAL_PROMPT": observability.NO_PROGRESS_FINAL_PROMPT,
    }
    exec(compile(code, "<native-no-progress-control-flow>", "exec"), namespace)
    requests, output, pending_calls, executed, first_res, second_res = asyncio.run(
        namespace["run_patched_progress_flow"]()
    )

    assert len(requests) == 2, "一次一般續問後，重複狀態只允許一次收尾請求"
    expected_tools = [
        {"type": "function", "function": {"name": "runPythonAnalysis"}},
        {"type": "function", "function": {"name": "renderAnalysisChart"}},
        {"type": "function", "function": {"name": "requestClarification"}},
    ]
    assert requests[0]["tools"] == expected_tools
    assert requests[0]["tool_choice"] == "auto"
    assert requests[1]["tools"] == expected_tools
    assert requests[1]["tool_choice"] == "none"
    assert requests[1]["messages"][0]["role"] == "assistant"
    assert requests[1]["messages"][0]["tool_calls"][0]["id"] == "render-call-1"
    assert requests[1]["messages"][1]["role"] == "tool"
    assert requests[1]["messages"][1]["tool_call_id"] == "render-call-1"
    assert (
        observability.NO_PROGRESS_FINAL_PROMPT in requests[1]["messages"][-1]["content"]
    )
    assert first_res["tool_calls"] and second_res["tool_calls"]
    assert pending_calls == []
    assert executed == [], "工具停用收尾若仍回傳工具意圖，不可執行"
    call = next(
        item
        for item in output
        if item.get("type") == "function_call" and item.get("call_id") == "forced-call"
    )
    paired = next(
        item
        for item in output
        if item.get("type") == "function_call_output"
        and item.get("call_id") == "forced-call"
    )
    assert call["status"] == paired["status"] == "failed"
    assert call["call_id"] == paired["call_id"] == "forced-call"
    existing_render = next(
        item
        for item in output
        if item.get("type") == "function_call_output"
        and item.get("call_id") == "render-call-1"
    )
    assert existing_render["embeds"] == ["existing-chart"]
    assert any(
        item.get("id") == "progress"
        and item.get("content", [{}])[0].get("text") == "我先分析…"
        for item in output
    ), "收尾失敗只追加安全提示，不覆蓋先前可見文字"
    assert any(
        item.get("role") == "assistant"
        and item.get("status") == "completed"
        and item.get("content", [{}])[0].get("text")
        == observability.NO_PROGRESS_FINAL_TOOL_INTENT_MESSAGE
        for item in output
        if item.get("type") == "message"
    )


def test_patched_middleware_uses_server_budget_for_one_tools_disabled_final() -> None:
    patch = _load_module("evaluation_observability_budget_patch", PATCH_PATH)
    observability = _load_module(
        "evaluation_observability_budget_behavior", OBSERVABILITY_PATH
    )
    updated = patch.patch_middleware_source(_middleware_source())
    tree = ast.parse(updated)
    assert "EvaluationToolBatchGate(tool_progress_guard)" in updated
    assert "ANALYSIS_BUDGET_RESERVED_MESSAGE" in updated
    assert "ANALYSIS_BUDGET_EXHAUSTED_MESSAGE" in updated
    assert "event_reason='analysis_budget_reserved'" in updated
    assert (
        "terminal_tool_message = ANALYSIS_BUDGET_EXHAUSTED_MESSAGE\n                        tool_turn_failed = True"
        in updated
    )
    assert updated.count("new_form_data['tool_choice'] = 'none'") == 1
    assert "new_form_data['tools']" not in updated, "收尾不得移除原工具宣告或改寫宣告"

    assignment = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "tool_batch_action"
            for target in node.targets
        )
    )
    guard = observability.EvaluationToolProgressGuard()
    guard.observe_result(
        "runPythonAnalysis",
        {
            "result_id": "a" * 48,
            "result_fingerprint": "f" * 64,
            "analysis_runs_remaining": 0,
            "artifacts": [{"relative_path": "summary.json"}],
        },
    )
    eval_globals = {
        "tool_progress_guard": guard,
        "decide_tool_batch_completion": observability.decide_tool_batch_completion,
    }
    eval_locals = {
        "clarification_message": None,
        "terminal_tool_message": None,
        "tool_calls": [],
        "near_iteration_limit": False,
        "tool_batch_gate": observability.EvaluationToolBatchGate(guard),
    }
    expression = compile(
        ast.Expression(assignment.value), "patched-final-decision", "eval"
    )
    assert eval(expression, eval_globals, eval_locals) == "safe_final"

    budget_terminal_branch = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            isinstance(statement, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "terminal_tool_message"
                for target in statement.targets
            )
            and isinstance(statement.value, ast.Name)
            and statement.value.id == "ANALYSIS_BUDGET_EXHAUSTED_MESSAGE"
            for statement in node.body
        )
    )
    terminal_condition = compile(
        ast.Expression(budget_terminal_branch.test), "budget-terminal-decision", "eval"
    )
    probe_guard = observability.EvaluationToolProgressGuard()
    probe_guard.observe_result(
        "runPythonAnalysis",
        {
            "analysis_runs_remaining": 0,
            "result_id": None,
            "artifacts": [],
            "stdout_preview": "probe",
        },
    )
    terminal_context = {
        "tool_batch_action": "analysis_budget_failed",
    }
    assert eval(terminal_condition, {}, terminal_context) is True

    render_error_guard = observability.EvaluationToolProgressGuard()
    render_error_guard.observe_result(
        "runPythonAnalysis",
        {
            "result_id": "b" * 48,
            "result_fingerprint": "e" * 64,
            "analysis_runs_remaining": 0,
            "artifacts": [{"relative_path": "summary.json"}],
        },
    )
    render_error_gate = observability.EvaluationToolBatchGate(render_error_guard)
    render_error = {"error": 'HTTP error 422: {"code":"render_code_error"}'}
    render_error_gate.observe_result("renderAnalysisChart", render_error)
    render_error_guard.observe_result("renderAnalysisChart", render_error)
    assert render_error_gate.executed_render_attempt
    assert render_error_guard.render_failure_unresolved
    terminal_context.update(
        {
            "tool_progress_guard": render_error_guard,
            "tool_batch_gate": render_error_gate,
            "tool_batch_action": observability.decide_tool_batch_completion(
                clarification_message=None,
                terminal_message=None,
                pending_tool_calls=[],
                progress_guard=render_error_guard,
                near_iteration_limit=False,
                render_attempted=render_error_gate.executed_render_attempt,
            ),
        }
    )
    assert eval(terminal_condition, {}, terminal_context) is False, (
        "budget 用完時仍應讓可修正的 render error 有一次工具續接機會"
    )

    declared_tools = [
        {"type": "function", "function": {"name": "runPythonAnalysis"}},
        {"type": "function", "function": {"name": "renderAnalysisChart"}},
    ]
    provider_requests = []
    if eval(expression, eval_globals, eval_locals):
        provider_requests.append({"tools": declared_tools, "tool_choice": "none"})
        guard.mark_final_attempted()
    assert eval(expression, eval_globals, eval_locals) == "analysis_budget_failed"
    assert provider_requests == [{"tools": declared_tools, "tool_choice": "none"}]


def test_native_16_iteration_limit_emits_final_and_preserves_partial_as_failed() -> (
    None
):
    patch = _load_module("evaluation_observability_iteration_limit_patch", PATCH_PATH)
    observability = _load_module(
        "evaluation_observability_iteration_limit_behavior", OBSERVABILITY_PATH
    )
    updated = patch.patch_middleware_source(_middleware_source())
    tree = ast.parse(updated)
    iteration_branch = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and "tool_iteration_calls" not in ast.unparse(node.test)
        and "tool_iteration_limit_reached" not in ast.unparse(node.test)
        and "max_tool_call_iterations" in ast.unparse(node.test)
        and "tool_call_iterations" in ast.unparse(node.test)
    )
    interpreter_guard = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and "DETECT_CODE_INTERPRETER" in ast.unparse(node.test)
        and "tool_iteration_limit_reached" in ast.unparse(node.test)
    )
    final_status_loop = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.For)
        and ast.unparse(node.target) == "item"
        and "tool_iteration_limit_reached" in ast.unparse(node)
    )

    async def run_limit(initial_output: list[dict]) -> dict:
        code = "\n".join(
            [
                "async def run_iteration_limit(initial_output, initial_batches):",
                "    max_tool_call_iterations = 16",
                "    tool_call_iterations = 0",
                "    model_continuations = 0",
                "    for _ in range(max_tool_call_iterations):",
                "        tool_call_iterations += 1",
                "        model_continuations += 1",
                "    tool_iteration_limit_reached = False",
                "    evaluation_request = False",
                "    DETECT_CODE_INTERPRETER = True",
                "    output = initial_output",
                "    tool_calls = initial_batches",
                "    errors = []",
                "    events = []",
                "    llm_continuations_after_limit = []",
                "    def output_id(prefix):",
                "        return f'{prefix}-test-{len(output)}'",
                "    def full_output():",
                "        return output",
                "    async def event_emitter(event):",
                "        events.append(event)",
                "    async def emit_message_error(error_content):",
                "        terminalize_pending_tool_calls(",
                "            output, tool_calls, reason='工具呼叫因本輪錯誤而未執行。',",
                "            id_factory=lambda: output_id('fco'),",
                "        )",
                "        errors.append(error_content)",
                "    async def fake_llm_continuation():",
                "        llm_continuations_after_limit.append(True)",
                textwrap.indent(ast.unparse(iteration_branch), "    "),
                textwrap.indent(ast.unparse(interpreter_guard), "    "),
                textwrap.indent(ast.unparse(final_status_loop), "    "),
                "    events.append({'type': 'chat:completion', 'data': {'done': True, 'output': output}})",
                "    return {",
                "        'output': output, 'tool_calls': tool_calls, 'errors': errors,",
                "        'events': events, 'model_continuations': model_continuations,",
                "        'llm_continuations_after_limit': llm_continuations_after_limit,",
                "    }",
            ]
        )
        namespace = {
            "TOOL_ITERATION_LIMIT_MESSAGE": observability.TOOL_ITERATION_LIMIT_MESSAGE,
            "finish_tool_iteration_limit_turn": observability.finish_tool_iteration_limit_turn,
            "terminalize_pending_tool_calls": observability.terminalize_pending_tool_calls,
            "log": logging.getLogger("test.iteration-limit"),
        }
        exec(compile(code, "<patched-native-iteration-limit>", "exec"), namespace)
        pending = [
            [
                {
                    "id": "call-17",
                    "function": {"name": "runPythonAnalysis", "arguments": "{}"},
                }
            ]
        ]
        return await namespace["run_iteration_limit"](initial_output, pending)

    prior_results = [
        item
        for iteration in range(1, 17)
        for item in (
            {
                "type": "function_call",
                "id": f"fc-{iteration}",
                "call_id": f"call-{iteration}",
                "name": "runPythonAnalysis",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "id": f"fco-{iteration}",
                "call_id": f"call-{iteration}",
                "status": "completed",
                "output": [{"type": "input_text", "text": "analysis result"}],
            },
        )
    ]

    blank = asyncio.run(run_limit(prior_results.copy()))
    assert blank["model_continuations"] == 16
    assert blank["llm_continuations_after_limit"] == []
    assert blank["errors"] == [observability.TOOL_ITERATION_LIMIT_MESSAGE]
    final_messages = [
        item
        for item in blank["output"]
        if item.get("type") == "message" and item.get("role") == "assistant"
    ]
    assert len(final_messages) == 1
    assert (
        final_messages[0]["content"][0]["text"]
        == observability.TOOL_ITERATION_LIMIT_MESSAGE
    )
    assert final_messages[0]["status"] == "completed"
    pending_call = next(
        item
        for item in blank["output"]
        if item.get("type") == "function_call" and item.get("call_id") == "call-17"
    )
    pending_output = next(
        item
        for item in blank["output"]
        if item.get("type") == "function_call_output"
        and item.get("call_id") == "call-17"
    )
    assert pending_call["status"] == pending_output["status"] == "failed"
    assert blank["events"][-1]["data"]["done"] is True
    assert all(item["status"] == "completed" for item in prior_results)

    partial_message = {
        "type": "message",
        "id": "partial-message",
        "role": "assistant",
        "status": "in_progress",
        "content": [{"type": "output_text", "text": "已保留的部分內容"}],
    }
    partial_output = prior_results.copy() + [
        {
            "type": "function_call_output",
            "id": "chart-result",
            "call_id": "chart-call",
            "status": "completed",
            "embeds": ["existing-chart"],
            "output": [{"type": "input_text", "text": "圖表已發布"}],
        },
        partial_message,
    ]
    partial = asyncio.run(run_limit(partial_output))
    assert partial_message["content"][0]["text"] == "已保留的部分內容"
    assert partial_message["status"] == "failed"
    assert sum(item.get("type") == "message" for item in partial["output"]) == 1
    assert next(item for item in partial["output"] if item.get("id") == "chart-result")[
        "embeds"
    ] == ["existing-chart"]
    assert (
        next(
            item
            for item in partial["output"]
            if item.get("type") == "function_call" and item.get("call_id") == "call-17"
        )["status"]
        == "failed"
    )
    assert partial["llm_continuations_after_limit"] == []
    assert partial["events"][-1]["data"]["done"] is True


def test_terminal_tool_error_is_visible_and_unexecuted_calls_are_failed() -> None:
    observability = _load_module(
        "evaluation_observability_terminal_failure_behavior_test", OBSERVABILITY_PATH
    )
    # 固定版 Open WebUI execute_tool_server 的錯誤回傳是 (error body, None)。
    nested_error = (
        {
            "error": "HTTP error 429: "
            + '{"code":"analysis_retry_limit","details":{"terminal":true}}'
        },
        None,
    )

    message = observability.terminal_tool_completion_message(
        "runPythonAnalysis", nested_error
    )
    assert message == ("分析未完成；已停止自動修正，本次不提供未驗證的統計數值。")
    assert (
        observability.terminal_tool_completion_message(
            "runPythonAnalysis",
            {"code": "analysis_code_error", "details": {"terminal": False}},
        )
        is None
    )
    assert (
        observability.terminal_tool_completion_message(
            "renderAnalysisChart",
            {"code": "chart_state_unknown", "details": {"terminal": True}},
        )
        == "互動圖表的發布狀態無法確認；請先查看原對話是否已有圖表，避免重複發布。"
    )
    assert (
        observability.terminal_tool_completion_message(
            "renderAnalysisChart",
            {"code": "chart_embed_failed", "details": {"terminal": True}},
        )
        == "互動圖表未能完成發布，已停止自動修正。"
    )

    output = [
        {
            "type": "function_call",
            "id": "call-terminal",
            "call_id": "call-terminal",
            "name": "runPythonAnalysis",
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "id": "result-terminal",
            "call_id": "call-terminal",
            "status": "completed",
            "output": [{"type": "input_text", "text": "private HTTP wrapper"}],
        },
    ]
    pending_batches = [
        [
            {
                "id": "call-not-run",
                "function": {
                    "name": "runPythonAnalysis",
                    "arguments": "{}",
                },
            }
        ]
    ]
    identifiers = iter(("msg-terminal", "result-not-run"))
    finished = observability.finish_terminal_tool_turn(
        output,
        message,
        pending_batches,
        message_id_factory=lambda: next(identifiers),
        result_id_factory=lambda: next(identifiers),
    )
    continuation_calls = 0
    events = []
    if finished:
        events.append({"type": "chat:completion", "data": {"output": output}})
    else:
        continuation_calls += 1
    events.append(
        {
            "type": "chat:completion",
            "data": {"done": True, "output": output, "usage": {"total_tokens": 9}},
        }
    )

    assert finished is True
    assert continuation_calls == 0
    assert pending_batches == []
    assert output[1]["output"][0]["text"] == "private HTTP wrapper"
    assert any(
        item.get("type") == "message"
        and "分析未完成；已停止自動修正" in item["content"][0]["text"]
        for item in output
    )
    assert any(
        item.get("type") == "function_call_output"
        and item.get("call_id") == "call-not-run"
        and item.get("status") == "failed"
        for item in output
    )
    assert events[-1]["data"]["done"] is True
    assert events[-1]["data"]["usage"] == {"total_tokens": 9}


def test_patched_middleware_unexecuted_event_roundtrips_to_evaluation_adapter() -> None:
    patch = _load_module(
        "evaluation_observability_patch_unexecuted_roundtrip", PATCH_PATH
    )
    observability = _load_module(
        "evaluation_observability_unexecuted_roundtrip", OBSERVABILITY_PATH
    )
    updated = patch.patch_middleware_source(_middleware_source())
    compile(updated, "middleware.py", "exec")
    assert "finish_terminal_tool_turn(" in updated

    # 這是 patched middleware 終止工具批次時呼叫的原生 output helper。
    skipped = {
        "id": "render-skipped",
        "function": {"name": "renderAnalysisChart", "arguments": "{}"},
    }
    output = [
        {
            "type": "function_call",
            "id": "analysis-terminal",
            "call_id": "analysis-terminal",
            "name": "runPythonAnalysis",
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "id": "analysis-terminal-output",
            "call_id": "analysis-terminal",
            "status": "completed",
            "output": [
                {
                    "type": "input_text",
                    "text": json.dumps(
                        {"code": "analysis_retry_limit", "details": {"terminal": True}}
                    ),
                }
            ],
        },
        {
            "type": "function_call",
            "id": "render-skipped",
            "call_id": "render-skipped",
            "name": "renderAnalysisChart",
            "status": "in_progress",
        },
    ]
    assert observability.finish_terminal_tool_turn(
        output,
        "分析未完成；已停止自動修正，本次不提供未驗證的統計數值。",
        [[skipped]],
        message_id_factory=lambda: "terminal-message",
        result_id_factory=lambda: "skipped-render-output",
    )
    skipped_output = next(
        item
        for item in output
        if item.get("type") == "function_call_output"
        and item.get("call_id") == "render-skipped"
    )
    assert skipped_output["evaluation_event"] == {
        "type": "tool_not_executed",
        "reason": "terminal_tool",
    }
    # 變更可見文案不影響新紀錄分類。
    skipped_output["output"] = [{"type": "input_text", "text": "文案已本地化更新"}]
    error = _analysis_completion_error(
        [
            {
                "name": "runPythonAnalysis",
                "call_id": "analysis-terminal",
                "status": "completed",
            },
            {
                "name": "renderAnalysisChart",
                "call_id": "render-skipped",
                "status": "failed",
            },
        ],
        output,
        [],
    )
    assert error is not None and error.get("code") == "analysis_retry_limit"

    # 真正已執行的 renderer failure 仍須勝過另一個未執行 renderer event。
    render_output = [
        {
            "type": "function_call_output",
            "call_id": "render-executed",
            "status": "failed",
            "output": [{"type": "input_text", "text": '{"code":"render_code_error"}'}],
        }
    ]
    observability.terminalize_pending_tool_calls(
        render_output,
        [[{"id": "render-skipped-2", "function": {"name": "renderAnalysisChart"}}]],
        reason="此 renderer 未執行",
        id_factory=lambda: "render-skipped-output-2",
        event_reason="analysis_budget_reserved",
    )
    render_error = _analysis_completion_error(
        [
            {
                "name": "renderAnalysisChart",
                "call_id": "render-executed",
                "status": "failed",
            },
            {
                "name": "renderAnalysisChart",
                "call_id": "render-skipped-2",
                "status": "failed",
            },
        ],
        render_output,
        [],
    )
    assert render_error is not None and render_error.get("code") == "render_failed"


async def _run_v0113_tool_batch(
    observability, response_tool_calls, handlers, *, progress_guard=None
):
    """依 v0.11.3 順序逐一執行普通工具，再 gather delegate_task。"""

    gate = observability.EvaluationToolBatchGate(progress_guard)
    tool_results = {}

    async def execute_tool_call(tool_call):
        if not gate.should_execute(tool_call):
            return None
        name = tool_call["function"]["name"]
        result = await handlers[name]()
        gate.observe_result(name, result)
        if progress_guard is not None:
            progress_guard.observe_result(name, result)
        return result

    delegate_calls = [
        tool_call
        for tool_call in response_tool_calls
        if tool_call["function"]["name"] == "delegate_task"
    ]
    for tool_call in response_tool_calls:
        if tool_call["function"]["name"] != "delegate_task":
            tool_results[id(tool_call)] = await execute_tool_call(tool_call)
    tool_results.update(
        zip(
            [id(tool_call) for tool_call in delegate_calls],
            await asyncio.gather(
                *(execute_tool_call(tool_call) for tool_call in delegate_calls)
            ),
        )
    )
    return tool_results, gate


def test_v0113_terminal_tool_stops_later_side_effects_and_pairs_failed_calls() -> None:
    observability = _load_module(
        "evaluation_observability_native_batch_terminal_test", OBSERVABILITY_PATH
    )
    executed: list[str] = []

    async def terminal_analysis():
        executed.append("runPythonAnalysis")
        # execute_tool_server error: (response_data, None)，原 tuple 仍交給 native flow。
        return (
            {
                "error": "HTTP error 429: "
                + '{"code":"analysis_retry_limit","details":{"terminal":true}}'
            },
            None,
        )

    async def side_effecting_tool():
        executed.append("sideEffectingTool")
        return ({"ok": True}, {"content-type": "application/json"})

    async def delegated_task():
        executed.append("delegate_task")
        return {"ok": True}

    calls = [
        {"id": "call-analysis", "function": {"name": "runPythonAnalysis"}},
        {"id": "call-side-effect", "function": {"name": "sideEffectingTool"}},
        {"id": "call-delegate", "function": {"name": "delegate_task"}},
    ]
    results, gate = asyncio.run(
        _run_v0113_tool_batch(
            observability,
            calls,
            {
                "runPythonAnalysis": terminal_analysis,
                "sideEffectingTool": side_effecting_tool,
                "delegate_task": delegated_task,
            },
        )
    )

    assert executed == ["runPythonAnalysis"]
    assert results[id(calls[1])] is None
    assert results[id(calls[2])] is None
    assert gate.terminal_message is not None
    assert gate.unexecuted_calls == calls[1:]

    output = [
        {
            "type": "function_call",
            "id": f"fc-{call['id']}",
            "call_id": call["id"],
            "name": call["function"]["name"],
            "status": "in_progress",
        }
        for call in calls
    ]
    output.append(
        {
            "type": "function_call_output",
            "id": "fco-analysis",
            "call_id": calls[0]["id"],
            "status": "failed",
            "output": [{"type": "input_text", "text": "terminal error"}],
        }
    )
    identifiers = iter(("msg-final", "fco-side-effect", "fco-delegate"))
    assert observability.finish_terminal_tool_turn(
        output,
        gate.terminal_message,
        [gate.unexecuted_calls],
        message_id_factory=lambda: next(identifiers),
        result_id_factory=lambda: next(identifiers),
    )
    for call in calls[1:]:
        function_call = next(
            item
            for item in output
            if item.get("call_id") == call["id"] and item["type"] == "function_call"
        )
        function_output = next(
            item
            for item in output
            if item.get("call_id") == call["id"]
            and item["type"] == "function_call_output"
        )
        assert function_call["status"] == "failed"
        assert function_output["status"] == "failed"

    continuation_calls = 0
    final_events = []
    if gate.terminal_message is not None:
        final_events.append({"type": "chat:completion", "data": {"output": output}})
    else:
        continuation_calls += 1
    final_events.append(
        {"type": "chat:completion", "data": {"done": True, "output": output}}
    )
    assert continuation_calls == 0
    assert any(
        item.get("type") == "message"
        and "分析未完成；已停止自動修正" in item["content"][0]["text"]
        for item in output
    )
    assert final_events[-1]["data"]["done"] is True


def test_v0113_successful_analysis_tuple_and_ordinary_tool_tuple_continue_normally() -> (
    None
):
    observability = _load_module(
        "evaluation_observability_native_batch_success_test", OBSERVABILITY_PATH
    )
    executed: list[str] = []

    async def successful_analysis():
        executed.append("runPythonAnalysis")
        return (
            {"result_id": "saved-result", "artifacts": [{"filename": "summary.json"}]},
            {"content-type": "application/json"},
        )

    async def ordinary_tool():
        executed.append("ordinaryTool")
        return ({"status": "ok"}, {"content-type": "application/json"})

    calls = [
        {"id": "call-analysis-success", "function": {"name": "runPythonAnalysis"}},
        {"id": "call-ordinary", "function": {"name": "ordinaryTool"}},
    ]
    results, gate = asyncio.run(
        _run_v0113_tool_batch(
            observability,
            calls,
            {
                "runPythonAnalysis": successful_analysis,
                "ordinaryTool": ordinary_tool,
            },
        )
    )

    assert executed == ["runPythonAnalysis", "ordinaryTool"]
    assert results[id(calls[0])] == (
        {"result_id": "saved-result", "artifacts": [{"filename": "summary.json"}]},
        {"content-type": "application/json"},
    )
    assert results[id(calls[1])] == (
        {"status": "ok"},
        {"content-type": "application/json"},
    )
    assert gate.clarification_message is None
    assert gate.terminal_message is None
    assert gate.unexecuted_calls == []


def test_v0113_successful_clarification_stops_same_batch() -> None:
    observability = _load_module(
        "evaluation_observability_native_batch_clarification_test", OBSERVABILITY_PATH
    )
    executed: list[str] = []

    async def clarification():
        executed.append("requestClarification")
        # execute_tool_server success: (response_data, response_headers).
        return (
            {
                "status": "awaiting_clarification",
                "question": "要採用哪一種落點定義？",
                "options": ["標記落點", "擊球方位"],
            },
            {"content-type": "application/json"},
        )

    async def later_tool():
        executed.append("laterTool")
        return ({"ok": True}, {"content-type": "application/json"})

    calls = [
        {"id": "call-clarify", "function": {"name": "requestClarification"}},
        {"id": "call-later", "function": {"name": "laterTool"}},
    ]
    results, gate = asyncio.run(
        _run_v0113_tool_batch(
            observability,
            calls,
            {"requestClarification": clarification, "laterTool": later_tool},
        )
    )

    assert executed == ["requestClarification"]
    assert results[id(calls[1])] is None
    assert gate.terminal_message is None
    assert gate.clarification_message is not None
    output = [
        {
            "type": "function_call",
            "id": f"fc-{call['id']}",
            "call_id": call["id"],
            "name": call["function"]["name"],
            "status": "in_progress",
        }
        for call in calls
    ]
    output.append(
        {
            "type": "function_call_output",
            "id": "fco-clarify",
            "call_id": calls[0]["id"],
            "status": "completed",
            "output": [{"type": "input_text", "text": "澄清已保存"}],
        }
    )
    identifiers = iter(("msg-clarification", "fco-later"))
    finished = observability.finish_successful_request_clarification(
        output,
        gate.clarification_message,
        [gate.unexecuted_calls],
        message_id_factory=lambda: next(identifiers),
        result_id_factory=lambda: next(identifiers),
    )
    continuation_calls = 0
    final_events = []
    if finished:
        final_events.append({"type": "chat:completion", "data": {"output": output}})
    else:
        continuation_calls += 1
    final_events.append(
        {"type": "chat:completion", "data": {"done": True, "output": output}}
    )
    assert continuation_calls == 0
    assert final_events[-1]["data"]["done"] is True
    assert any(
        item.get("type") == "message"
        and "要採用哪一種落點定義？" in item["content"][0]["text"]
        for item in output
    )
    assert any(
        item.get("type") == "function_call_output"
        and item.get("call_id") == "call-later"
        and item.get("status") == "failed"
        for item in output
    )
    assert next(
        item
        for item in output
        if item.get("type") == "function_call_output"
        and item.get("call_id") == "call-later"
    )["evaluation_event"] == {
        "type": "tool_not_executed",
        "reason": "clarification",
    }


def test_v0113_nonterminal_batch_keeps_native_delegate_execution() -> None:
    observability = _load_module(
        "evaluation_observability_native_batch_nonterminal_test", OBSERVABILITY_PATH
    )
    executed: list[str] = []

    def handler(name: str):
        async def run():
            executed.append(name)
            return {"status": "ok"}

        return run

    calls = [
        {"id": "call-delegate", "function": {"name": "delegate_task"}},
        {"id": "call-first", "function": {"name": "firstTool"}},
        {"id": "call-second", "function": {"name": "secondTool"}},
    ]
    results, gate = asyncio.run(
        _run_v0113_tool_batch(
            observability,
            calls,
            {
                "delegate_task": handler("delegate_task"),
                "firstTool": handler("firstTool"),
                "secondTool": handler("secondTool"),
            },
        )
    )

    assert executed == ["firstTool", "secondTool", "delegate_task"]
    assert all(results[id(call)] == {"status": "ok"} for call in calls)
    assert gate.unexecuted_calls == []
    assert gate.terminal_message is None
    assert gate.clarification_message is None


def test_clarification_success_requires_structured_success_payload() -> None:
    observability = _load_module(
        "evaluation_observability_clarification_test", OBSERVABILITY_PATH
    )

    assert observability.is_successful_request_clarification_result(
        "requestClarification",
        '{"status":"awaiting_clarification","question":"站位如何分區？","options":["前場","後場"]}',
    )
    assert not observability.is_successful_request_clarification_result(
        "requestClarification", '{"error":"工具失敗"}'
    )
    assert not observability.is_successful_request_clarification_result(
        "runPythonAnalysis",
        '{"status":"awaiting_clarification","question":"有效嗎？","options":[]}',
    )
    assert not observability.is_successful_request_clarification_result(
        "requestClarification",
        '{"status":"awaiting_clarification","question":" ","options":[]}',
    )
    assert not observability.is_successful_request_clarification_result(
        "requestClarification",
        [
            {"status": "awaiting_clarification", "question": "看似有效", "options": []},
            {"content-type": "application/json"},
        ],
    )
    assert (
        observability.terminal_tool_completion_message(
            "runPythonAnalysis",
            [
                {"error": 'HTTP error 429: {"code":"analysis_retry_limit"}'},
                None,
            ],
        )
        is None
    )


def test_successful_clarification_is_visible_terminal_content_without_continuation() -> (
    None
):
    observability = _load_module(
        "evaluation_observability_clarification_behavior_test", OBSERVABILITY_PATH
    )
    outputs = [
        '{"status":"awaiting_clarification","question":"站位分區如何定義？",'
        '"options":["前場","中場","後場"]}',
        '{"status":"awaiting_clarification","question":"第二個澄清？",'
        '"options":["選項甲"]}',
    ]
    clarification_message = None
    for result in outputs:
        text = observability.format_request_clarification_message(
            "requestClarification", result
        )
        if text is not None and clarification_message is None:
            clarification_message = text

    output = [
        {
            "type": "message",
            "id": "msg-existing",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "既有正文"}],
        },
        {
            "type": "function_call",
            "id": "fc-clarify",
            "call_id": "fc-clarify",
            "name": "requestClarification",
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "id": "fco-clarify",
            "call_id": "fc-clarify",
            "output": [{"type": "input_text", "text": outputs[0]}],
            "status": "completed",
        },
    ]
    pending_batches = [
        [
            {
                "id": "fc-not-run",
                "function": {
                    "name": "runPythonAnalysis",
                    "arguments": "{}",
                },
            }
        ]
    ]
    identifiers = iter(("msg-clarification", "fco-not-run"))
    finished = observability.finish_successful_request_clarification(
        output,
        clarification_message,
        pending_batches,
        message_id_factory=lambda: next(identifiers),
        result_id_factory=lambda: next(identifiers),
    )

    continuation_calls = 0
    events = []

    async def finish_native_turn() -> None:
        if finished:
            events.append({"type": "chat:completion", "data": {"output": output}})
        else:
            nonlocal continuation_calls
            continuation_calls += 1
        for item in output:
            if item.get("status") == "in_progress":
                item["status"] = "completed"
        events.append(
            {
                "type": "chat:completion",
                "data": {
                    "done": True,
                    "output": output,
                    "usage": {"total_tokens": 17},
                },
            }
        )

    asyncio.run(finish_native_turn())
    assistant_messages = [
        item
        for item in output
        if item.get("type") == "message" and item.get("role") == "assistant"
    ]
    clarification_output = assistant_messages[1]
    visible_text = clarification_output["content"][0]["text"]

    assert finished is True
    assert pending_batches == []
    assert continuation_calls == 0
    assert len(assistant_messages) == 2
    assert assistant_messages[0]["content"][0]["text"] == "既有正文"
    assert "站位分區如何定義？" in visible_text
    assert "1. 前場\n2. 中場\n3. 後場" in visible_text
    assert "第二個澄清？" not in visible_text
    assert any(
        item.get("type") == "message"
        and item.get("content", [{}])[0].get("text") == visible_text
        for item in events[0]["data"]["output"]
    )
    assert events[-1]["type"] == "chat:completion"
    assert events[-1]["data"]["done"] is True
    assert events[-1]["data"]["usage"] == {"total_tokens": 17}
    assert any(
        item.get("type") == "function_call_output"
        and item.get("call_id") == "fc-not-run"
        and item.get("status") == "failed"
        for item in output
    )


def test_unexecuted_calls_get_failed_output_without_changing_completed_results() -> (
    None
):
    observability = _load_module(
        "evaluation_observability_terminal_tools_test", OBSERVABILITY_PATH
    )
    output = [
        {
            "type": "function_call",
            "id": "call-done",
            "call_id": "call-done",
            "name": "runPythonAnalysis",
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "id": "fco-done",
            "call_id": "call-done",
            "status": "completed",
            "output": [{"type": "input_text", "text": "ok"}],
        },
        {
            "type": "function_call",
            "id": "call-never-run",
            "call_id": "call-never-run",
            "name": "requestClarification",
            "arguments": "{}",
            "status": "completed",
        },
    ]
    ids = iter(("fco-failed",))

    terminalized = observability.terminalize_dangling_function_calls(
        output,
        reason="工具呼叫未執行。",
        id_factory=lambda: next(ids),
    )

    assert terminalized == 1
    assert output[0]["status"] == "completed"
    assert output[1]["status"] == "completed"
    assert output[2]["status"] == "failed"
    assert output[3] == {
        "type": "function_call_output",
        "id": "fco-failed",
        "call_id": "call-never-run",
        "output": [{"type": "input_text", "text": "工具呼叫未執行。"}],
        "status": "failed",
        "evaluation_event": {"type": "tool_not_executed", "reason": "stopped"},
    }


def test_queued_tool_batch_is_recorded_failed_when_not_executed() -> None:
    observability = _load_module(
        "evaluation_observability_pending_tools_test", OBSERVABILITY_PATH
    )
    output = []
    ids = iter(("fco-never",))

    terminalized = observability.terminalize_pending_tool_calls(
        output,
        [
            [
                {
                    "id": "call-never",
                    "function": {
                        "name": "requestClarification",
                        "arguments": '{"question":"需要補答？","options":[]}',
                    },
                }
            ]
        ],
        reason="澄清已提交；此呼叫未執行。",
        id_factory=lambda: next(ids),
    )

    assert terminalized == 1
    assert output[0]["type"] == "function_call"
    assert output[0]["call_id"] == "call-never"
    assert output[0]["status"] == "failed"
    assert output[1]["type"] == "function_call_output"
    assert output[1]["call_id"] == "call-never"
    assert output[1]["status"] == "failed"


def test_stage_logs_and_safe_errors_do_not_include_provider_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    observability = _load_module("evaluation_observability_test", OBSERVABILITY_PATH)
    metadata = {
        "badmintonai_evaluation": True,
        "badmintonai_operation_id": "a" * 32 + ":1",
        "chat_id": "b" * 24,
        "user_message_id": "00000000-0000-4000-8000-000000000001",
        "message_id": "00000000-0000-4000-8000-000000000002",
        "task_id": "00000000-0000-4000-8000-000000000003",
    }
    secret_body = "vendor-secret-body-and-api-key"
    with caplog.at_level(logging.INFO):
        observability.log_evaluation_stage(
            logging.getLogger("evaluation-observability-test"),
            "upstream_headers",
            {**metadata, "message_id": secret_body},
            duration_ms=125,
            http_status=429,
        )
    assert secret_body not in caplog.text
    assert "operation_id=" + "a" * 32 + ":1" in caplog.text
    assert "message_id=sha256:" in caplog.text
    assert "http_status=429" in caplog.text
    assert secret_body not in observability.safe_http_error_message(429)
    assert secret_body not in observability.safe_evaluation_error(
        RuntimeError(secret_body)
    )
    assert "逾時" in observability.safe_evaluation_error(TimeoutError(secret_body))


def test_logged_stream_logs_once_for_first_chunk_and_completion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    observability = _load_module(
        "evaluation_observability_stream_test", OBSERVABILITY_PATH
    )
    metadata = {"badmintonai_evaluation": True, "badmintonai_operation_id": "stream-1"}

    async def source(_response):
        yield b"one"
        yield b"two"
        yield b"three"

    async def collect():
        return [
            chunk
            async for chunk in observability.logged_evaluation_stream(
                object(),
                metadata,
                0,
                source,
                logging.getLogger("evaluation-stream-test"),
            )
        ]

    with caplog.at_level(logging.INFO):
        assert asyncio.run(collect()) == [b"one", b"two", b"three"]
    stages = [
        record.getMessage().split("stage=", 1)[1].split()[0]
        for record in caplog.records
        if record.name == "evaluation-stream-test"
    ]
    assert stages == ["upstream_first_chunk", "upstream_stream_done"]
