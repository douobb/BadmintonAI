"""TASK-020 PNG 驗證與 Open WebUI 私有聊天附件橋接測試。"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

import pytest

from badminton_ai.server.chart_display import (
    MAX_EMBED_HTML_BYTES,
    OpenWebUIBridgeError,
    OpenWebUIChartBridge,
    OpenWebUIEventNotPersisted,
    OpenWebUIEventOutcomeUnknown,
    OpenWebUIIdentityMismatch,
    PNGArtifactError,
    decode_verified_png,
    validate_png_bytes,
)
from badminton_ai.server.plotly_rich import (
    render_plotly_charts_html,
    validate_plotly_charts,
)


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"".join(
    (
        _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)),
        _png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\xff")),
        _png_chunk(b"IEND", b""),
    )
)
API_USER_ID = "admin-user"
CHAT_ID = "chat-123"
MESSAGE_ID = "message-456"
USER_MESSAGE_ID = "user-message-123"
FILE_ID = "2baefb3d-2363-4e2f-9fc4-8f55c12fae44"


class _Response:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.body = body
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]


class _FakeWebUI:
    def __init__(
        self,
        *,
        event_result: bool = True,
        event_status: int = 200,
        event_error_after_persist: Exception | None = None,
        api_user_id: str = API_USER_ID,
        chat_user_id: str | None = None,
        chat_status: int = 200,
        chat_response_override: Any | None = None,
        initial_message_exists: bool = True,
        pending_assistant_child: bool = False,
        persist_embed_event: bool = True,
    ):
        self.event_result = event_result
        self.event_status = event_status
        self.event_error_after_persist = event_error_after_persist
        self.api_user_id = api_user_id
        self.chat_user_id = chat_user_id or api_user_id
        self.chat_status = chat_status
        self.chat_response_override = chat_response_override
        self.initial_message_exists = initial_message_exists
        self.pending_assistant_child = pending_assistant_child
        self.persist_embed_event = persist_embed_event
        self.requests: list[Any] = []
        self.persisted_files: dict[tuple[str, str], list[dict[str, str]]] = {}
        self.persisted_embeds: dict[tuple[str, str], list[str]] = {}
        self.message_nodes: dict[str, dict[str, dict[str, Any]]] = {}
        self.current_ids: dict[str, str] = {}

    def urlopen(self, request: Any, *, timeout: float) -> _Response:
        self.requests.append(request)
        path = urlsplit(request.full_url).path
        if request.method == "GET" and path == "/api/v1/auths/":
            body = json.dumps({"id": self.api_user_id, "role": "admin"}).encode()
        elif request.method == "GET" and path.startswith("/api/v1/chats/"):
            if self.chat_status != 200:
                return _Response(b"{}", status=self.chat_status)
            if self.chat_response_override is not None:
                body = json.dumps(self.chat_response_override).encode()
            else:
                chat_id = path.rsplit("/", 1)[-1]
                messages = self._chat_nodes(chat_id)
                for (stored_chat, message_id), embeds in self.persisted_embeds.items():
                    if stored_chat == chat_id:
                        messages.setdefault(message_id, {"role": "assistant"})[
                            "embeds"
                        ] = embeds
                body = json.dumps(
                    {
                        "id": chat_id,
                        "user_id": self.chat_user_id,
                        "chat": {
                            "history": {
                                "messages": messages,
                                "currentId": self.current_ids[chat_id],
                            }
                        },
                    }
                ).encode()
        elif path == "/api/v1/files/":
            body = json.dumps({"id": FILE_ID}).encode()
        elif path.endswith("/event"):
            if self.event_status != 200:
                return _Response(b"{}", status=self.event_status)
            parts = path.split("/")
            event = json.loads(request.data)
            persist_event = (
                self.persist_embed_event
                if event["type"] == "embeds"
                else self.event_result
            )
            if persist_event:
                target = (parts[4], parts[6])
                if event["type"] == "files":
                    self.persisted_files[target] = event["data"]["files"]
                elif event["type"] == "embeds":
                    embeds = event["data"]["embeds"]
                    nodes = self._chat_nodes(target[0])
                    is_new_message = target[1] not in nodes
                    if is_new_message:
                        parent_id = next(
                            (
                                existing_id
                                for existing_id, existing_message in nodes.items()
                                if target[1] in existing_message.get("childrenIds", [])
                            ),
                            None,
                        )
                        node = {
                            "id": target[1],
                            "role": "assistant",
                            "parentId": parent_id,
                            "childrenIds": [],
                        }
                        nodes[target[1]] = node
                        parent = nodes.get(parent_id) if parent_id else None
                        if isinstance(parent, dict) and target[1] not in parent.get(
                            "childrenIds", []
                        ):
                            parent.setdefault("childrenIds", []).append(target[1])
                        self.current_ids[target[0]] = target[1]
                    else:
                        node = nodes[target[1]]
                    if event["data"].get("replace"):
                        self.persisted_embeds[target] = embeds
                    else:
                        self.persisted_embeds[target] = (
                            embeds + self.persisted_embeds.get(target, [])
                        )
                    node["embeds"] = self.persisted_embeds[target]
                else:
                    raise AssertionError(f"Unexpected event type: {event['type']}")
            if self.event_error_after_persist is not None:
                raise self.event_error_after_persist
            body = json.dumps(self.event_result).encode()
        elif request.method == "DELETE":
            body = b"{}"
        else:
            raise AssertionError(
                f"Unexpected Open WebUI request: {request.method} {path}"
            )
        return _Response(body)

    def _chat_nodes(self, chat_id: str) -> dict[str, dict[str, Any]]:
        """建立 v0.11.3 測試用聊天樹與目前節點。"""

        if chat_id not in self.message_nodes:
            nodes = {
                USER_MESSAGE_ID: {
                    "id": USER_MESSAGE_ID,
                    "role": "user",
                    "parentId": None,
                    "childrenIds": (
                        [MESSAGE_ID]
                        if self.initial_message_exists or self.pending_assistant_child
                        else []
                    ),
                }
            }
            if self.initial_message_exists:
                nodes[MESSAGE_ID] = {
                    "id": MESSAGE_ID,
                    "role": "assistant",
                    "parentId": USER_MESSAGE_ID,
                    "childrenIds": [],
                }
                self.current_ids[chat_id] = MESSAGE_ID
            else:
                self.current_ids[chat_id] = USER_MESSAGE_ID
            self.message_nodes[chat_id] = nodes
        return self.message_nodes[chat_id]

    def upsert_final_assistant(
        self, *, chat_id: str, message_id: str, parent_id: str, content: str
    ) -> None:
        """模擬 v0.11.3 最終 assistant upsert 對既有節點做欄位合併。"""

        nodes = self._chat_nodes(chat_id)
        existing = nodes.get(message_id, {})
        nodes[message_id] = {
            **existing,
            "id": message_id,
            "role": "assistant",
            "parentId": parent_id,
            "content": content,
        }
        parent = nodes[parent_id]
        if message_id not in parent["childrenIds"]:
            parent["childrenIds"].append(message_id)


def _bridge(fake: _FakeWebUI | None = None) -> OpenWebUIChartBridge:
    server = fake or _FakeWebUI()
    return OpenWebUIChartBridge(
        base_url="http://open-webui:8080",
        api_key="secret-test-key",
        urlopen=server.urlopen,
    )


def _embed_html(fingerprint: str, marker: str = "chart") -> str:
    return (
        '<!doctype html><html><head><meta name="badmintonai-chart-fingerprint" '
        f'content="sha256:{fingerprint}"></head><body>{marker}</body></html>'
    )


def test_decode_verified_png_requires_matching_metadata_size_and_real_png() -> None:
    assert (
        decode_verified_png(
            content_base64=base64.b64encode(VALID_PNG).decode("ascii"),
            size_bytes=len(VALID_PNG),
            extension=".png",
            mime_type="image/png",
            kind="chart",
        )
        == VALID_PNG
    )

    with pytest.raises(PNGArtifactError):
        decode_verified_png(
            content_base64="not base64!",
            size_bytes=len(VALID_PNG),
            extension=".png",
            mime_type="image/png",
            kind="chart",
        )
    with pytest.raises(PNGArtifactError):
        decode_verified_png(
            content_base64=base64.b64encode(VALID_PNG).decode("ascii"),
            size_bytes=len(VALID_PNG) + 1,
            extension=".png",
            mime_type="image/png",
            kind="chart",
        )
    with pytest.raises(PNGArtifactError):
        decode_verified_png(
            content_base64=base64.b64encode(b"not an image").decode("ascii"),
            size_bytes=len(b"not an image"),
            extension=".png",
            mime_type="image/png",
            kind="chart",
        )
    with pytest.raises(PNGArtifactError):
        decode_verified_png(
            content_base64=base64.b64encode(VALID_PNG).decode("ascii"),
            size_bytes=len(VALID_PNG),
            extension=".png",
            mime_type="image/jpeg",
            kind="chart",
        )


def test_png_chunk_crc_and_compressed_data_are_checked() -> None:
    corrupt = bytearray(VALID_PNG)
    corrupt[-8] ^= 0x01

    with pytest.raises(PNGArtifactError):
        validate_png_bytes(bytes(corrupt))


def test_bridge_uploads_private_file_and_persists_files_event() -> None:
    fake = _FakeWebUI()
    bridge = _bridge(fake)

    result = bridge.attach_pngs(
        [(VALID_PNG, "landing_heatmap.png")],
        chat_id=CHAT_ID,
        message_id=MESSAGE_ID,
        user_id=API_USER_ID,
    )

    assert result[0].file_id == FILE_ID
    assert result[0].url == f"/api/v1/files/{FILE_ID}/content"
    assert [request.method for request in fake.requests] == ["GET", "POST", "POST"]
    assert all(
        request.headers["Authorization"] == "Bearer secret-test-key"
        for request in fake.requests
    )
    upload = fake.requests[1]
    assert 'filename="badminton-chart-01.png"' in upload.data.decode(
        "ascii", errors="ignore"
    )
    assert VALID_PNG in upload.data
    event = fake.requests[2]
    assert urlsplit(event.full_url).path.endswith(
        f"/chats/{CHAT_ID}/messages/{MESSAGE_ID}/event"
    )
    assert json.loads(event.data) == {
        "type": "files",
        "data": {
            "files": [
                {
                    "type": "image",
                    "id": FILE_ID,
                    "url": f"/api/v1/files/{FILE_ID}/content",
                    "name": "badminton-chart-01.png",
                    "content_type": "image/png",
                }
            ]
        },
    }
    assert base64.b64encode(VALID_PNG) not in event.data


def test_persisted_file_event_is_available_after_bridge_recreation() -> None:
    fake = _FakeWebUI()
    first_bridge = _bridge(fake)

    uploaded = first_bridge.attach_pngs(
        [(VALID_PNG, "landing_heatmap.png")],
        chat_id=CHAT_ID,
        message_id=MESSAGE_ID,
        user_id=API_USER_ID,
    )

    reloaded_bridge = _bridge(fake)
    assert reloaded_bridge.configured
    persisted_files = fake.persisted_files[(CHAT_ID, MESSAGE_ID)]
    assert len(uploaded) == 1
    assert persisted_files == [uploaded[0].as_file_object()]
    assert persisted_files[0]["url"] == f"/api/v1/files/{FILE_ID}/content"


def test_bridge_refuses_identity_mismatch_before_uploading() -> None:
    fake = _FakeWebUI(api_user_id="other-user")

    with pytest.raises(OpenWebUIIdentityMismatch):
        _bridge(fake).attach_pngs(
            [(VALID_PNG, "chart.png")],
            chat_id=CHAT_ID,
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )

    assert [request.method for request in fake.requests] == ["GET"]


def test_bridge_rejects_invalid_png_before_any_open_webui_call() -> None:
    fake = _FakeWebUI()
    bridge = _bridge(fake)

    with pytest.raises(PNGArtifactError):
        bridge.attach_pngs(
            [(b"not a png", "chart.png")],
            chat_id=CHAT_ID,
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )

    assert fake.requests == []


def test_bridge_keeps_file_when_event_returns_false_because_result_is_ambiguous() -> (
    None
):
    fake = _FakeWebUI(event_result=False)

    with pytest.raises(OpenWebUIEventOutcomeUnknown):
        _bridge(fake).attach_pngs(
            [(VALID_PNG, "chart.png")],
            chat_id=CHAT_ID,
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )

    assert [request.method for request in fake.requests] == [
        "GET",
        "POST",
        "POST",
    ]
    assert fake.persisted_files == {}


def test_bridge_cleans_up_when_event_is_definitively_rejected_before_persistence() -> (
    None
):
    fake = _FakeWebUI(event_status=401)

    with pytest.raises(OpenWebUIEventNotPersisted):
        _bridge(fake).attach_pngs(
            [(VALID_PNG, "chart.png")],
            chat_id=CHAT_ID,
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )

    assert [request.method for request in fake.requests] == [
        "GET",
        "POST",
        "POST",
        "DELETE",
    ]
    assert fake.requests[-1].full_url.endswith(f"/api/v1/files/{FILE_ID}")


def test_bridge_never_deletes_file_when_event_commit_succeeds_but_response_is_lost() -> (
    None
):
    fake = _FakeWebUI(event_error_after_persist=TimeoutError("response lost"))

    with pytest.raises(OpenWebUIEventOutcomeUnknown):
        _bridge(fake).attach_pngs(
            [(VALID_PNG, "chart.png")],
            chat_id=CHAT_ID,
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )

    assert [request.method for request in fake.requests] == ["GET", "POST", "POST"]
    assert fake.persisted_files[(CHAT_ID, MESSAGE_ID)] == [
        {
            "type": "image",
            "id": FILE_ID,
            "url": f"/api/v1/files/{FILE_ID}/content",
            "name": "badminton-chart-01.png",
            "content_type": "image/png",
        }
    ]


def test_bridge_emits_persistent_appending_embeds_event() -> None:
    fake = _FakeWebUI()
    bridge = _bridge(fake)
    html = _embed_html("a" * 64)

    result = bridge.emit_chart_embed(
        html,
        chat_id=CHAT_ID,
        message_id=MESSAGE_ID,
        user_id=API_USER_ID,
    )

    assert result == "embedded"
    assert [request.method for request in fake.requests] == [
        "GET",
        "GET",
        "POST",
        "GET",
    ]
    assert urlsplit(fake.requests[1].full_url).path == f"/api/v1/chats/{CHAT_ID}"
    event_request = fake.requests[-2]
    assert urlsplit(event_request.full_url).path.endswith(
        f"/chats/{CHAT_ID}/messages/{MESSAGE_ID}/event"
    )
    assert json.loads(event_request.data) == {
        "type": "embeds",
        "data": {"embeds": [html], "replace": False},
    }
    assert fake.persisted_embeds[(CHAT_ID, MESSAGE_ID)] == [html]


def test_linked_pending_assistant_is_created_then_final_upsert_preserves_chat_tree() -> (
    None
):
    fake = _FakeWebUI(
        initial_message_exists=False,
        pending_assistant_child=True,
    )
    html = _embed_html("9" * 64)

    result = _bridge(fake).emit_chart_embed(
        html,
        chat_id=CHAT_ID,
        message_id=MESSAGE_ID,
        user_id=API_USER_ID,
    )

    assert result == "embedded"
    pending_node = fake.message_nodes[CHAT_ID][MESSAGE_ID]
    assert pending_node["parentId"] == USER_MESSAGE_ID
    assert pending_node["childrenIds"] == []
    assert pending_node["embeds"] == [html]
    assert fake.current_ids[CHAT_ID] == MESSAGE_ID

    fake.upsert_final_assistant(
        chat_id=CHAT_ID,
        message_id=MESSAGE_ID,
        parent_id=USER_MESSAGE_ID,
        content="已完成圖表分析",
    )

    final_node = fake.message_nodes[CHAT_ID][MESSAGE_ID]
    assert final_node["parentId"] == USER_MESSAGE_ID
    assert final_node["embeds"] == [html]
    assert final_node["content"] == "已完成圖表分析"
    assert fake.message_nodes[CHAT_ID][USER_MESSAGE_ID]["childrenIds"] == [MESSAGE_ID]
    assert fake.current_ids[CHAT_ID] == MESSAGE_ID

    assert (
        _bridge(fake).emit_chart_embed(
            html,
            chat_id=CHAT_ID,
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )
        == "duplicate_suppressed"
    )
    assert [request.method for request in fake.requests].count("POST") == 1


def test_missing_assistant_without_user_child_link_is_rejected_before_event() -> None:
    fake = _FakeWebUI(initial_message_exists=False)

    with pytest.raises(OpenWebUIEventNotPersisted):
        _bridge(fake).emit_chart_embed(
            _embed_html("c" * 64),
            chat_id=CHAT_ID,
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )

    assert [request.method for request in fake.requests] == ["GET", "GET"]
    assert fake.persisted_embeds == {}
    assert set(fake.message_nodes[CHAT_ID]) == {USER_MESSAGE_ID}


def test_existing_assistant_without_valid_user_parent_is_rejected_before_event() -> (
    None
):
    fake = _FakeWebUI()
    nodes = fake._chat_nodes(CHAT_ID)
    nodes[MESSAGE_ID]["parentId"] = None
    nodes[USER_MESSAGE_ID]["childrenIds"] = []

    with pytest.raises(OpenWebUIEventNotPersisted):
        _bridge(fake).emit_chart_embed(
            _embed_html("d" * 64),
            chat_id=CHAT_ID,
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )

    assert [request.method for request in fake.requests] == ["GET", "GET"]
    assert fake.persisted_embeds == {}


def test_embed_event_true_response_is_verified_before_reporting_success() -> None:
    fake = _FakeWebUI(persist_embed_event=False)
    html = _embed_html("a" * 64)

    with pytest.raises(OpenWebUIEventOutcomeUnknown):
        _bridge(fake).emit_chart_embed(
            html,
            chat_id=CHAT_ID,
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )

    assert [request.method for request in fake.requests] == [
        "GET",
        "GET",
        "POST",
        "GET",
    ]
    assert fake.persisted_embeds == {}
    assert [request.method for request in fake.requests].count("POST") == 1


def test_embed_event_that_commits_before_false_response_is_not_reposted() -> None:
    fake = _FakeWebUI(event_result=False)
    html = _embed_html("b" * 64)

    assert (
        _bridge(fake).emit_chart_embed(
            html,
            chat_id=CHAT_ID,
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )
        == "embedded"
    )
    assert fake.persisted_embeds[(CHAT_ID, MESSAGE_ID)] == [html]
    assert [request.method for request in fake.requests].count("POST") == 1


def test_plotly_figure_data_persists_and_duplicate_is_reused_after_reload() -> None:
    fake = _FakeWebUI()
    chart = validate_plotly_charts(
        {
            "schema_version": "badminton-plotly/v1",
            "charts": [
                {
                    "title": "互動球路圖",
                    "figure": {
                        "data": [{"type": "bar", "x": ["發短球"], "y": [2]}],
                        "layout": {},
                    },
                }
            ],
        }
    )
    html = render_plotly_charts_html(chart)
    first = _bridge(fake).emit_chart_embed(
        html,
        chat_id=CHAT_ID,
        message_id=MESSAGE_ID,
        user_id=API_USER_ID,
    )

    # 新 bridge 代表重載後的再次 Tool 呼叫；payload 已在持久化 embeds 內。
    persisted = fake.persisted_embeds[(CHAT_ID, MESSAGE_ID)]
    assert first == "embedded"
    assert len(persisted) == 1
    assert "互動球路圖" in persisted[0]
    assert '"y":[2]' in persisted[0]
    assert "plotly-6.6.0.min.js" in persisted[0]
    assert (
        _bridge(fake).emit_chart_embed(
            html,
            chat_id=CHAT_ID,
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )
        == "duplicate_suppressed"
    )
    assert fake.persisted_embeds[(CHAT_ID, MESSAGE_ID)] == persisted
    assert all(
        request.headers["Authorization"] == "Bearer secret-test-key"
        for request in fake.requests
    )


def test_persisted_embed_is_available_after_bridge_recreation() -> None:
    fake = _FakeWebUI()
    html = _embed_html("b" * 64, "fixed-template")
    _bridge(fake).emit_chart_embed(
        html,
        chat_id=CHAT_ID,
        message_id=MESSAGE_ID,
        user_id=API_USER_ID,
    )

    assert _bridge(fake).configured
    assert fake.persisted_embeds[(CHAT_ID, MESSAGE_ID)] == [html]


def test_multiple_embeds_on_same_message_are_preserved_after_reload() -> None:
    fake = _FakeWebUI()
    first_html = _embed_html("c" * 64, "first chart")
    second_html = _embed_html("d" * 64, "second chart")
    bridge = _bridge(fake)

    bridge.emit_chart_embed(
        first_html,
        chat_id=CHAT_ID,
        message_id=MESSAGE_ID,
        user_id=API_USER_ID,
    )
    bridge.emit_chart_embed(
        second_html,
        chat_id=CHAT_ID,
        message_id=MESSAGE_ID,
        user_id=API_USER_ID,
    )

    # v0.11.3 merges a new embeds list before the previously persisted list.
    reloaded_embeds = fake.persisted_embeds[(CHAT_ID, MESSAGE_ID)]
    assert reloaded_embeds == [second_html, first_html]
    assert _bridge(fake).configured
    assert fake.persisted_embeds[(CHAT_ID, MESSAGE_ID)] == reloaded_embeds


def test_same_semantic_chart_is_suppressed_without_replacing_other_embeds() -> None:
    html = _embed_html("e" * 64, "same chart")
    other_source = "<iframe src='other-source'></iframe>"
    previous_chart = _embed_html("f" * 64, "different chart")
    fake = _FakeWebUI()
    fake.persisted_embeds[(CHAT_ID, MESSAGE_ID)] = [other_source, previous_chart, html]

    result = _bridge(fake).emit_chart_embed(
        html,
        chat_id=CHAT_ID,
        message_id=MESSAGE_ID,
        user_id=API_USER_ID,
    )

    assert result == "duplicate_suppressed"
    assert [request.method for request in fake.requests] == ["GET", "GET"]
    assert fake.persisted_embeds[(CHAT_ID, MESSAGE_ID)] == [
        other_source,
        previous_chart,
        html,
    ]


def test_same_chart_retry_after_bridge_reload_is_suppressed() -> None:
    html = _embed_html("8" * 64, "persisted chart")
    fake = _FakeWebUI()

    assert (
        _bridge(fake).emit_chart_embed(
            html,
            chat_id=CHAT_ID,
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )
        == "embedded"
    )
    assert (
        _bridge(fake).emit_chart_embed(
            html,
            chat_id=CHAT_ID,
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )
        == "duplicate_suppressed"
    )

    assert fake.persisted_embeds[(CHAT_ID, MESSAGE_ID)] == [html]


def test_different_semantic_chart_appends_and_preserves_existing_sources() -> None:
    previous_chart = _embed_html("1" * 64, "old grouped bar")
    other_source = "<iframe src='other-source'></iframe>"
    new_chart = _embed_html("2" * 64, "corrected grouped bar")
    fake = _FakeWebUI()
    fake.persisted_embeds[(CHAT_ID, MESSAGE_ID)] = [other_source, previous_chart]

    result = _bridge(fake).emit_chart_embed(
        new_chart,
        chat_id=CHAT_ID,
        message_id=MESSAGE_ID,
        user_id=API_USER_ID,
    )

    assert result == "embedded"
    assert fake.persisted_embeds[(CHAT_ID, MESSAGE_ID)] == [
        new_chart,
        other_source,
        previous_chart,
    ]


def test_same_process_concurrent_retries_only_append_once() -> None:
    html = _embed_html("7" * 64, "concurrent retry")
    fake = _FakeWebUI()

    def emit(_index: int) -> str:
        return _bridge(fake).emit_chart_embed(
            html,
            chat_id=CHAT_ID,
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(emit, range(8)))

    assert results.count("embedded") == 1
    assert results.count("duplicate_suppressed") == 7
    assert fake.persisted_embeds[(CHAT_ID, MESSAGE_ID)] == [html]


def test_embed_dedup_fails_closed_on_unavailable_chat_or_wrong_owner() -> None:
    html = _embed_html("3" * 64)
    unavailable = _FakeWebUI(chat_status=503)
    with pytest.raises(OpenWebUIEventOutcomeUnknown):
        _bridge(unavailable).emit_chart_embed(
            html,
            chat_id=CHAT_ID,
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )
    assert [request.method for request in unavailable.requests] == ["GET", "GET"]
    assert unavailable.persisted_embeds == {}

    wrong_owner = _FakeWebUI(chat_user_id="somebody-else")
    with pytest.raises(OpenWebUIIdentityMismatch):
        _bridge(wrong_owner).emit_chart_embed(
            html,
            chat_id=CHAT_ID,
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )
    assert [request.method for request in wrong_owner.requests] == ["GET", "GET"]
    assert wrong_owner.persisted_embeds == {}


def test_bridge_refuses_embed_for_wrong_api_key_owner_or_oversized_html() -> None:
    fake = _FakeWebUI(api_user_id="other-user")
    bridge = _bridge(fake)
    with pytest.raises(OpenWebUIIdentityMismatch):
        bridge.emit_chart_embed(
            _embed_html("4" * 64),
            chat_id=CHAT_ID,
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )
    assert [request.method for request in fake.requests] == ["GET"]

    fake = _FakeWebUI()
    with pytest.raises(OpenWebUIBridgeError):
        _bridge(fake).emit_chart_embed(
            _embed_html("4" * 64) + "x" * (MAX_EMBED_HTML_BYTES + 1),
            chat_id=CHAT_ID,
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )
    assert fake.requests == []


def test_bridge_rejects_malformed_chat_identifiers_without_http_calls() -> None:
    fake = _FakeWebUI()

    with pytest.raises(OpenWebUIBridgeError):
        _bridge(fake).attach_pngs(
            [(VALID_PNG, "chart.png")],
            chat_id="../../other-user",
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )

    assert fake.requests == []


def test_file_response_id_must_be_a_uuid() -> None:
    fake = _FakeWebUI()
    fake.urlopen = lambda request, *, timeout: _Response(
        json.dumps({"id": "../../other-user"}).encode()
        if request.method == "POST"
        else json.dumps({"id": API_USER_ID, "role": "admin"}).encode()
    )
    bridge = _bridge(fake)

    with pytest.raises(OpenWebUIBridgeError):
        bridge.attach_pngs(
            [(VALID_PNG, "chart.png")],
            chat_id=CHAT_ID,
            message_id=MESSAGE_ID,
            user_id=API_USER_ID,
        )
