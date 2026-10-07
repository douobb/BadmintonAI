"""將固定 Open WebUI v0.11.3 的原生聊天匯出選單改接專案同站端點。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

UPSTREAM_VERSION = "0.11.3"
UPSTREAM_COMMIT = "2a960a59fe1dbbd35282f0556b3666d81102e781"
CHAT_MENU_PATH = Path("src/lib/components/layout/Sidebar/ChatMenu.svelte")
PDF_FUNCTION_SHA256 = "bb3577fe73f1031338a43cd62200811da76103a774c65e796d0ec2aaa4139c95"

PDF_FUNCTION_START = "\tconst downloadPdf = async () => {"
JSON_FUNCTION_START = "\tconst downloadJSONExport = async () => {"


def _expand_tabs(source: str) -> str:
    """讓多行範本中的可讀 tab 標記轉為實際縮排 tab。"""
    return source.replace("\\t", "\t")


ORIGINAL_PDF_BUTTON = _expand_tabs("""\t\t\t\t\t<button
\t\t\t\t\t\tdraggable="false"
\t\t\t\t\t\tclass="flex h-[1.6875rem] gap-2 items-center rounded-xl px-2 text-[0.8125rem] cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-900 select-none w-full"
\t\t\t\t\t\ton:click={() => {
\t\t\t\t\t\t\tdownloadPdf();
\t\t\t\t\t\t}}
\t\t\t\t\t>
\t\t\t\t\t\t<div class="flex items-center line-clamp-1">{$i18n.t('PDF document (.pdf)')}</div>
\t\t\t\t\t</button>""")

NEW_EXPORT_BUTTONS = _expand_tabs("""\t\t\t\t\t<button
\t\t\t\t\t\tdraggable="false"
\t\t\t\t\t\tdisabled={exportInProgress !== null}
\t\t\t\t\t\taria-busy={exportInProgress === 'pdf'}
\t\t\t\t\t\tclass="flex h-[1.6875rem] gap-2 items-center rounded-xl px-2 text-[0.8125rem] cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-900 disabled:cursor-not-allowed disabled:opacity-50 select-none w-full"
\t\t\t\t\t\ton:click={() => downloadChatExport('pdf')}
\t\t\t\t\t>
\t\t\t\t\t\t<div class="flex items-center line-clamp-1">
\t\t\t\t\t\t\t{exportInProgress === 'pdf' ? '正在準備下載…' : $i18n.t('PDF document (.pdf)')}
\t\t\t\t\t\t</div>
\t\t\t\t\t</button>
\t\t\t\t\t<button
\t\t\t\t\t\tdraggable="false"
\t\t\t\t\t\tdisabled={exportInProgress !== null}
\t\t\t\t\t\taria-busy={exportInProgress === 'html'}
\t\t\t\t\t\tclass="flex h-[1.6875rem] gap-2 items-center rounded-xl px-2 text-[0.8125rem] cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-900 disabled:cursor-not-allowed disabled:opacity-50 select-none w-full"
\t\t\t\t\t\ton:click={() => downloadChatExport('html')}
\t\t\t\t\t>
\t\t\t\t\t\t<div class="flex items-center line-clamp-1">
\t\t\t\t\t\t\t{exportInProgress === 'html' ? '正在準備下載…' : '互動 HTML (.html)'}
\t\t\t\t\t\t</div>
\t\t\t\t\t</button>""")

ORIGINAL_HIDDEN_MESSAGES = _expand_tabs("""{#if chat && showFullMessages}
\t<div class="hidden w-full h-full flex-col">
\t\t<div id="full-messages-container">
\t\t\t<Messages
\t\t\t\tclassName="h-full flex pt-4 pb-8 w-full"
\t\t\t\tchatId={`chat-preview-${chat?.id ?? ''}`}
\t\t\t\tuser={$user}
\t\t\t\treadOnly={true}
\t\t\t\thistory={chat.chat.history}
\t\t\t\tmessages={chat.chat.messages}
\t\t\t\tautoScroll={true}
\t\t\t\tsendMessage={() => {}}
\t\t\t\tcontinueResponse={() => {}}
\t\t\t\tregenerateResponse={() => {}}
\t\t\t\tmessagesCount={null}
\t\t\t\teditCodeBlock={false}
\t\t\t/>
\t\t</div>
\t</div>
{/if}""")

DOWNLOAD_FUNCTION = _expand_tabs("""\tconst downloadChatExport = async (format: 'pdf' | 'html') => {
\t\tif (exportInProgress !== null) {
\t\t\treturn;
\t\t}

\t\texportInProgress = format;
\t\ttry {
\t\t\tconst endpoint = `/badmintonai/evaluation/api/chats/${encodeURIComponent(chatId)}/export.${format}`;
\t\t\tconst response = await fetch(endpoint, {
\t\t\t\tcredentials: 'same-origin',
\t\t\t\theaders: localStorage.token
\t\t\t\t\t? { Authorization: `Bearer ${localStorage.token}` }
\t\t\t\t\t: {}
\t\t\t});

\t\t\tif (!response.ok) {
\t\t\t\tconst body = await response.json().catch(() => null);
\t\t\t\tconst detail = typeof body?.detail === 'string' ? body.detail : '';
\t\t\t\ttoast.error(detail || `${$i18n.t('Download failed')} (HTTP ${response.status})`);
\t\t\t\treturn;
\t\t\t}

\t\t\tconst blob = await response.blob();
\t\t\tconst disposition = response.headers.get('content-disposition') ?? '';
\t\t\tconst filename =
\t\t\t\tdisposition.match(/filename="?([^";]+)"?/i)?.[1] ?? `chat-export-${chatId}.${format}`;
\t\t\tsaveAs(blob, filename);
\t\t} catch (error) {
\t\t\tconsole.error('Error exporting chat', error);
\t\t\ttoast.error($i18n.t('Download failed'));
\t\t} finally {
\t\t\texportInProgress = null;
\t\t}
\t};

