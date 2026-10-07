"""以受限的 Chromium 環境將可信任的工作台 HTML 轉為 PDF。"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

MAX_PDF_HTML_BYTES = 64 * 1024 * 1024
MAX_PDF_BYTES = 32 * 1024 * 1024
PDF_RENDER_TIMEOUT_SECONDS = 45
PDF_QUEUE_TIMEOUT_SECONDS = 5
PDF_READY_TIMEOUT_MS = 30_000
PDF_RENDER_SLOTS = asyncio.Semaphore(2)


class PDFRenderError(RuntimeError):
    """PDF 產生失敗，包含可安全回傳給使用者的訊息與 HTTP 狀態。"""

    def __init__(self, message: str, *, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


async def render_html_to_pdf(document: str) -> bytes:
    """將伺服器產生的單一 HTML 轉成受大小、時間與併發限制的 PDF。"""

    if not isinstance(document, str) or not document.lstrip().casefold().startswith(
        "<!doctype html"
    ):
        raise PDFRenderError("PDF 來源文件格式無效", status_code=400)
    try:
        html_size = len(document.encode("utf-8"))
    except UnicodeEncodeError:
        raise PDFRenderError("PDF 來源文件格式無效", status_code=400) from None
    if html_size > MAX_PDF_HTML_BYTES:
        raise PDFRenderError("PDF 來源文件超過 64 MiB 上限", status_code=413)

    try:
        await asyncio.wait_for(
            PDF_RENDER_SLOTS.acquire(), timeout=PDF_QUEUE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        raise PDFRenderError("PDF 服務目前忙碌，請稍後重試", status_code=503) from None

    try:
        try:
            pdf_bytes = await asyncio.wait_for(
                _render_with_chromium(document), timeout=PDF_RENDER_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            raise PDFRenderError("PDF 產生逾時，請稍後重試", status_code=504) from None
    finally:
        PDF_RENDER_SLOTS.release()

    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise PDFRenderError("PDF 引擎未產生有效檔案", status_code=502)
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise PDFRenderError("PDF 超過 32 MiB 下載上限", status_code=413)
    return pdf_bytes


async def _render_with_chromium(document: str) -> bytes:
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError:
        raise PDFRenderError("PDF 引擎尚未安裝", status_code=503) from None

    with tempfile.TemporaryDirectory(prefix="badmintonai-pdf-") as temp_dir:
        pdf_path = Path(temp_dir) / "export.pdf"
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-background-networking",
                        "--disable-component-update",
                        "--disable-default-apps",
                        "--disable-extensions",
                        "--disable-sync",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                )
            except Exception:
                raise PDFRenderError("無法啟動 PDF 瀏覽器，請聯絡管理員") from None

            try:
                context = await browser.new_context(
                    accept_downloads=False,
                    color_scheme="light",
                    java_script_enabled=True,
                    locale="zh-TW",
                    service_workers="block",
                )
                try:

                    async def block_external_request(route: object) -> None:
                        request = route.request  # type: ignore[attr-defined]
                        scheme = urlsplit(request.url).scheme.casefold()
                        if scheme in {"about", "blob", "data"}:
                            await route.continue_()  # type: ignore[attr-defined]
                        else:
                            await route.abort()  # type: ignore[attr-defined]

                    await context.route("**/*", block_external_request)
                    page = await context.new_page()
                    await page.set_viewport_size({"width": 1080, "height": 1440})
                    page.set_default_timeout(PDF_READY_TIMEOUT_MS)
                    await page.set_content(
                        document,
                        wait_until="load",
                        timeout=PDF_READY_TIMEOUT_MS,
                    )
                    await page.emulate_media(media="print", color_scheme="light")
                    await page.evaluate("() => document.fonts.ready.then(() => true)")
                    await page.wait_for_function(
                        "() => window.__BADMINTON_PDF_RENDER__ && "
                        "['ready', 'error'].includes(window.__BADMINTON_PDF_RENDER__.status)",
                        timeout=PDF_READY_TIMEOUT_MS,
                    )
                    render_state = await page.evaluate(
                        "() => window.__BADMINTON_PDF_RENDER__"
                    )
                    if (
                        not isinstance(render_state, dict)
                        or render_state.get("status") != "ready"
                    ):
                        raise PDFRenderError(
                            "圖表無法完成繪製，未產生 PDF", status_code=422
                        )
                    await page.pdf(
                        path=str(pdf_path),
                        format="A4",
                        print_background=True,
                        prefer_css_page_size=True,
                    )
                except PlaywrightTimeoutError:
                    raise PDFRenderError(
                        "字型或圖表渲染逾時，未產生 PDF", status_code=504
                    ) from None
                except PDFRenderError:
                    raise
                except Exception:
                    raise PDFRenderError(
                        "PDF 瀏覽器無法完成文件轉換", status_code=502
                    ) from None
                finally:
                    await context.close()
            finally:
                await browser.close()

        try:
            size = pdf_path.stat().st_size
            if size < 5 or size > MAX_PDF_BYTES:
                raise PDFRenderError("PDF 超過允許大小或內容無效", status_code=413)
            return pdf_path.read_bytes()
        except OSError:
            raise PDFRenderError("PDF 暫存檔無法讀取", status_code=502) from None


__all__ = [
    "MAX_PDF_BYTES",
    "MAX_PDF_HTML_BYTES",
    "PDFRenderError",
    "render_html_to_pdf",
]
