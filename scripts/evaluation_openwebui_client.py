"""Open WebUI v0.11.3 API adapter for the resumable evaluation runner.

Open WebUI does not expose a server-side idempotency key for chat creation or
completion requests. Mutations with unknown outcomes are therefore reconciled by
stable chat/message IDs and left pending when the result cannot be read back.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from scripts.evaluation_runner import TurnResult

_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_CHART_FINGERPRINT = re.compile(
    r'<meta name="badmintonai-chart-fingerprint" content="sha256:([0-9a-f]{64})">'
)
_TOOL_CALL_TYPES = {"function_call", "tool_call"}
_ANALYSIS_TERMINAL_CODES = frozenset(
    {
        "analysis_message_limit",
        "analysis_retry_limit",
        "analysis_terminated",
        "sandbox_timeout",
        "sandbox_unavailable",
        "sandbox_execution_failure",
    }
)
_BADMINTONAI_STREAM_ERRORS = {
    "badmintonai_stream_no_data_timeout": "模型服務串流等待資料逾時；回合已停止，未完成輸出不視為完成。",
    "badmintonai_stream_no_progress_timeout": "模型服務串流未持續產生回答或工具參數；回合已停止，未完成部分不視為已驗證。",
    "badmintonai_stream_total_timeout": "模型服務串流達到總等待上限；回合已停止，未完成輸出不視為完成。",
    "badmintonai_stream_upstream_error": "模型服務串流中斷；回合已停止，未完成輸出不視為完成。",
    "badmintonai_stream_incomplete": "模型服務串流未正常結束；回合已停止，未完成輸出不視為完成。",
}
_UNEXECUTED_TOOL_OUTPUTS = frozenset(
    {
        "本輪已因工具終止狀態停止；工具呼叫未執行。",
        "澄清已提交；本輪其餘工具呼叫未執行。",
        "工具呼叫因本輪錯誤而未執行。",
        "分析執行額度已保留給本輪收尾；此分析呼叫未執行。",
        "無進展收尾已停止；此工具呼叫未執行。",
        "工具呼叫因達到本輪上限而未執行。",
    }
)
_FIXED_TOOL_TURN_FAILURES = {
    "本則回答的分析執行次數已用完，且仍有未解錯誤或沒有可驗證的保存結果；分析未完成，不提供未驗證數值。": (
        "analysis_message_limit",
        "分析執行次數已達上限，且沒有可驗證的完整結果；未完成部分不視為已驗證結論。",
    ),
    "本輪收尾未能產生可靠的最終答覆；已保存結果與已發布圖表仍保留，不提供未驗證的結論或數值。": (
        "analysis_incomplete",
        "本輪收尾未能產生可驗證的完整答覆；已保存結果與圖表保留，未完成部分不視為結論。",
    ),
    "已停止重複呼叫；收尾仍要求工具，本輪未執行該呼叫，未完成部分不提供未驗證結論。": (
        "analysis_incomplete",
        "收尾仍要求工具，因此本輪未完成；未完成部分不視為已驗證結論。",
    ),
    "本輪已達工具呼叫上限，尚未完成；不提供未驗證的結論或數值。": (
        "analysis_incomplete",
        "本輪已達工具呼叫上限；未完成部分不視為已驗證結論。",
    ),
}
_QUOTED_SPAN = re.compile(r"「[^」\n]*」|『[^』\n]*』|“[^”\n]*”|\"[^\"\n]*\"")
_MARKDOWN_QUOTE_LINE = re.compile(r"(?m)^\s*>.*$")
_CHOICE_LINE = re.compile(r"(?m)^\s*(?:\d+[.)、]|[A-Za-z][.)]|[-*•])\s+\S")
_DIRECT_CLARIFICATION = re.compile(
    r"(?:請問|(?:請|麻煩)(?:你|您)?\s*(?:選擇|挑選|確認|提供|補充|指定|定義|決定|回答)|"
    r"(?:你|您)\s*(?:指的是|所指的是|需要的是|想要的是))[^。！？?？\n]{0,100}[?？]"
    r"|(?:你|您)\s*(?:希望|想要|想|打算|要)[^。！？?？\n]{0,50}"
    r"(?:哪|何種|什麼)[^。！？?？\n]{0,40}[?？]"
    r"|(?:could you|would you|please)\s+(?:clarify|choose|select|specify|confirm|provide)"
    r"[^.!?\n]{0,100}\?",
    re.IGNORECASE,
)
_CHOICE_REQUEST = re.compile(
    r"(?:請|麻煩)(?:你|您)?\s*(?:選擇|挑選|選一|擇一)|"
    r"(?:你|您)\s*(?:希望|想要|想|打算|要)[^。！？?？\n]{0,30}"
    r"(?:選擇|挑選|採用|採取|使用)|"
    r"(?:please\s+)?(?:choose|select)\s+(?:one|an? option)",
    re.IGNORECASE,
)
_TAIPEI_TIMEZONE = timezone(timedelta(hours=8))
_EVALUATION_ROOT_FOLDER = "評測"
_TOOL_EVENT_CONTRACT_API = (
    "decode_tool_payload",
    "decode_tool_result_payload",
    "decode_tool_error_payload",
    "is_unexecuted_tool_output",
)


def _load_tool_event_contract() -> Any:
    """先用已安裝的 Open WebUI helper；本機開發時才讀 repo source。"""

    try:
        from open_webui import evaluation_observability as contract
    except ModuleNotFoundError as exc:
        if exc.name not in {"open_webui", "open_webui.evaluation_observability"}:
            raise
        try:
            from openwebui_patch import evaluation_observability as contract
        except ModuleNotFoundError as source_exc:
            if source_exc.name not in {
                "openwebui_patch",
                "openwebui_patch.evaluation_observability",
            }:
                raise
            raise RuntimeError(
                "找不到相容的 evaluation_observability helper；請以新版部署來源更新 Open WebUI helper。"
            ) from source_exc

    missing = [
        name
        for name in _TOOL_EVENT_CONTRACT_API
        if not callable(getattr(contract, name, None))
    ]
    if missing:
        raise RuntimeError(
            "Open WebUI evaluation_observability helper 與掛載的評測 client 版本不相容；"
            "請更新 helper 映像，缺少 API：" + ", ".join(missing)
        )
    return contract


_tool_event_contract = _load_tool_event_contract()


class OpenWebUIClientError(RuntimeError):
    """不含 request headers/body 的 Open WebUI API 錯誤。"""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.uncertain = uncertain


class OpenWebUIStateUncertain(OpenWebUIClientError):
    """遠端 mutation 可能已完成；不得自動重送。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, uncertain=True)