""")


def _replace_once(source: str, original: str, replacement: str, label: str) -> str:
    occurrences = source.count(original)
    if occurrences != 1:
        raise RuntimeError(
            f"Open WebUI v{UPSTREAM_VERSION} ChatMenu 結構不符：{label} (count={occurrences})"
        )
    return source.replace(original, replacement, 1)


def _pdf_function_region(source: str) -> tuple[int, int, str]:
    if source.count(PDF_FUNCTION_START) != 1 or source.count(JSON_FUNCTION_START) != 1:
        raise RuntimeError("Open WebUI v0.11.3 ChatMenu 匯出函式邊界不符")
    start = source.index(PDF_FUNCTION_START)
    end = source.index(JSON_FUNCTION_START, start)
    if end <= start:
        raise RuntimeError("Open WebUI v0.11.3 ChatMenu 匯出函式順序不符")
    return start, end, source[start:end]


def _verify_pdf_function(source: str) -> None:
    _start, _end, region = _pdf_function_region(source)
    digest = hashlib.sha256(region.encode("utf-8")).hexdigest()
    if digest != PDF_FUNCTION_SHA256:
        raise RuntimeError(
            "Open WebUI v0.11.3 ChatMenu PDF 原始區塊已變更，拒絕套用補丁"
        )


def patch_chatmenu_source(source: str) -> str:
    """依固定錨點改寫 ChatMenu；所有改動都要求唯一對應，避免誤套。"""
    _verify_pdf_function(source)
    source = _replace_once(
        source,
        "import { getContext, tick } from 'svelte';",
        "import { getContext } from 'svelte';\n\timport { toast } from 'svelte-sonner';",
        "Svelte imports",
    )
    source = _replace_once(
        source,
        "\timport { downloadChatAsPDF } from '$lib/apis/utils';\n",
        "",
        "legacy PDF API import",
    )
    source = _replace_once(
        source,
        "\timport Messages from '$lib/components/chat/Messages.svelte';\n",
        "",
        "PDF preview component import",
    )
    source = _replace_once(
        source,
        "import { chats, folders, settings, theme, user } from '$lib/stores';",
        "import { chats, folders, user } from '$lib/stores';",
        "PDF settings imports",
    )
    source = _replace_once(
        source,
        "\tlet chat = null;\n\tlet showFullMessages = false;",
        "\tlet exportInProgress: 'pdf' | 'html' | null = null;",
        "PDF preview state",
    )

    start, end, _region = _pdf_function_region(source)
    source = source[:start] + DOWNLOAD_FUNCTION + source[end:]
    source = _replace_once(
        source,
        ORIGINAL_HIDDEN_MESSAGES,
        "",
        "PDF preview markup",
    )
    source = _replace_once(
        source,
        ORIGINAL_PDF_BUTTON,
        NEW_EXPORT_BUTTONS,
        "native PDF download menu item",
    )

    obsolete = (
        "downloadChatAsPDF",
        "jspdf",
        "html2canvas-pro",
        "full-messages-container",
        "showFullMessages",
    )
    if any(marker in source for marker in obsolete):
        raise RuntimeError("Open WebUI ChatMenu 仍含已移除的舊 PDF 匯出流程")
    return source


def _validate_checkout(repository_root: Path) -> None:
    package = json.loads((repository_root / "package.json").read_text(encoding="utf-8"))
    lock = json.loads(
        (repository_root / "package-lock.json").read_text(encoding="utf-8")
    )
    locked_version = lock.get("packages", {}).get("", {}).get("version")
    if package.get("version") != UPSTREAM_VERSION or locked_version != UPSTREAM_VERSION:
        raise RuntimeError("Open WebUI package 與 lockfile 版本不符，拒絕建置前端補丁")
    if lock.get("lockfileVersion") != 3:
        raise RuntimeError("Open WebUI lockfile 結構不符，拒絕建置前端補丁")

    revision = subprocess.run(
        ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if revision != UPSTREAM_COMMIT:
        raise RuntimeError(
            f"Open WebUI commit 不符：預期 {UPSTREAM_COMMIT}，實際 {revision}"
        )


def apply_patch(repository_root: Path) -> None:
    _validate_checkout(repository_root)
    source_path = repository_root / CHAT_MENU_PATH
    source = source_path.read_text(encoding="utf-8")
    _verify_pdf_function(source)
    patched = patch_chatmenu_source(source)
    source_path.write_text(patched, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/app")
    apply_patch(root)
