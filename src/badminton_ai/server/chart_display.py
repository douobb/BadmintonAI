"""驗證分析 PNG，並以 Open WebUI 私有檔案附件呈現在原聊天訊息。"""

from __future__ import annotations

import json
import re
import struct
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from dataclasses import dataclass
from typing import Any, Callable, Sequence

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PNG_BYTES = 10 * 1024 * 1024
MAX_PNG_PIXELS = 25_000_000
MAX_PNG_DECODED_BYTES = 100 * 1024 * 1024
MAX_EMBED_HTML_BYTES = 2 * 1024 * 1024
MAX_CHAT_DEDUP_RESPONSE_BYTES = 8 * 1024 * 1024
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CHART_FINGERPRINT_MARKER = re.compile(
    r'<meta name="badmintonai-chart-fingerprint" content="sha256:([0-9a-f]{64})">'
)
_EMBED_EMIT_LOCK = threading.Lock()
_BIT_DEPTHS = {
    0: frozenset({1, 2, 4, 8, 16}),
    2: frozenset({8, 16}),
    3: frozenset({1, 2, 4, 8}),
    4: frozenset({8, 16}),
    6: frozenset({8, 16}),
}
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


class PNGArtifactError(ValueError):
    """PNG 位元組或圖片結構未通過檢查。"""


class OpenWebUIBridgeError(RuntimeError):
    """Open WebUI 上傳或訊息附件事件未成功。"""


class OpenWebUIBridgeNotConfigured(OpenWebUIBridgeError):
    """缺少 Open WebUI URL 或 API key。"""


class OpenWebUIIdentityMismatch(OpenWebUIBridgeError):
    """工具呼叫者與目前 Open WebUI API key 的使用者不一致。"""


class OpenWebUIEventNotPersisted(OpenWebUIBridgeError):
    """Open WebUI 在執行 event emitter 前明確拒絕請求。"""


class OpenWebUIEventOutcomeUnknown(OpenWebUIBridgeError):
    """Event 結果不確定；不得刪除可能已被訊息引用的檔案。"""


class _OpenWebUIHTTPError(OpenWebUIBridgeError):
    def __init__(self, status_code: int) -> None:
        super().__init__("Open WebUI 圖表附件請求失敗")
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class UploadedImage:
    """Open WebUI 檔案附件所需的公開欄位。"""

    file_id: str
    url: str
    name: str
    content_type: str = "image/png"

    def as_file_object(self) -> dict[str, str]:
        return {
            "type": "image",
            "id": self.file_id,
            "url": self.url,
            "name": self.name,
            "content_type": self.content_type,
        }


def decode_verified_png(
    *,
    content_base64: str,
    size_bytes: int,
    extension: str,
    mime_type: str,
    kind: str,
) -> bytes:
    """驗證 artifact 契約、PNG chunk/CRC 與解壓後 scanline，再回傳原始 bytes。"""

    if extension.casefold() != ".png" or mime_type != "image/png" or kind != "chart":
        raise PNGArtifactError("PNG artifact metadata 不一致")
    if not isinstance(content_base64, str):
        raise PNGArtifactError("PNG artifact base64 格式錯誤")
    try:
        import base64

        content = base64.b64decode(content_base64, validate=True)
    except (ValueError, TypeError) as exc:
        raise PNGArtifactError("PNG artifact base64 格式錯誤") from exc
    if len(content) != size_bytes:
        raise PNGArtifactError("PNG artifact 大小不一致")
    validate_png_bytes(content)
    return content


