"""驗證固定 Open WebUI ChatMenu 原生匯出補丁。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openwebui_patch import apply_chatmenu_exports as patch


def _source_fixture(monkeypatch: pytest.MonkeyPatch) -> str:
    pdf_function = (
        "\tconst downloadPdf = async () => {\n"
        "\t\tthrow new Error('legacy PDF renderer');\n"
        "\t};\n\n"
    )
    monkeypatch.setattr(
        patch,
        "PDF_FUNCTION_SHA256",
        hashlib.sha256(pdf_function.encode("utf-8")).hexdigest(),
    )
    return "\n".join(
        (
            "import { getContext, tick } from 'svelte';",
            "\timport { downloadChatAsPDF } from '$lib/apis/utils';",
            "\timport Messages from '$lib/components/chat/Messages.svelte';",
            "import { chats, folders, settings, theme, user } from '$lib/stores';",
            "\tlet chat = null;\n\tlet showFullMessages = false;",
            pdf_function
            + "\tconst downloadJSONExport = async () => {\n\t\treturn;\n\t};",
            patch.ORIGINAL_HIDDEN_MESSAGES,
            "{#if $user?.role === 'admin' || ($user.permissions?.chat?.export ?? true)}",
            "\t<button>downloadJSONExport(); JSON</button>",
            "\t<button>downloadTxt(); TXT</button>",
            patch.ORIGINAL_PDF_BUTTON,
            "{/if}",
        )
    )


def test_patch_replaces_only_pdf_flow_and_adds_pdf_html_downloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = patch.patch_chatmenu_source(_source_fixture(monkeypatch))

    assert "import { toast } from 'svelte-sonner';" in result
    assert "import { getContext, tick } from 'svelte';" not in result
    assert "downloadChatAsPDF" not in result
    assert "full-messages-container" not in result
    assert "showFullMessages" not in result
    assert "$user.permissions?.chat?.export ?? true" in result
    assert "downloadJSONExport(); JSON" in result
    assert "downloadTxt(); TXT" in result
    assert "互動 HTML (.html)" in result
    assert "正在準備下載…" in result
    assert (
        "/badmintonai/evaluation/api/chats/${encodeURIComponent(chatId)}/export.${format}"
        in result
    )
    assert "credentials: 'same-origin'" in result
    assert "Authorization: `Bearer ${localStorage.token}`" in result
    assert "if (exportInProgress !== null)" in result
    assert "disabled={exportInProgress !== null}" in result
    assert "toast.error" in result
    assert "saveAs(blob, filename)" in result
    assert "content-disposition" in result
    assert "`chat-export-${chatId}.${format}`" in result


def test_patch_rejects_chatmenu_structure_or_pdf_block_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_fixture(monkeypatch)
    with pytest.raises(RuntimeError, match="結構不符：native PDF download menu item"):
        patch.patch_chatmenu_source(source.replace("PDF document (.pdf)", "PDF"))

    with pytest.raises(RuntimeError, match="PDF 原始區塊已變更"):
        patch.patch_chatmenu_source(source.replace("legacy PDF renderer", "changed"))


def test_checkout_validation_requires_pinned_version_lockfile_and_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"version": patch.UPSTREAM_VERSION}), encoding="utf-8"
    )
    (tmp_path / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {"": {"version": patch.UPSTREAM_VERSION}},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        patch.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=patch.UPSTREAM_COMMIT),
    )
    patch._validate_checkout(tmp_path)

    monkeypatch.setattr(
        patch.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="0" * 40),
    )
    with pytest.raises(RuntimeError, match="commit 不符"):
        patch._validate_checkout(tmp_path)

    lock = json.loads((tmp_path / "package-lock.json").read_text(encoding="utf-8"))
    lock["lockfileVersion"] = 2
    (tmp_path / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    monkeypatch.setattr(
        patch.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=patch.UPSTREAM_COMMIT),
    )
    with pytest.raises(RuntimeError, match="lockfile 結構不符"):
        patch._validate_checkout(tmp_path)

    (tmp_path / "package.json").write_text(
        json.dumps({"version": "0.11.4"}), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="版本不符"):
        patch._validate_checkout(tmp_path)
