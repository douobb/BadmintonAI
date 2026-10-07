"""固定 Open WebUI 的失敗訊息沿用原生 regenerate 分支，僅人工重試。"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

try:
    from openwebui_patch.apply_chatmenu_exports import _validate_checkout
except ModuleNotFoundError:
    from apply_chatmenu_exports import _validate_checkout

CHAT_PATH = Path("src/lib/components/chat/Chat.svelte")
RESPONSE_PATH = Path("src/lib/components/chat/Messages/ResponseMessage.svelte")
START = "\tconst regenerateResponse = async (message, suggestionPrompt = null) => {"
END = "\tconst continueResponse = async () => {"
REGENERATE_SHA256 = "cbfca1be2c8d9f7ce01695f56a8ec876a4b7a44fc7eee753d02e3774cdc2b58f"
ERROR_LINE = "\t\t\t\t\t\t\t\t<Error content={message?.error?.content ?? message.content} />"

RETRY_WRAPPER = """	let badmintonaiRetryInProgress = false;
	const regenerateResponse = async (message, suggestionPrompt = null) => {
		if (!message?.error) return nativeRegenerateResponse(message, suggestionPrompt);
		if (badmintonaiRetryInProgress || generating || (taskIds?.length ?? 0) > 0) return;
		if (!message.done || history.messages[history.currentId]?.done === false) return;
		badmintonaiRetryInProgress = true;
		const retryChatId = $chatId;
		try {
			// 只讀原生 task；無法確認時保留失敗分支，不重送生成。
			let tasks;
			try {
				tasks = await getTaskIdsByChatId(localStorage.token, retryChatId);
			} catch {
				toast.error('無法確認原回合狀態，尚未重送。');
				return;
			}
			if (!Array.isArray(tasks?.task_ids) || tasks.task_ids.length > 0) {
				toast.error('原回合仍執行或狀態未知，請稍後重新讀取對話。');
				return;
			}
			if ($chatId !== retryChatId || generating || (taskIds?.length ?? 0) > 0) return;
			await nativeRegenerateResponse(message, suggestionPrompt);
		} finally {
			badmintonaiRetryInProgress = false;
		}
	};

"""

RETRY_BUTTON = """
								{#if !readOnly && ($user?.role === 'admin' || ($user?.permissions?.chat?.regenerate_response ?? true))}
									<button
										type="button"
										aria-label="重試最後失敗回合"
										aria-busy={badmintonaiRetryPending}
										disabled={badmintonaiRetryPending || !message.done || history.messages[history.currentId]?.done === false}
										class="mt-2 rounded-lg px-3 py-1.5 text-sm bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-100 hover:bg-gray-200 dark:hover:bg-gray-700 disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2"
										on:click={async () => {
											if (badmintonaiRetryPending || !message.done) return;
											badmintonaiRetryPending = true;
											try { await regenerateResponse(message); }
											finally { badmintonaiRetryPending = false; }
										}}
									>Retry（重試）</button>
								{/if}"""


def patch_chat_source(source: str) -> str:
    if "let badmintonaiRetryInProgress = false;" in source:
        if "const nativeRegenerateResponse = async" not in source or RETRY_WRAPPER not in source:
            raise RuntimeError("人工 Retry 補丁不完整")
        return source
    start = source.index(START)
    end = source.index(END, start)
    region = source[start:end]
    if hashlib.sha256(region.encode("utf-8")).hexdigest() != REGENERATE_SHA256:
        raise RuntimeError("原生 regenerate 函式已變更，拒絕套用 Retry")
    native = region.replace("const regenerateResponse = async", "const nativeRegenerateResponse = async", 1)
    return source[:start] + native + RETRY_WRAPPER + source[end:]


def patch_response_source(source: str) -> str:
    if "let badmintonaiRetryPending = false;" in source:
        if RETRY_BUTTON not in source:
            raise RuntimeError("失敗訊息 Retry 補丁不完整")
        return source
    if source.count(ERROR_LINE) != 1 or source.count("\texport let regenerateResponse: Function;") != 1:
        raise RuntimeError("原生失敗訊息結構已變更，拒絕套用 Retry")
    source = source.replace("\texport let regenerateResponse: Function;",
                            "\texport let regenerateResponse: Function;\n\tlet badmintonaiRetryPending = false;", 1)
    return source.replace(ERROR_LINE, ERROR_LINE + RETRY_BUTTON, 1)


def apply_patch(root: Path) -> None:
    _validate_checkout(root)
    changes = [(root / CHAT_PATH, patch_chat_source), (root / RESPONSE_PATH, patch_response_source)]
    prepared = [(path, patch(path.read_text(encoding="utf-8"))) for path, patch in changes]
    for path, source in prepared:
        path.write_text(source, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    apply_patch(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/app"))
