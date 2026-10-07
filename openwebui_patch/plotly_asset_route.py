"""Open WebUI 同站提供固定版 Plotly.js；上游只連內部 Tool Server 固定路徑。"""

from __future__ import annotations

import asyncio
import http.client
import urllib.error
import urllib.request
from email.message import Message
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

PLOTLY_VERSION = "6.6.0"
PLOTLY_JS_VERSION = "3.4.0"
PLOTLY_ASSET_URL = "http://tool-server:8000/assets/plotly-6.6.0.min.js"
MAX_PLOTLY_ASSET_BYTES = 16 * 1024 * 1024
PLOTLY_ASSET_TIMEOUT_SECONDS = 10
PLOTLY_ASSET_HEADERS = {
    "Cache-Control": "public, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
    "X-Plotly-Python-Version": PLOTLY_VERSION,
}

router = APIRouter()


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """固定資產來源若轉址即視為失敗，不跟隨至其他主機。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PlotlyAssetUpstreamError(RuntimeError):
    """固定 Tool Server 資產無法通過代理檢查。"""


def _open_upstream(request: urllib.request.Request, timeout: int) -> Any:
    """只以固定 URL 開啟 GET，並拒絕 HTTP 轉址。"""

    opener = urllib.request.build_opener(_NoRedirectHandler)
    return opener.open(request, timeout=timeout)


def _fetch_plotly_javascript() -> bytes:
    """讀取並驗證固定版本 bundle；不接受呼叫端指定 URL 或路徑。"""

    request = urllib.request.Request(
        PLOTLY_ASSET_URL,
        headers={"Accept": "application/javascript"},
        method="GET",
    )
    try:
        with _open_upstream(request, PLOTLY_ASSET_TIMEOUT_SECONDS) as response:
            if getattr(response, "status", 200) != 200:
                raise PlotlyAssetUpstreamError("上游狀態錯誤")
            headers = getattr(response, "headers", Message())
            if headers.get_content_type() != "application/javascript":
                raise PlotlyAssetUpstreamError("上游 MIME 類型錯誤")
            if headers.get("X-Plotly-Python-Version") != PLOTLY_VERSION:
                raise PlotlyAssetUpstreamError("上游 Plotly 版本錯誤")
            content_length = headers.get("Content-Length")
            expected_length = None
            if content_length is not None:
                try:
                    expected_length = int(content_length)
                    if expected_length < 0:
                        raise PlotlyAssetUpstreamError("上游長度標頭無效")
                    if expected_length > MAX_PLOTLY_ASSET_BYTES:
                        raise PlotlyAssetUpstreamError("上游資產超過大小限制")
                except ValueError as exc:
                    raise PlotlyAssetUpstreamError("上游長度標頭無效") from exc
            content = response.read(MAX_PLOTLY_ASSET_BYTES + 1)
    except PlotlyAssetUpstreamError:
        raise
    except (
        http.client.HTTPException,
        urllib.error.URLError,
        TimeoutError,
        OSError,
    ) as exc:
        raise PlotlyAssetUpstreamError("固定 Plotly 資產上游無法連線") from exc

    if len(content) > MAX_PLOTLY_ASSET_BYTES:
        raise PlotlyAssetUpstreamError("上游資產超過大小限制")
    if expected_length is not None and len(content) != expected_length:
        raise PlotlyAssetUpstreamError("上游資產長度不一致")
    version_marker = f"plotly.js v{PLOTLY_JS_VERSION}".encode("ascii")
    if version_marker not in content.lower():
        raise PlotlyAssetUpstreamError("上游 bundle 版本標記錯誤")
    return content


@router.get("/assets/plotly-6.6.0.min.js", include_in_schema=False)
async def plotly_asset() -> Response:
    """提供唯讀固定版 JS，不需要 session cookie，也不暴露使用者資料。

    Open WebUI 的 srcdoc iframe 預設採 opaque origin，無法可靠攜帶登入 cookie；
    此路徑只回傳公開且不可變的靜態程式庫，並拒絕任意 URL 代理。
    """

    try:
        content = await asyncio.to_thread(_fetch_plotly_javascript)
    except PlotlyAssetUpstreamError as exc:
        raise HTTPException(
            status_code=502, detail="固定 Plotly 資產目前不可用"
        ) from exc
    return Response(
        content=content,
        media_type="application/javascript",
        headers=PLOTLY_ASSET_HEADERS,
    )