def validate_png_bytes(content: bytes) -> None:
    """以標準函式庫驗證 PNG 結構，並限制圖片尺寸與解壓資料量。"""

    if not isinstance(content, bytes) or not content.startswith(PNG_SIGNATURE):
        raise PNGArtifactError("PNG signature 無效")
    if len(content) > MAX_PNG_BYTES or len(content) < 8 + 25:
        raise PNGArtifactError("PNG 大小超出可接受範圍")

    offset = len(PNG_SIGNATURE)
    width = height = bit_depth = color_type = 0
    saw_ihdr = saw_plte = saw_idat = saw_iend = False
    idat_ended = False
    idat_chunks: list[bytes] = []

    while offset < len(content):
        if len(content) - offset < 12:
            raise PNGArtifactError("PNG chunk 不完整")
        chunk_length = struct.unpack_from(">I", content, offset)[0]
        chunk_type_start = offset + 4
        chunk_type = content[chunk_type_start : chunk_type_start + 4]
        data_start = chunk_type_start + 4
        data_end = data_start + chunk_length
        chunk_end = data_end + 4
        if chunk_end > len(content) or not all(
            (65 <= item <= 90) or (97 <= item <= 122) for item in chunk_type
        ):
            raise PNGArtifactError("PNG chunk 長度或名稱無效")
        chunk_data = content[data_start:data_end]
        stored_crc = struct.unpack_from(">I", content, data_end)[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
        if stored_crc != actual_crc:
            raise PNGArtifactError("PNG chunk checksum 無效")

        if not saw_ihdr:
            if chunk_type != b"IHDR" or chunk_length != 13:
                raise PNGArtifactError("PNG 缺少有效 IHDR")
            (
                width,
                height,
                bit_depth,
                color_type,
                compression_method,
                filter_method,
                interlace_method,
            ) = struct.unpack(">IIBBBBB", chunk_data)
            if (
                width < 1
                or height < 1
                or width * height > MAX_PNG_PIXELS
                or color_type not in _BIT_DEPTHS
                or bit_depth not in _BIT_DEPTHS[color_type]
                or compression_method != 0
                or filter_method != 0
                or interlace_method != 0
            ):
                raise PNGArtifactError("PNG IHDR 欄位不受支援")
            saw_ihdr = True
        elif chunk_type == b"IHDR":
            raise PNGArtifactError("PNG 不可包含重複 IHDR")

        if chunk_type == b"PLTE":
            if saw_idat or saw_plte or not (3 <= chunk_length <= 768):
                raise PNGArtifactError("PNG PLTE 無效")
            if chunk_length % 3:
                raise PNGArtifactError("PNG palette 長度無效")
            palette_entries = chunk_length // 3
            if color_type in {0, 4} or (
                color_type == 3 and palette_entries > 2**bit_depth
            ):
                raise PNGArtifactError("PNG palette 與 color type 不一致")
            saw_plte = True
        elif chunk_type == b"IDAT":
            if idat_ended:
                raise PNGArtifactError("PNG IDAT chunk 必須連續")
            saw_idat = True
            idat_chunks.append(chunk_data)
        else:
            if saw_idat:
                idat_ended = True
            if chunk_type == b"IEND":
                if chunk_length != 0 or not saw_idat:
                    raise PNGArtifactError("PNG IEND 無效")
                if chunk_end != len(content):
                    raise PNGArtifactError("PNG IEND 後不可有額外內容")
                saw_iend = True
                offset = chunk_end
                break
            # PNG 的 critical chunk 以大寫開頭；只接受本驗證器會檢查的 chunk。
            if chunk_type[0] & 0x20 == 0 and chunk_type not in {
                b"IHDR",
                b"PLTE",
                b"IDAT",
                b"IEND",
            }:
                raise PNGArtifactError("PNG 包含未支援的 critical chunk")

        offset = chunk_end

    if not saw_iend or not saw_idat or (color_type == 3 and not saw_plte):
        raise PNGArtifactError("PNG 必要 chunk 缺失")

    bits_per_pixel = _CHANNELS[color_type] * bit_depth
    row_bytes = (width * bits_per_pixel + 7) // 8
    expected_decoded_size = height * (row_bytes + 1)
    if expected_decoded_size > MAX_PNG_DECODED_BYTES:
        raise PNGArtifactError("PNG 解碼後資料量超出上限")

    decoder = zlib.decompressobj()
    try:
        decoded = decoder.decompress(b"".join(idat_chunks), expected_decoded_size + 1)
    except zlib.error as exc:
        raise PNGArtifactError("PNG image data 無法解壓") from exc
    if (
        len(decoded) != expected_decoded_size
        or not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
    ):
        raise PNGArtifactError("PNG image data 長度無效")
    if any(decoded[row * (row_bytes + 1)] > 4 for row in range(height)):
        raise PNGArtifactError("PNG scanline filter 無效")


class OpenWebUIChartBridge:
    """以持久 event 附加 Rich UI 或 PNG 至原聊天訊息。"""

    def __init__(
        self,
        *,
        base_url: str | None,
        api_key: str | None,
        timeout_seconds: float = 10.0,
        urlopen: Callable[..., Any] | None = None,
    ) -> None:
        self._base_url = _validate_base_url(base_url) if base_url else None
        self._api_key = api_key.strip() if isinstance(api_key, str) else ""
        self._timeout_seconds = timeout_seconds
        self._urlopen = urlopen or urllib.request.urlopen
        self._api_user_id: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._api_key)

    def attach_pngs(
        self,
        images: Sequence[tuple[bytes, str]],
        *,
        chat_id: str,
        message_id: str,
        user_id: str,
    ) -> tuple[UploadedImage, ...]:
        """驗證身分後上傳所有 PNG，並以可持久化的 `files` event 附加。"""

        if not self.configured:
            raise OpenWebUIBridgeNotConfigured("Open WebUI 圖表橋接未設定")
        _validate_open_webui_id(chat_id, "chat_id")
        _validate_open_webui_id(message_id, "message_id")
        _validate_open_webui_id(user_id, "user_id")
        if not images:
            return ()
        for content, _original_name in images:
            validate_png_bytes(content)

        api_user_id = self._get_api_user_id()
        if api_user_id != user_id:
            raise OpenWebUIIdentityMismatch(
                "Open WebUI API key 使用者與聊天使用者不一致"
            )

        uploaded: list[UploadedImage] = []
        try:
            for index, (content, _original_name) in enumerate(images, start=1):
                file_name = f"badminton-chart-{index:02d}.png"
                file_id = self._upload_file(content, file_name)
                uploaded.append(
                    UploadedImage(
                        file_id=file_id,
                        url=f"/api/v1/files/{file_id}/content",
                        name=file_name,
                    )
                )
        except OpenWebUIBridgeError:
            self._delete_uploaded_files(uploaded)
            raise

        try:
            self._emit_files_event(
                chat_id=chat_id,
                message_id=message_id,
                files=[item.as_file_object() for item in uploaded],
            )
        except OpenWebUIEventNotPersisted:
            self._delete_uploaded_files(uploaded)
            raise
        return tuple(uploaded)

    def emit_chart_embed(
        self,
        html_content: str,
        *,
        chat_id: str,
        message_id: str,
        user_id: str,
    ) -> str:
        """持久附加固定圖表；同程序內抑制完全相同的重試，不覆蓋舊 embeds。

        v0.11.3 的 event API 沒有 CAS/idempotency key；鎖僅序列化本程序呼叫，
        不保證與其他 Open WebUI writer 原子協調。讀取不確定時 fail closed。
        """

        if not self.configured:
            raise OpenWebUIBridgeNotConfigured("Open WebUI 圖表橋接未設定")
        _validate_open_webui_id(chat_id, "chat_id")
        _validate_open_webui_id(message_id, "message_id")
        _validate_open_webui_id(user_id, "user_id")
        if not isinstance(html_content, str) or not html_content:
            raise OpenWebUIBridgeError("Rich UI HTML 格式無效")
        if len(html_content.encode("utf-8")) > MAX_EMBED_HTML_BYTES:
            raise OpenWebUIBridgeError("Rich UI HTML 超過大小上限")
        fingerprints = _CHART_FINGERPRINT_MARKER.findall(html_content)
        if len(fingerprints) != 1:
            raise OpenWebUIBridgeError("Rich UI 缺少有效圖表指紋")
        fingerprint = fingerprints[0]

        def has_fingerprint(embeds: Sequence[str]) -> bool:
            return any(
                fingerprint in _CHART_FINGERPRINT_MARKER.findall(existing_html)
                for existing_html in embeds
            )

        if self._get_api_user_id() != user_id:
            raise OpenWebUIIdentityMismatch(
                "Open WebUI API key 使用者與聊天使用者不一致"
            )
        # Open WebUI 沒有依指紋原子 upsert；鎖只避免同一 bridge process
        # 內的 check-then-append 競態，跨 process/外部 writer 仍是 best effort。
        with _EMBED_EMIT_LOCK:
            embeds = self._get_message_embeds(
                chat_id=chat_id,
                message_id=message_id,
                user_id=user_id,
            )
            if has_fingerprint(embeds):
                return "duplicate_suppressed"

            try:
                self._emit_embeds_event(
                    chat_id=chat_id,
                    message_id=message_id,
                    html_content=html_content,
                )
            except OpenWebUIEventOutcomeUnknown as exc:
                # event route 可能在提交後遺失回應；只讀回一次確認，不重送。
                try:
                    persisted_embeds = self._get_message_embeds(
                        chat_id=chat_id,
                        message_id=message_id,
                        user_id=user_id,
                    )
                except OpenWebUIBridgeError:
                    raise exc
                if has_fingerprint(persisted_embeds):
                    return "embedded"
                raise exc

            # True 只代表 emitter 回傳；讀回確認資料庫寫入後才回報成功。
            persisted_embeds = self._get_message_embeds(
                chat_id=chat_id,
                message_id=message_id,
                user_id=user_id,
            )
            if not has_fingerprint(persisted_embeds):
                raise OpenWebUIEventOutcomeUnknown(
                    "Open WebUI 未確認 Rich UI 已寫入聊天"
                )
        return "embedded"

    def _get_message_embeds(
        self,
        *,
        chat_id: str,
        message_id: str,
        user_id: str,
    ) -> list[str]:
        """透過使用者授權的 chat GET 讀取單一訊息 embeds；任何不確定都不寫入。"""

        encoded_chat_id = urllib.parse.quote(chat_id, safe="")
        try:
            chat_response = self._request_json(
                "GET",
                f"{self._base_url}/api/v1/chats/{encoded_chat_id}",
                max_response_bytes=MAX_CHAT_DEDUP_RESPONSE_BYTES,
            )
        except _OpenWebUIHTTPError as exc:
            if exc.status_code in {401, 403, 404}:
                raise OpenWebUIIdentityMismatch(
                    "Open WebUI 無法授權讀取目前聊天"
                ) from exc
            raise OpenWebUIEventOutcomeUnknown(
                "Open WebUI 聊天嵌入清單讀取結果不確定"
            ) from exc
        except Exception as exc:
            raise OpenWebUIEventOutcomeUnknown(
                "Open WebUI 聊天嵌入清單讀取結果不確定"
            ) from exc

        if (
            not isinstance(chat_response, dict)
            or chat_response.get("id") != chat_id
            or chat_response.get("user_id") != user_id
        ):
            raise OpenWebUIIdentityMismatch("Open WebUI 聊天擁有者與呼叫者不一致")
        chat = chat_response.get("chat")
        history = chat.get("history") if isinstance(chat, dict) else None
        messages = history.get("messages") if isinstance(history, dict) else None
        if not isinstance(messages, dict):
            raise OpenWebUIEventOutcomeUnknown("Open WebUI 聊天訊息清單格式無效")
        if message_id not in messages:
            # 僅接受 user.childrenIds 已明確預留的 assistant ID，讓 event upsert
            # 可依 v0.11.3 的聊天樹規則回推 parentId；不能只信任呼叫端 header。
            pending_parents = [
                parent
                for parent in messages.values()
                if isinstance(parent, dict)
                and parent.get("role") == "user"
                and isinstance(parent.get("childrenIds"), list)
                and message_id in parent["childrenIds"]
            ]
            if len(pending_parents) != 1:
                raise OpenWebUIEventNotPersisted(
                    "Open WebUI 無法證明待寫入 assistant 訊息的聊天關聯"
                )
            return []
        message = messages[message_id]
        if not isinstance(message, dict):
            raise OpenWebUIEventOutcomeUnknown("Open WebUI 找不到目前 assistant 訊息")
        parent_id = message.get("parentId")
        parent = messages.get(parent_id) if isinstance(parent_id, str) else None
        if (
            message.get("role") != "assistant"
            or not isinstance(parent, dict)
            or parent.get("role") != "user"
            or not isinstance(parent.get("childrenIds"), list)
            or message_id not in parent["childrenIds"]
        ):
            raise OpenWebUIEventNotPersisted(
                "Open WebUI assistant 訊息缺少有效的 user parent 關聯"
            )

        embeds = message.get("embeds")
        if embeds is None:
            metadata = message.get("metadata")
            embeds = metadata.get("embeds") if isinstance(metadata, dict) else None
        if embeds is None:
            return []
        if not isinstance(embeds, list) or not all(
            isinstance(item, str) for item in embeds
        ):
            raise OpenWebUIEventOutcomeUnknown("Open WebUI 聊天嵌入清單格式無效")
        return embeds

    def _get_api_user_id(self) -> str:
        if self._api_user_id:
            return self._api_user_id
        payload = self._request_json(
            "GET",
            f"{self._base_url}/api/v1/auths/",
        )
        user_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(user_id, str) or _ID_PATTERN.fullmatch(user_id) is None:
            raise OpenWebUIBridgeError("Open WebUI API key 無法識別使用者")
        self._api_user_id = user_id
        return user_id

    def _upload_file(self, content: bytes, file_name: str) -> str:
        boundary = f"----BadmintonAI-{uuid.uuid4().hex}"
        body = b"".join(
            [
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
                    "Content-Type: image/png\r\n\r\n"
                ).encode("ascii"),
                content,
                f"\r\n--{boundary}--\r\n".encode("ascii"),
            ]
        )
        payload = self._request_json(
            "POST",
            f"{self._base_url}/api/v1/files/?process=false&process_in_background=false",
            body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        file_id = payload.get("id") if isinstance(payload, dict) else None
        try:
            parsed_id = str(uuid.UUID(file_id)) if isinstance(file_id, str) else ""
        except (ValueError, AttributeError):
            parsed_id = ""
        if not parsed_id or parsed_id != file_id.casefold():
            raise OpenWebUIBridgeError("Open WebUI 上傳回應缺少有效檔案 ID")
        return parsed_id

    def _emit_files_event(
        self,
        *,
        chat_id: str,
        message_id: str,
        files: list[dict[str, str]],
    ) -> None:
        try:
            payload = self._request_json(
                "POST",
                (
                    f"{self._base_url}/api/v1/chats/{urllib.parse.quote(chat_id, safe='')}"
                    f"/messages/{urllib.parse.quote(message_id, safe='')}/event"
                ),
                body=json.dumps(
                    {"type": "files", "data": {"files": files}},
                    separators=(",", ":"),
                ).encode("utf-8"),
                content_type="application/json",
            )
        except _OpenWebUIHTTPError as exc:
            if exc.status_code in {401, 403, 404, 405, 422}:
                # v0.11.3 在 event emitter 前處理認證、chat owner 與 request schema。
                raise OpenWebUIEventNotPersisted(
                    "Open WebUI 在持久化圖表附件前拒絕 event"
                ) from exc
            raise OpenWebUIEventOutcomeUnknown(
                "Open WebUI event 回應狀態不確定"
            ) from exc
        except Exception as exc:
            raise OpenWebUIEventOutcomeUnknown(
                "Open WebUI event 回應狀態不確定"
            ) from exc
        if payload is not True:
            # v0.11.3 的 event route 可能在 emitter 部分完成後回傳 false；保留檔案。
            raise OpenWebUIEventOutcomeUnknown("Open WebUI 未確認聊天圖片附件事件")

    def _emit_embeds_event(
        self,
        *,
        chat_id: str,
        message_id: str,
        html_content: str,
    ) -> None:
        try:
            payload = self._request_json(
                "POST",
                (
                    f"{self._base_url}/api/v1/chats/{urllib.parse.quote(chat_id, safe='')}"
                    f"/messages/{urllib.parse.quote(message_id, safe='')}/event"
                ),
                body=json.dumps(
                    {
                        "type": "embeds",
                        # Open WebUI v0.11.3 在 replace=false 時會保留既有 embeds，
                        # 再加入本次圖表；replace=true 會覆蓋同訊息先前的圖表。
                        "data": {"embeds": [html_content], "replace": False},
                    },
                    separators=(",", ":"),
                ).encode("utf-8"),
                content_type="application/json",
            )
        except _OpenWebUIHTTPError as exc:
            if exc.status_code in {401, 403, 404, 405, 422}:
                raise OpenWebUIEventNotPersisted(
                    "Open WebUI 在持久化 Rich UI 前明確拒絕 event"
                ) from exc
            raise OpenWebUIEventOutcomeUnknown(
                "Open WebUI Rich UI event 回應狀態不確定"
            ) from exc
        except Exception as exc:
            raise OpenWebUIEventOutcomeUnknown(
                "Open WebUI Rich UI event 回應狀態不確定"
            ) from exc
        if payload is not True:
            raise OpenWebUIEventOutcomeUnknown("Open WebUI 未確認 Rich UI event")

    def _delete_uploaded_files(self, uploaded: Sequence[UploadedImage]) -> None:
        for image in uploaded:
            try:
                self._request_bytes(
                    "DELETE",
                    f"{self._base_url}/api/v1/files/{image.file_id}",
                )
            except OpenWebUIBridgeError:
                # 清理失敗不覆蓋主要上傳／事件錯誤；未關聯檔案仍留在 API key 擁有者的私有檔案庫。
                continue

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        max_response_bytes: int | None = None,
    ) -> Any:
        response_body = self._request_bytes(
            method,
            url,
            body=body,
            content_type=content_type,
            max_response_bytes=max_response_bytes,
        )
        try:
            return json.loads(response_body)
        except (ValueError, TypeError) as exc:
            raise OpenWebUIBridgeError("Open WebUI 回傳無法解析的 JSON") from exc

    def _request_bytes(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        max_response_bytes: int | None = None,
    ) -> bytes:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._urlopen(request, timeout=self._timeout_seconds) as response:
                status = getattr(response, "status", 200)
                if not 200 <= status < 300:
                    raise _OpenWebUIHTTPError(status)
                response_body = (
                    response.read(max_response_bytes + 1)
                    if max_response_bytes is not None
                    else response.read()
                )
                if (
                    max_response_bytes is not None
                    and len(response_body) > max_response_bytes
                ):
                    raise OpenWebUIBridgeError("Open WebUI 聊天回應超過讀取上限")
                return response_body
        except urllib.error.HTTPError as exc:
            raise _OpenWebUIHTTPError(exc.code) from exc
        except OpenWebUIBridgeError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OpenWebUIBridgeError("Open WebUI 圖表附件請求失敗") from exc


def _validate_base_url(value: str) -> str:
    cleaned = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(cleaned)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("BADMINTON_AI_OPEN_WEBUI_URL 必須是無路徑的 HTTP(S) origin")
    return cleaned


def _validate_open_webui_id(value: str, field: str) -> None:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise OpenWebUIBridgeError(f"Open WebUI {field} 格式無效")


__all__ = [
    "MAX_PNG_BYTES",
    "MAX_PNG_DECODED_BYTES",
    "MAX_PNG_PIXELS",
    "OpenWebUIBridgeError",
    "OpenWebUIBridgeNotConfigured",
    "OpenWebUIChartBridge",
    "OpenWebUIEventNotPersisted",
    "OpenWebUIEventOutcomeUnknown",
    "OpenWebUIIdentityMismatch",
    "PNGArtifactError",
    "UploadedImage",
    "decode_verified_png",
    "validate_png_bytes",
]
