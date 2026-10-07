"""驗證共享 Chromium PDF renderer 的限制與安全失敗行為。"""

from __future__ import annotations

import asyncio

import pytest

import scripts.html_to_pdf as pdf_module
from scripts.html_to_pdf import PDFRenderError, render_html_to_pdf


def _run(coroutine):
    return asyncio.run(coroutine)


def test_pdf_renderer_accepts_only_pdf_output(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_render(_document: str) -> bytes:
        return b"%PDF-1.7\nfixture"

    monkeypatch.setattr(pdf_module, "_render_with_chromium", fake_render)

    assert (
        _run(render_html_to_pdf("<!doctype html><html></html>")) == b"%PDF-1.7\nfixture"
    )

    async def invalid_render(_document: str) -> bytes:
        return b"not a PDF"

    monkeypatch.setattr(pdf_module, "_render_with_chromium", invalid_render)
    with pytest.raises(PDFRenderError, match="未產生有效檔案"):
        _run(render_html_to_pdf("<!doctype html><html></html>"))


def test_pdf_renderer_rejects_invalid_or_oversized_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(PDFRenderError) as invalid:
        _run(render_html_to_pdf("<html></html>"))
    assert invalid.value.status_code == 400

    monkeypatch.setattr(pdf_module, "MAX_PDF_HTML_BYTES", 20)
    with pytest.raises(PDFRenderError) as oversized:
        _run(render_html_to_pdf("<!doctype html>" + "x" * 20))
    assert oversized.value.status_code == 413


def test_pdf_renderer_rejects_oversized_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_module, "MAX_PDF_BYTES", 10)

    async def oversized_render(_document: str) -> bytes:
        return b"%PDF-1.7\n" + b"x" * 10

    monkeypatch.setattr(pdf_module, "_render_with_chromium", oversized_render)
    with pytest.raises(PDFRenderError) as error:
        _run(render_html_to_pdf("<!doctype html><html></html>"))
    assert error.value.status_code == 413


def test_pdf_renderer_enforces_timeout_and_queue_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pdf_module, "PDF_RENDER_TIMEOUT_SECONDS", 0.001)

    async def slow_render(_document: str) -> bytes:
        await asyncio.sleep(0.03)
        return b"%PDF-1.7\nfixture"

    monkeypatch.setattr(pdf_module, "_render_with_chromium", slow_render)
    with pytest.raises(PDFRenderError) as timeout:
        _run(render_html_to_pdf("<!doctype html><html></html>"))
    assert timeout.value.status_code == 504

    monkeypatch.setattr(pdf_module, "PDF_QUEUE_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(pdf_module, "PDF_RENDER_SLOTS", asyncio.Semaphore(0))
    with pytest.raises(PDFRenderError) as busy:
        _run(render_html_to_pdf("<!doctype html><html></html>"))
    assert busy.value.status_code == 503