class OpenWebUIEvaluationClient:
    """以固定模型 ID 呼叫既有 Open WebUI 設定，不覆寫 tools 或 model params。

    API key 必須由呼叫端明確傳入；此 client 不讀取環境變數或 .env，也不會
    將 key 寫入評測回應。urlopen 可注入 mock transport 供測試使用。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_id: str,
        timeout_seconds: float = 20.0,
        poll_interval_seconds: float = 0.5,
        max_wait_seconds: float = 120.0,
        urlopen: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        clarification_detector: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        self._base_url = _validate_base_url(base_url)
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("必須明確提供 Open WebUI API key")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("必須明確提供 Open WebUI model ID")
        if timeout_seconds <= 0 or poll_interval_seconds < 0 or max_wait_seconds < 0:
            raise ValueError("Open WebUI timeout 設定無效")
        self._api_key = api_key.strip()
        self._model_id = model_id.strip()
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._max_wait_seconds = max_wait_seconds
        self._urlopen = urlopen or urllib.request.urlopen
        self._sleep = sleep
        self._monotonic = monotonic
        self._clarification_detector = (
            clarification_detector or _looks_like_clarification
        )

    def create_conversation(
        self,
        run_id: str,
        question_id: str,
        idempotency_key: str,
        model_snapshot: Any,
    ) -> str:
        """建空白 chat；若 POST 結果不明，僅以固定標題搜尋復原，不盲目重建。"""

        self._validate_model_snapshot(model_snapshot)
        title = _conversation_title(run_id, question_id, idempotency_key)
        existing = self._find_conversation(title)
        if existing:
            return existing

        body = {
            "chat": {
                "title": title,
                "models": [self._model_id],
                "history": {"currentId": None, "messages": {}},
                "messages": [],
            }
        }
        try:
            response = self._request_json(
                "POST",
                "/api/v1/chats/new",
                body=body,
                mutation=True,
            )
        except OpenWebUIStateUncertain as original_error:
            try:
                recovered = self._find_conversation(title)
            except Exception as recovery_error:
                raise OpenWebUIStateUncertain(
                    "建立對話結果不明，且目前無法確認是否已建立"
                ) from recovery_error
            if recovered:
                return recovered
            raise original_error

        conversation_id = response.get("id") if isinstance(response, dict) else None
        if not isinstance(conversation_id, str) or not conversation_id:
            raise OpenWebUIStateUncertain("建立對話回應缺少 ID，無法確認遠端狀態")
        return conversation_id

    def recover_conversation(
        self, run_id: str, question_id: str, idempotency_key: str
    ) -> str | None:
        """以穩定標題找回 chat；搜尋不到仍屬不確定，不代表可安全重建。"""

        title = _conversation_title(run_id, question_id, idempotency_key)
        conversation_id = self._find_conversation(title)
        if conversation_id is None:
            raise OpenWebUIStateUncertain(
                "尚未搜尋到待建立的對話；保留待復原狀態以避免重複建立"
            )
        return conversation_id

    def create_conversation_in_folder(
        self,
        run_id: str,
        question_id: str,
        idempotency_key: str,
        model_snapshot: Any,
        *,
        created_at: str,
        question_count: int,
    ) -> str:
        """在本次 run 的原生資料夾建立 Q{id} 對話，讀回確認歸檔位置。"""

        self._validate_model_snapshot(model_snapshot)
        folder_id = self._ensure_evaluation_run_folder(
            run_id, created_at=created_at, question_count=question_count
        )
        title = _question_conversation_title(question_id)
        existing = self._find_conversation_in_folder(title, folder_id)
        if existing:
            return existing

        body = {
            "chat": {
                "title": title,
                "models": [self._model_id],
                "history": {"currentId": None, "messages": {}},
                "messages": [],
            },
            "folder_id": folder_id,
        }
        try:
            response = self._request_json(
                "POST",
                "/api/v1/chats/new",
                body=body,
                mutation=True,
            )
            conversation_id = response.get("id") if isinstance(response, dict) else None
            if not isinstance(conversation_id, str) or not conversation_id:
                raise OpenWebUIStateUncertain(
                    "建立對話回應缺少 ID，無法確認資料夾歸檔狀態"
                )
            self._verify_chat_folder(conversation_id, folder_id)
            return conversation_id
        except OpenWebUIStateUncertain as original_error:
            try:
                recovered = self._find_conversation_in_folder(title, folder_id)
            except Exception as recovery_error:
                raise OpenWebUIStateUncertain(
                    "建立資料夾對話結果不明，且目前無法確認是否已建立"
                ) from recovery_error
            if recovered:
                return recovered
            raise original_error

    def recover_conversation_in_folder(
        self,
        run_id: str,
        question_id: str,
        _idempotency_key: str,
        *,
        created_at: str,
        question_count: int,
    ) -> str:
        """只在原 run 資料夾讀回 Q{id}；找不到時不觸發重建。"""

        folder_id = self._ensure_evaluation_run_folder(
            run_id, created_at=created_at, question_count=question_count
        )
        title = _question_conversation_title(question_id)
        conversation_id = self._find_conversation_in_folder(title, folder_id)
        if conversation_id is None:
            raise OpenWebUIStateUncertain(
                "尚未在原評測資料夾讀到待建立對話；保留待復原狀態以避免重複建立"
            )
        return conversation_id

    def send_turn(
        self,
        conversation_id: str,
        content: str,
        operation_id: str,
    ) -> TurnResult:
        """在既有 chat 送一輪，並等到 assistant message 完成或逾時待復原。"""

        if not isinstance(content, str):
            raise ValueError("回合內容必須是字串")
        tool_ids = self._model_tool_ids()
        user_message_id = _message_id(operation_id, "user")
        assistant_message_id = _message_id(operation_id, "assistant")

        # 查父節點是唯讀 preflight；失敗時尚未送出 mutation，可由操作者重試。
        chat = self._get_chat(conversation_id, uncertain_on_failure=False)
        parent_id = _current_assistant_id(chat)
        user_message = {
            "id": user_message_id,
            "role": "user",
            "content": content,
            "parentId": parent_id,
            "models": [self._model_id],
        }
        request_body = {
            "model": self._model_id,
            "messages": [{"role": "user", "content": content}],
            "stream": True,
            "chat_id": conversation_id,
            "parent_id": parent_id,
            "id": assistant_message_id,
            "session_id": _message_id(operation_id, "session"),
            "user_message": user_message,
            # 由固定版 Open WebUI chat endpoint 取出並併入伺服器 metadata；
            # 不放進 metadata（該端點會覆蓋之），也不帶入供應商 payload。
            "badmintonai_evaluation": True,
            "badmintonai_operation_id": operation_id,
            "tool_ids": tool_ids,
        }

        accepted = self._request_json(
            "POST",
            "/api/chat/completions",
            body=request_body,
            mutation=True,
        )
        if not isinstance(accepted, dict) or accepted.get("chat_id") != conversation_id:
            raise OpenWebUIStateUncertain("completion 已送出但回應未確認原對話 ID")

        deadline = self._monotonic() + self._max_wait_seconds
        while True:
            current = self._get_chat(conversation_id, uncertain_on_failure=True)
            result = _turn_result(
                current,
                user_message_id,
                assistant_message_id,
                self._clarification_detector,
            )
            if result is not None:
                return result
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise OpenWebUIStateUncertain(
                    "assistant 回合尚未完成；保留原 operation 等待讀回結果"
                )
            self._sleep(min(self._poll_interval_seconds, remaining))

    def recover_turn(
        self, conversation_id: str, operation_id: str
    ) -> TurnResult | None:
        """只讀取原 message IDs；未完成或找不到時不回傳 None 觸發重送。"""

        chat = self._get_chat(conversation_id, uncertain_on_failure=True)
        result = _turn_result(
            chat,
            _message_id(operation_id, "user"),
            _message_id(operation_id, "assistant"),
            self._clarification_detector,
        )
        if result is None:
            raise OpenWebUIStateUncertain(
                "尚無可確認的 assistant 結果；保留待復原狀態，不重送回合"
            )
        return result

    def assert_retry_idle(self, conversation_id: str) -> None:
        """人工 Retry 前只查原生任務；未知或活躍時保留讀回，不重送。"""
        encoded_id = urllib.parse.quote(conversation_id, safe="")
        try:
            payload = self._request_json("GET", f"/api/tasks/chat/{encoded_id}")
        except Exception as exc:
            raise OpenWebUIStateUncertain("無法確認原生任務狀態，未重送") from exc
        if not isinstance(payload, dict) or not isinstance(
            payload.get("task_ids"), list
        ):
            raise OpenWebUIStateUncertain("原生任務清單格式未知，未重送")
        if payload["task_ids"]:
            raise OpenWebUIStateUncertain("原生任務仍執行，未重送")

    def _validate_model_snapshot(self, model_snapshot: Any) -> None:
        if not isinstance(model_snapshot, dict):
            return
        configured_id = model_snapshot.get("model_id", model_snapshot.get("model"))
        if configured_id is not None and configured_id != self._model_id:
            raise ValueError("runner model snapshot 與 adapter model ID 不一致")

    def get_model_snapshot(self) -> dict[str, Any]:
        """唯讀擷取既有模型 ID、版本時間與工具綁定，不複製其他設定。"""

        item, tool_ids = self._model_entry()
        info = item.get("info")
        updated_at = info.get("updated_at") if isinstance(info, dict) else None
        if updated_at is None:
            updated_at = item.get("updated_at")
        if isinstance(updated_at, float) and not math.isfinite(updated_at):
            updated_at = None
        elif isinstance(updated_at, bool) or not isinstance(
            updated_at, (str, int, float)
        ):
            updated_at = None
        return {
            "model_id": self._model_id,
            "model_name": item.get("name")
            if isinstance(item.get("name"), str)
            else None,
            "updated_at": updated_at,
            "tool_ids": tool_ids,
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def _model_tool_ids(self) -> list[str]:
        """讀取目前模型的工具綁定；聊天 API 不會自行套用 meta.toolIds。"""

        _item, tool_ids = self._model_entry()
        return tool_ids

    def _model_entry(self) -> tuple[dict[str, Any], list[str]]:
        payload = self._request_json("GET", "/api/models")
        models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise OpenWebUIClientError("Open WebUI 模型清單格式無效")
        matches = [
            item
            for item in models
            if isinstance(item, dict) and item.get("id") == self._model_id
        ]
        if len(matches) != 1:
            raise OpenWebUIClientError("找不到唯一的評測模型設定")
        item = matches[0]
        info = matches[0].get("info")
        meta = info.get("meta") if isinstance(info, dict) else None
        tool_ids = meta.get("toolIds", []) if isinstance(meta, dict) else []
        if not isinstance(tool_ids, list) or any(
            not isinstance(item, str) or not item for item in tool_ids
        ):
            raise OpenWebUIClientError("評測模型的 toolIds 格式無效")
        return item, tool_ids

    def _ensure_evaluation_run_folder(
        self, run_id: str, *, created_at: str, question_count: int
    ) -> str:
        root_id = self._ensure_folder(_EVALUATION_ROOT_FOLDER, parent_id=None)
        run_name = _evaluation_run_folder_name(
            run_id, created_at=created_at, question_count=question_count
        )
        return self._ensure_folder(run_name, parent_id=root_id)

    def _ensure_folder(self, name: str, *, parent_id: str | None) -> str:
        existing = self._find_folder(name, parent_id=parent_id)
        if existing is not None:
            return existing

        try:
            self._request_json(
                "POST",
                "/api/v1/folders/",
                body={"name": name, "parent_id": parent_id, "meta": {}},
                mutation=True,
            )
        except OpenWebUIClientError as original_error:
            try:
                recovered = self._find_folder(name, parent_id=parent_id)
            except Exception as recovery_error:
                if original_error.uncertain:
                    raise OpenWebUIStateUncertain(
                        "建立評測資料夾結果不明，且目前無法確認是否已建立"
                    ) from recovery_error
                raise original_error
            if recovered is not None:
                return recovered
            raise original_error

        try:
            created = self._find_folder(name, parent_id=parent_id)
        except Exception as verification_error:
            raise OpenWebUIStateUncertain(
                "評測資料夾建立後無法讀回確認；不建立未歸檔對話"
            ) from verification_error
        if created is None:
            raise OpenWebUIStateUncertain("評測資料夾建立後尚未讀回；不建立未歸檔對話")
        return created

    def _find_folder(self, name: str, *, parent_id: str | None) -> str | None:
        folders = self._request_json("GET", "/api/v1/folders/")
        if not isinstance(folders, list):
            raise OpenWebUIStateUncertain("Open WebUI 資料夾清單格式無法確認")

        matches: list[str] = []
        for item in folders:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not item.get("id")
                or not isinstance(item.get("name"), str)
            ):
                raise OpenWebUIStateUncertain(
                    "Open WebUI 資料夾清單含無法辨識項目；未建立對話"
                )
            if item.get("name") != name:
                continue
            folder_id = item.get("id")
            detail = self._get_folder(folder_id)
            if detail.get("name") != name or "parent_id" not in detail:
                raise OpenWebUIStateUncertain(
                    "Open WebUI 評測資料夾詳細資料格式無法確認"
                )
            if detail.get("parent_id") == parent_id:
                matches.append(folder_id)

        if len(matches) > 1:
            raise OpenWebUIStateUncertain(
                "找到多個同名評測資料夾，需人工判定；未建立對話"
            )
        return matches[0] if matches else None

    def _get_folder(self, folder_id: str) -> dict[str, Any]:
        encoded_id = urllib.parse.quote(folder_id, safe="")
        payload = self._request_json("GET", f"/api/v1/folders/{encoded_id}")
        if isinstance(payload, dict) and isinstance(payload.get("folder"), dict):
            payload = payload["folder"]
        if not isinstance(payload, dict) or payload.get("id") != folder_id:
            raise OpenWebUIStateUncertain("Open WebUI 評測資料夾讀回無法確認 ID")
        return payload

    def _find_conversation_in_folder(self, title: str, folder_id: str) -> str | None:
        seen_ids: set[str] = set()
        matches: list[str] = []
        for page in range(1, 101):
            query = urllib.parse.urlencode({"text": title, "page": page})
            results = self._request_json("GET", f"/api/v1/chats/search?{query}")
            if not isinstance(results, list):
                raise OpenWebUIStateUncertain("Open WebUI 評測對話搜尋回應格式無法確認")
            if not results:
                break
            page_new_ids: set[str] = set()
            for item in results:
                if not isinstance(item, dict):
                    raise OpenWebUIStateUncertain(
                        "Open WebUI 評測對話搜尋含無法辨識項目"
                    )
                result_id = item.get("id")
                if isinstance(result_id, str) and result_id:
                    if result_id not in seen_ids:
                        page_new_ids.add(result_id)
                    already_seen = result_id in seen_ids
                    seen_ids.add(result_id)
                else:
                    raise OpenWebUIStateUncertain("評測對話搜尋結果缺少 chat ID")
                if item.get("title") != title:
                    continue
                conversation_id = result_id
                if already_seen:
                    continue
                chat = self._get_chat(conversation_id, uncertain_on_failure=True)
                actual_folder_id = _chat_folder_id(chat)
                if not isinstance(actual_folder_id, str):
                    raise OpenWebUIStateUncertain(
                        "無法確認同名對話的資料夾歸屬；未建立重複對話"
                    )
                if actual_folder_id == folder_id:
                    matches.append(conversation_id)
            if not page_new_ids:
                raise OpenWebUIStateUncertain(
                    "評測對話搜尋分頁未前進；未建立可能重複的對話"
                )
        else:
            raise OpenWebUIStateUncertain(
                "評測對話搜尋超過安全頁數；未建立可能重複的對話"
            )

        if len(matches) > 1:
            raise OpenWebUIStateUncertain(
                "同一評測資料夾找到多個同名題目對話，需人工判定"
            )
        return matches[0] if matches else None

    def _verify_chat_folder(self, conversation_id: str, folder_id: str) -> None:
        chat = self._get_chat(conversation_id, uncertain_on_failure=True)
        if _chat_folder_id(chat) != folder_id:
            raise OpenWebUIStateUncertain(
                "新對話讀回後未確認位於本次評測資料夾；停止續跑"
            )

    def _find_conversation(self, title: str) -> str | None:
        query = urllib.parse.urlencode({"text": title, "page": 1})
        results = self._request_json("GET", f"/api/v1/chats/search?{query}")
        if not isinstance(results, list):
            raise OpenWebUIClientError(
                "Open WebUI 對話搜尋回應格式無效", uncertain=True
            )
        matches = [
            item
            for item in results
            if isinstance(item, dict) and item.get("title") == title
        ]
        if len(matches) > 1:
            raise OpenWebUIStateUncertain("找到多個相同評測標記的對話，需人工判定")
        if not matches:
            return None
        conversation_id = matches[0].get("id")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise OpenWebUIStateUncertain("評測對話搜尋結果缺少 chat ID")
        return conversation_id

    def _get_chat(
        self, conversation_id: str, *, uncertain_on_failure: bool
    ) -> dict[str, Any]:
        encoded_id = urllib.parse.quote(conversation_id, safe="")
        try:
            payload = self._request_json("GET", f"/api/v1/chats/{encoded_id}")
        except OpenWebUIClientError as exc:
            if uncertain_on_failure:
                raise OpenWebUIStateUncertain(
                    "Open WebUI 對話讀取失敗；回合完成狀態未知"
                ) from exc
            raise OpenWebUIClientError(
                "Open WebUI 對話讀取失敗，尚未送出回合", retryable=False
            ) from exc
        if not isinstance(payload, dict) or payload.get("id") != conversation_id:
            if uncertain_on_failure:
                raise OpenWebUIStateUncertain("Open WebUI 對話回應無法確認 chat ID")
            raise OpenWebUIClientError("Open WebUI 對話回應無法確認 chat ID")
        return payload

    def export_chat_html(
        self,
        conversation_id: str,
        *,
        theme: str = "auto",
        redact_value: Callable[[Any], Any] | None = None,
    ) -> str:
        """讀回既有 chat，交由安全的離線匯出器呈現 active branch。"""

        if (
            not isinstance(conversation_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", conversation_id) is None
        ):
            raise OpenWebUIClientError("評測對話 ID 格式無效")
        try:
            chat = self._get_chat(conversation_id, uncertain_on_failure=False)
        except OpenWebUIClientError:
            raise

        # 只遮罩對話資料；產生 HTML 後再遮罩會誤改固定 Plotly 腳本。
        if redact_value is not None:
            chat = redact_value(chat)

        try:
            from scripts.evaluation_report_html import fetch_plotly_javascript
            from scripts.export_chat_html import ChatExportError, build_chat_export_html

            document, _message_count, _chart_count, _warning_count = (
                build_chat_export_html(
                    chat,
                    theme=theme,
                    native_validation=False,
                    plotly_javascript_provider=fetch_plotly_javascript,
                )
            )
        except (ImportError, OSError):
            raise OpenWebUIClientError("原對話匯出元件目前不可用") from None
        except ChatExportError:
            raise OpenWebUIClientError("原對話內容無法安全呈現") from None
        if not isinstance(document, str):
            raise OpenWebUIClientError("原對話匯出結果格式無效")
        return document

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        mutation: bool = False,
    ) -> Any:
        encoded_body = (
            json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if body is not None
            else None
        )
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=encoded_body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with self._urlopen(request, timeout=self._timeout_seconds) as response:
                status = getattr(response, "status", 200)
                data = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            data = b""
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if mutation:
                raise OpenWebUIStateUncertain(
                    "Open WebUI mutation 連線中斷，遠端狀態未知"
                ) from exc
            raise OpenWebUIClientError(
                "Open WebUI 唯讀請求暫時失敗", retryable=True
            ) from exc

        if status < 200 or status >= 300:
            if mutation and status >= 500:
                raise OpenWebUIStateUncertain(
                    f"Open WebUI mutation 回傳 HTTP {status}，遠端狀態未知"
                )
            raise OpenWebUIClientError(
                f"Open WebUI API 回傳 HTTP {status}",
                retryable=status == 429 or status >= 500,
            )
        if len(data) > _MAX_RESPONSE_BYTES:
            if mutation:
                raise OpenWebUIStateUncertain(
                    "Open WebUI mutation 回應超過讀取上限，遠端狀態未知"
                )
            raise OpenWebUIClientError("Open WebUI API 回應超過讀取上限")
        if not data:
            return None
        try:
            return json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            if mutation:
                raise OpenWebUIStateUncertain(
                    "Open WebUI mutation 回應無法解析，遠端狀態未知"
                ) from exc
            raise OpenWebUIClientError("Open WebUI API 回應不是有效 JSON") from exc


def _validate_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("必須提供 Open WebUI base URL")
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
        raise ValueError("Open WebUI base URL 必須是無路徑的 HTTP(S) origin")
    return cleaned


def _conversation_title(run_id: str, question_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]
    return f"BadmintonAI Evaluation [{run_id}:{question_id}:{digest}]"


def _question_conversation_title(question_id: str) -> str:
    if not isinstance(question_id, str) or re.fullmatch(r"\d+", question_id) is None:
        raise ValueError("評測題目 ID 必須是數字")
    return f"Q{question_id}"


def _evaluation_run_folder_name(
    run_id: str, *, created_at: str, question_count: int
) -> str:
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("評測 run ID 無效")
    if (
        isinstance(question_count, bool)
        or not isinstance(question_count, int)
        or question_count < 0
    ):
        raise ValueError("評測題數無效")
    if not isinstance(created_at, str) or not created_at:
        raise ValueError("評測 run 建立時間無效")
    try:
        timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("評測 run 建立時間格式無效") from exc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    local_time = timestamp.astimezone(_TAIPEI_TIMEZONE)
    short_run_id = re.sub(r"[^A-Za-z0-9_-]", "", run_id)[:12]
    if not short_run_id:
        short_run_id = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
    return f"{local_time:%Y-%m-%d %H%M}（{question_count}題）-{short_run_id}"


def _chat_folder_id(chat: dict[str, Any]) -> Any:
    if "folder_id" in chat:
        return chat["folder_id"]
    inner_chat = chat.get("chat")
    return inner_chat.get("folder_id") if isinstance(inner_chat, dict) else None


def _message_id(operation_id: str, role: str) -> str:
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"badmintonai-eval:{role}:{operation_id}")
    )


def _history_messages(chat_response: dict[str, Any]) -> dict[str, Any]:
    chat = chat_response.get("chat")
    history = chat.get("history") if isinstance(chat, dict) else None
    messages = history.get("messages") if isinstance(history, dict) else None
    return messages if isinstance(messages, dict) else {}


def _current_assistant_id(chat_response: dict[str, Any]) -> str | None:
    chat = chat_response.get("chat")
    history = chat.get("history") if isinstance(chat, dict) else None
    if not isinstance(history, dict):
        raise OpenWebUIClientError("Open WebUI 對話缺少 history")
    messages = _history_messages(chat_response)
    current_id = history.get("currentId")
    if current_id is None and not messages:
        return None
    current = messages.get(current_id) if isinstance(current_id, str) else None
    visited: set[str] = set()
    while isinstance(current, dict) and isinstance(current_id, str):
        if current_id in visited:
            break
        visited.add(current_id)
        if current.get("role") == "assistant":
            if current.get("done") is True or current.get("error"):
                return current_id
            raise OpenWebUIStateUncertain(
                "上一個 assistant 回合尚未完成，不可開始新的回合"
            )
        parent_id = current.get("parentId")
        current_id = parent_id if isinstance(parent_id, str) else None
        current = messages.get(current_id) if current_id else None
    raise OpenWebUIStateUncertain(
        "無法確認目前對話分支的 assistant parent，不可另開分支"
    )


def _turn_result(
    chat_response: dict[str, Any],
    user_message_id: str,
    assistant_message_id: str,
    clarification_detector: Callable[[dict[str, Any]], bool],
) -> TurnResult | None:
    messages_by_id = _history_messages(chat_response)
    user_message = messages_by_id.get(user_message_id)
    assistant_message = messages_by_id.get(assistant_message_id)
    if not isinstance(assistant_message, dict):
        return None
    terminal = assistant_message.get("done") is True or bool(
        assistant_message.get("error")
    )
    if not terminal:
        return None

    result_messages = []
    if isinstance(user_message, dict):
        result_messages.append(user_message)
    result_messages.append(assistant_message)
    output = assistant_message.get("output")
    tool_calls = _extract_tool_calls(assistant_message, output)
    charts = _extract_chart_metadata(assistant_message)
    # 只採用持久化 assistant 訊息中的實際用量欄位；缺值保留 null，不估算。
    usage = assistant_message.get("usage")
    if usage is None:
        info = assistant_message.get("info")
        usage = info.get("usage") if isinstance(info, dict) else None
    if not isinstance(usage, dict):
        usage = None

    message_error = assistant_message.get("error")
    error = None
    if message_error:
        error_content = (
            (message_error.get("content") or message_error.get("message"))
            if isinstance(message_error, dict)
            else str(message_error)
        )
        error_payload = (
            error_content
            if isinstance(error_content, dict)
            else _decode_tool_result_payload(error_content)
            if isinstance(error_content, str)
            else None
        )
        if isinstance(error_payload, dict) and isinstance(
            error_payload.get("error"), dict
        ):
            error_payload = error_payload["error"]
        error_code = (
            error_payload.get("code") if isinstance(error_payload, dict) else None
        )
        if error_code in _BADMINTONAI_STREAM_ERRORS:
            error = {
                "code": error_code,
                "message": _BADMINTONAI_STREAM_ERRORS[error_code],
                "retryable": False,
            }
        else:
            error = {
                "message": str(error_content or "Open WebUI assistant message failed"),
                "retryable": False,
            }
    if error is None:
        error = _analysis_completion_error(tool_calls, output, charts)
    if error is None and any(
        call.get("status") in {"pending", "running", "in_progress"}
        for call in tool_calls
    ):
        error = {
            "message": "Open WebUI 回合已結束但仍有未完成的工具呼叫；需檢查原始對話",
            "retryable": False,
        }
    clarification_calls = [
        call for call in tool_calls if call.get("name") == "requestClarification"
    ]
    completed_clarification_calls = [
        call for call in clarification_calls if call.get("status") == "completed"
    ]
    text_candidate = (
        clarification_text_candidate(assistant_message)
        if clarification_detector is _looks_like_clarification
        else bool(clarification_detector(assistant_message))
    )
    if clarification_calls and not completed_clarification_calls:
        clarification_signal = "tool_failed"
        clarification_review_reason = (
            "requestClarification 事件未完成；需人工確認本輪是否應等待補答"
        )
    elif completed_clarification_calls and not any(
        _clarification_call_has_question(call) for call in completed_clarification_calls
    ):
        clarification_signal = "tool_failed"
        clarification_review_reason = (
            "requestClarification 事件已完成但缺少有效問題內容；需人工複核"
        )
    elif completed_clarification_calls:
        clarification_signal = "event"
        clarification_review_reason = None
    elif text_candidate:
        clarification_signal = "text_candidate"
        clarification_review_reason = (
            "文字看似追問但未保存 completed requestClarification 事件"
        )
    else:
        clarification_signal = "none"
        clarification_review_reason = None

    if clarification_signal == "tool_failed" and error is None:
        error = {
            "message": clarification_review_reason,
            "retryable": False,
        }
    if (
        error is None
        and clarification_signal != "event"
        and not charts
        and not _has_visible_answer(assistant_message, output)
    ):
        error = {
            "message": "Open WebUI 回合已結束但沒有可見答案；需檢查工具循環與原始對話",
            "retryable": False,
        }
    awaiting_clarification = clarification_signal in {"event", "text_candidate"}
    return TurnResult(
        messages=result_messages,
        tool_calls=tool_calls,
        charts=charts,
        usage=usage,
        awaiting_clarification=awaiting_clarification,
        clarification_signal=clarification_signal,
        clarification_review_reason=clarification_review_reason,
        error=error,
    )


def _analysis_completion_error(
    tool_calls: list[dict[str, Any]],
    output: Any,
    charts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """僅依工具狀態判定已知未完成；不猜測回答文字或語意正確性。"""

    fixed_turn_failure: dict[str, Any] | None = None
    if isinstance(output, list):
        for item in output:
            if (
                not isinstance(item, dict)
                or item.get("type") != "message"
                or item.get("role") != "assistant"
            ):
                continue
            fixed_failure = _FIXED_TOOL_TURN_FAILURES.get(
                _content_text(item.get("content")).strip()
            )
            if fixed_failure is not None:
                code, message = fixed_failure
                fixed_turn_failure = {
                    "code": code,
                    "message": message,
                    "retryable": False,
                }
                break

    analyses = [call for call in tool_calls if call.get("name") == "runPythonAnalysis"]
    renderers = [
        call for call in tool_calls if call.get("name") == "renderAnalysisChart"
    ]
    if not analyses and not renderers:
        return fixed_turn_failure

    analysis_outcome: str | None = None
    analysis_terminal_error: dict[str, Any] | None = None
    for call in analyses:
        call_id = call.get("call_id") or call.get("id")
        if _tool_was_explicitly_unexecuted(call, output):
            continue
        payloads = _tool_result_payloads(call_id, output)
        outcome: str | None = None
        for raw_text in payloads:
            payload = _decode_tool_result_payload(raw_text)
            if not isinstance(payload, dict):
                continue
            if isinstance(payload.get("code"), str) or payload.get("error"):
                outcome = "failure"
                code = payload.get("code")
                if isinstance(code, str) and code in _ANALYSIS_TERMINAL_CODES:
                    messages = {
                        "analysis_message_limit": "分析執行次數已達上限；本輪已停止，未完成部分不視為已驗證結論。",
                        "analysis_retry_limit": "分析修正次數已達上限；本輪已停止，未完成部分不視為已驗證結論。",
                        "analysis_terminated": "分析因基礎設施錯誤停止；不提供未驗證結論。",
                        "sandbox_timeout": "分析執行逾時並已停止；不提供未驗證結論。",
                        "sandbox_unavailable": "分析環境無法使用；本輪已停止，未完成部分不視為已驗證結論。",
                        "sandbox_execution_failure": "分析環境執行失敗並已停止；不提供未驗證結論。",
                    }
                    analysis_terminal_error = {
                        "code": code,
                        "message": messages[code],
                        "retryable": False,
                    }
            elif (
                isinstance(payload.get("result_id"), str)
                and bool(payload.get("result_id"))
                and isinstance(payload.get("artifacts"), list)
                and any(
                    isinstance(artifact, dict)
                    and isinstance(artifact.get("relative_path"), str)
                    and bool(artifact["relative_path"].strip())
                    for artifact in payload["artifacts"]
                )
            ):
                outcome = "success"
            elif (
                isinstance(payload.get("stdout_preview"), str)
                and bool(payload["stdout_preview"].strip())
                and payload.get("result_id") is None
                and payload.get("artifacts") == []
            ):
                outcome = "probe"
            elif "result_id" in payload or payload.get("artifacts"):
                # 宣稱有正式結果但沒有可辨識的已保存檔名，不能當成功。
                outcome = "failure"
        if call.get("status") == "failed" and outcome != "success":
            outcome = "failure"
        if outcome == "failure":
            analysis_outcome = "failure"
        elif outcome == "success":
            # 只有後續真正保存結果才清除失敗；stdout-only 探查不能洗掉失敗。
            analysis_outcome = None

    executed_renderers = [
        call for call in renderers if not _tool_was_explicitly_unexecuted(call, output)
    ]
    render_error = (
        _render_completion_error(executed_renderers, output)
        if executed_renderers
        else None
    )
    if render_error is not None:
        return render_error
    if analysis_terminal_error is not None:
        return analysis_terminal_error
    if fixed_turn_failure is not None:
        return fixed_turn_failure
    if analysis_outcome == "failure":
        return {
            "message": "Python 分析工具未成功完成；回答需要人工複核",
            "retryable": False,
        }
    if not isinstance(output, list):
        return None

    if not charts:
        for call in analyses:
            call_id = call.get("call_id") or call.get("id")
            for raw_text in _tool_result_payloads(call_id, output):
                payload = _decode_tool_result_payload(raw_text)
                status = (
                    payload.get("rich_ui_status") if isinstance(payload, dict) else None
                )
                if status == "embed_unknown":
                    return {
                        "message": "互動圖表附加狀態不明且未讀到圖表；需要人工複核",
                        "retryable": False,
                    }
                if status == "invalid_spec":
                    return {
                        "message": "互動圖表規格無效且未讀到圖表；需要人工複核",
                        "retryable": False,
                    }
                if status in {
                    "bridge_not_configured",
                    "chat_context_missing",
                    "identity_mismatch",
                    "embed_failed",
                }:
                    return {
                        "message": "互動圖表未成功附加且未讀到圖表；需要人工複核",
                        "retryable": False,
                    }
    return None


def _tool_was_explicitly_unexecuted(call: dict[str, Any], output: Any) -> bool:
    """只依 middleware 固定 failed 配對文案識別未執行呼叫。"""

    call_id = call.get("call_id") or call.get("id")
    if (
        call.get("status") != "failed"
        or not isinstance(call_id, str)
        or not isinstance(output, list)
    ):
        return False
    for item in output:
        if (
            not isinstance(item, dict)
            or item.get("type") != "function_call_output"
            or item.get("call_id") != call_id
            or item.get("status") != "failed"
        ):
            continue
        if _tool_event_contract.is_unexecuted_tool_output(item, call_id):
            return True
        blocks = item.get("output")
        texts = (
            [blocks]
            if isinstance(blocks, str)
            else [
                block.get("text")
                for block in blocks
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ]
            if isinstance(blocks, list)
            else []
        )
        if any(
            text.strip() in _UNEXECUTED_TOOL_OUTPUTS
            for text in texts
            if isinstance(text, str)
        ):
            return True
    return False


def _tool_result_payloads(call_id: Any, output: Any) -> list[str]:
    if not isinstance(call_id, str) or not call_id or not isinstance(output, list):
        return []
    texts: list[str] = []
    for item in output:
        if (
            not isinstance(item, dict)
            or item.get("type") not in {"function_call_output", "tool_result"}
            or item.get("call_id") != call_id
        ):
            continue
        blocks = item.get("output")
        if isinstance(blocks, str):
            texts.append(blocks)
        elif isinstance(blocks, list):
            texts.extend(
                block["text"]
                for block in blocks
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
    return texts


def _decode_tool_result_payload(text: str) -> dict[str, Any] | None:
    """相容舊呼叫名稱；依 middleware 共用的有界 envelope 解碼契約。"""

    return _tool_event_contract.decode_tool_result_payload(text)


def _render_completion_error(
    renderers: list[dict[str, Any]], output: Any
) -> dict[str, Any] | None:
    """採最後一次可修正 render 結果；未知或終止狀態不可被後續工具回應洗掉。"""

    if not isinstance(output, list):
        return {
            "message": "互動圖表工具結果未能確認；回答需要人工複核",
            "retryable": False,
        }

    last_success = False
    unknown_codes = {"chart_state_unknown", "render_state_unavailable"}
    terminal_codes = {
        "chart_state_unknown",
        "render_retry_limit",
        "render_terminated",
        "render_state_unavailable",
        "chart_bridge_unavailable",
        "chart_identity_mismatch",
        "chart_embed_failed",
    }
    for call in renderers:
        call_id = call.get("call_id") or call.get("id")
        payloads = _tool_result_payloads(call_id, output)
        outcome: str | None = None
        terminal = False
        terminal_code: str | None = None
        for text in payloads:
            payload = _decode_tool_result_payload(text)
            if not isinstance(payload, dict):
                continue
            if payload.get("status") in {"embedded", "duplicate_suppressed"}:
                outcome = "success"
                continue
            code = payload.get("code")
            if isinstance(code, str):
                outcome = "failure"
                details = payload.get("details")
                terminal = (
                    terminal
                    or (isinstance(details, dict) and details.get("terminal") is True)
                    or code in terminal_codes
                )
                if terminal:
                    terminal_code = code
        if call.get("status") == "failed" and outcome is None:
            outcome = "failure"
        if terminal:
            if terminal_code not in unknown_codes:
                return {
                    "code": "render_failed",
                    "message": "互動圖表未能完成發布；已停止自動修正，請查看原對話中的既有結果。",
                    "retryable": False,
                }
            return {
                "message": "互動圖表的發布狀態無法確認；請先查看原對話是否已有圖表，避免重複發布。",
                "retryable": False,
            }
        if outcome is None:
            # 缺少可核對的回覆屬未知狀態；後續工具結果不能洗掉這個不確定性。
            return {
                "message": "互動圖表工具結果未能確認；回答需要人工複核",
                "retryable": False,
            }
        last_success = outcome == "success"

    if last_success:
        return None
    return {
        "code": "render_failed",
        "message": "互動圖表未能完成發布；已停止自動修正，請查看原對話中的既有結果。",
        "retryable": False,
    }


def _extract_tool_calls(
    assistant_message: dict[str, Any], output: Any
) -> list[dict[str, Any]]:
    calls = assistant_message.get("tool_calls")
    result = (
        [item for item in calls if isinstance(item, dict)]
        if isinstance(calls, list)
        else []
    )
    if isinstance(output, list):
        result.extend(
            item
            for item in output
            if isinstance(item, dict)
            and (
                item.get("type") in _TOOL_CALL_TYPES
                or (
                    item.get("type") not in {"function_call_output", "tool_result"}
                    and item.get("tool_name") is not None
                )
            )
        )
    return result


def _extract_chart_metadata(assistant_message: dict[str, Any]) -> list[dict[str, Any]]:
    embeds = assistant_message.get("embeds")
    if embeds is None:
        metadata = assistant_message.get("metadata")
        embeds = metadata.get("embeds") if isinstance(metadata, dict) else None
    charts: list[dict[str, Any]] = []
    if isinstance(embeds, list):
        for index, embed in enumerate(embeds):
            if isinstance(embed, str):
                encoded = embed.encode("utf-8")
                item: dict[str, Any] = {
                    "index": index,
                    "mime_type": "text/html",
                    "bytes": len(encoded),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                }
                fingerprints = _CHART_FINGERPRINT.findall(embed)
                if len(fingerprints) == 1:
                    item["fingerprint"] = fingerprints[0]
                charts.append(item)
            elif isinstance(embed, dict):
                charts.append({"index": index, "metadata": embed})

    files = assistant_message.get("files")
    if isinstance(files, list):
        for file_item in files:
            if not isinstance(file_item, dict):
                continue
            name = str(file_item.get("name") or "").casefold()
            if name.startswith("badminton-chart-") or file_item.get("kind") == "chart":
                charts.append({"type": "file", "metadata": file_item})
    return charts


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            item.get("text", "")
            for item in value
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


def _has_visible_answer(assistant_message: dict[str, Any], output: Any) -> bool:
    """避免把工具循環中斷後的空白 assistant 訊息誤當完成回答。"""

    if _content_text(assistant_message.get("content")).strip():
        return True
    if not isinstance(output, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("type") == "message"
        and _content_text(item.get("content")).strip()
        for item in output
    )


def _looks_like_clarification(message: dict[str, Any]) -> bool:
    """優先使用已保存的明確工具事件；文字僅相容舊對話。"""

    if message.get("awaiting_clarification") is True:
        return True
    tool_calls = _extract_tool_calls(message, message.get("output"))
    if any(
        call.get("name") == "requestClarification" and call.get("status") == "completed"
        for call in tool_calls
    ):
        return True
    return clarification_text_candidate(message)


def clarification_text_candidate(message: dict[str, Any]) -> bool:
    """保留舊 checkpoint 相容線索；新 run 不以文字線索自動等待補答。"""

    tool_calls = _extract_tool_calls(message, message.get("output"))
    # 舊回合沒有結構化標記時才保留文字判定；已做分析的回答即使末尾
    # 邀請進一步確認，也不可直接當成尚未作答。
    if any(
        call.get("name") == "runPythonAnalysis" and call.get("status") == "completed"
        for call in tool_calls
    ):
        return False
    text = _content_text(message.get("content")).strip()
    if not text:
        return False
    # 引述或 Markdown 引言中的問題不是模型正在向使用者提出的澄清。
    text = _MARKDOWN_QUOTE_LINE.sub("", text)
    text = _QUOTED_SPAN.sub("", text)
    if not text.strip():
        return False
    if _DIRECT_CLARIFICATION.search(text):
        return True

    # 有多個明確選項時，接受不帶問號的「請擇一」指示；單純條列或反問不算。
    choice_count = len(_CHOICE_LINE.findall(text))
    return choice_count >= 2 and bool(_CHOICE_REQUEST.search(text))


def _clarification_call_has_question(call: dict[str, Any]) -> bool:
    arguments = call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return False
    return (
        isinstance(arguments, dict)
        and isinstance(arguments.get("question"), str)
        and bool(arguments["question"].strip())
    )


__all__ = [
    "OpenWebUIClientError",
    "OpenWebUIEvaluationClient",
    "OpenWebUIStateUncertain",
    "clarification_text_candidate",
]
