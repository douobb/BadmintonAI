"""Open WebUI 評測回合的安全階段紀錄工具。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any

_SAFE_LOG_ID_PATTERNS = (
    re.compile(r"[0-9a-f]{24}\Z", re.IGNORECASE),
    re.compile(r"[0-9a-f]{32}\Z", re.IGNORECASE),
    re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z", re.IGNORECASE),
    re.compile(r"[0-9a-f]{32}:[0-9]{1,6}\Z", re.IGNORECASE),
)
_SAFE_OUTCOMES = frozenset({"success", "error", "failed", "uncertain", "timeout"})
_SAVED_RESULT_ID_PATTERN = re.compile(r"[a-f0-9]{48}\Z")
_RESULT_FINGERPRINT_PATTERN = re.compile(r"[a-f0-9]{64}\Z")
_ARTIFACT_COMPONENT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_MAX_TOOL_PAYLOAD_CHARS = 64 * 1024
_NATIVE_TOOL_RESPONSE_NAMES = frozenset(
    {"requestClarification", "runPythonAnalysis", "renderAnalysisChart", "readAnalysisResult"}
)
_UNEXECUTED_TOOL_REASONS = frozenset(
    {
        "stopped",
        "turn_error",
        "no_progress_final",
        "clarification",
        "terminal_tool",
        "tool_iteration_limit",
        "analysis_budget_reserved",
        "stream_failure",
    }
)
_BADMINTONAI_STREAM_FAILURE_CODES = frozenset(
    {
        "stream_no_data_timeout",
        "stream_no_progress_timeout",
        "stream_total_timeout",
        "stream_upstream_error",
        "stream_incomplete",
    }
)
_BADMINTONAI_STREAM_FAILURE_MESSAGES = {
    "stream_no_data_timeout": "模型服務串流等待資料逾時；本輪已停止，未完成輸出不視為完成。",
    "stream_no_progress_timeout": "模型服務串流未持續產生回答或工具參數；本輪已停止，未完成部分不視為已驗證。",
    "stream_total_timeout": "模型服務串流達到總等待上限；本輪已停止，未完成輸出不視為完成。",
    "stream_upstream_error": "模型服務串流中斷；本輪已停止，未完成輸出不視為完成。",
    "stream_incomplete": "模型服務串流未正常結束；本輪已停止，未完成輸出不視為完成。",
}
_MAX_STREAM_PROGRESS_LINE_BYTES = 1024 * 1024
TOOL_ITERATION_LIMIT_MESSAGE = (
    "本輪已達工具呼叫上限，尚未完成；不提供未驗證的結論或數值。"
)
NO_PROGRESS_FINAL_PROMPT = (
    "請只根據本回合已保存且可驗證的分析結果及已發布圖表回答原問題。"
    "若證據不足或只完成部分，明確說明未完成部分；不要編造數值或呼叫工具。"
)
NO_PROGRESS_FINAL_FAILURE_MESSAGE = (
    "本輪收尾未能產生可靠的最終答覆；已保存結果與已發布圖表仍保留，"
    "不提供未驗證的結論或數值。"
)
NO_PROGRESS_FINAL_TOOL_INTENT_MESSAGE = (
    "已停止重複呼叫；收尾仍要求工具，本輪未執行該呼叫，未完成部分不提供未驗證結論。"
)
ANALYSIS_BUDGET_EXHAUSTED_MESSAGE = (
    "本則回答的分析執行次數已用完，且仍有未解錯誤或沒有可驗證的保存結果；"
    "分析未完成，不提供未驗證數值。"
)
ANALYSIS_BUDGET_RESERVED_MESSAGE = (
    "分析執行額度已保留給本輪收尾；此分析呼叫未執行。"
)


def decode_tool_payload(value: Any) -> dict[str, Any] | None:
    """以共用 64 KiB 上限解碼工具 JSON／HTTP error envelope。"""

    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or len(value) > _MAX_TOOL_PAYLOAD_CHARS:
        return None
    candidate = value.strip()
    if candidate.startswith("HTTP error "):
        _, separator, candidate = candidate.partition(":")
        if not separator:
            return None
        candidate = candidate.strip()
    try:
        payload = json.loads(candidate)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def decode_tool_result_payload(value: Any) -> dict[str, Any] | None:
    """解開有界的巢狀 JSON／HTTP envelope，不替呼叫端判定成功。"""

    payload = decode_tool_payload(value)
    if payload is None:
        return None
    nested = payload.get("error")
    if isinstance(nested, str) and nested.startswith("HTTP error "):
        return decode_tool_result_payload(nested) or payload
    if isinstance(nested, dict):
        if isinstance(nested.get("code"), str):
            return nested
        message = nested.get("message")
        if isinstance(message, str) and message.startswith("HTTP error "):
            return decode_tool_result_payload(message) or payload
    return payload


def decode_tool_error_payload(value: Any) -> dict[str, Any] | None:
    """相容錯誤解析呼叫端，共用同一有界 result-envelope 解碼。"""

    return decode_tool_result_payload(value)


def is_unexecuted_tool_output(item: Any, call_id: Any) -> bool:
    """辨認新紀錄的結構化未執行配對，不依賴可見文案。"""

    if (
        not isinstance(item, dict)
        or item.get("type") != "function_call_output"
        or item.get("status") != "failed"
        or not isinstance(call_id, str)
        or item.get("call_id") != call_id
    ):
        return False
    event = item.get("evaluation_event")
    return (
        isinstance(event, dict)
        and event.get("type") == "tool_not_executed"
        and event.get("reason") in _UNEXECUTED_TOOL_REASONS
    )


def _native_tool_response_payload(tool_name: Any, tool_result: Any) -> Any:
    """只解開固定版 external-tool 的 (response_data, response_headers) 結果。"""

    if (
        tool_name in _NATIVE_TOOL_RESPONSE_NAMES
        and isinstance(tool_result, tuple)
        and len(tool_result) == 2
    ):
        response_data, response_headers = tool_result
        if isinstance(response_data, dict) and (
            response_headers is None or isinstance(response_headers, Mapping)
        ):
            return response_data
    return tool_result


def _decoded_native_tool_payload(tool_name: Any, tool_result: Any) -> dict[str, Any] | None:
    payload = _native_tool_response_payload(tool_name, tool_result)
    return decode_tool_payload(payload)


def _valid_saved_result_payload(payload: Any) -> tuple[str, str] | None:
    """只將帶 opaque ID、server fingerprint 與有效 manifest 的正式結果當證據。"""

    if not isinstance(payload, dict):
        return None
    result_id = payload.get("result_id")
    fingerprint = payload.get("result_fingerprint")
    artifacts = payload.get("artifacts")
    if (
        not isinstance(result_id, str)
        or _SAVED_RESULT_ID_PATTERN.fullmatch(result_id) is None
        or not isinstance(fingerprint, str)
        or _RESULT_FINGERPRINT_PATTERN.fullmatch(fingerprint) is None
        or not isinstance(artifacts, list)
        or not 1 <= len(artifacts) <= 16
    ):
        return None
    for artifact in artifacts:
        relative_path = artifact.get("relative_path") if isinstance(artifact, dict) else None
        if not isinstance(relative_path, str) or relative_path.startswith("/") or "\\" in relative_path:
            return None
        components = relative_path.split("/")
        if any(
            component in {"", ".", ".."}
            or _ARTIFACT_COMPONENT_PATTERN.fullmatch(component) is None
            for component in components
        ):
            return None
        if components[-1].rsplit(".", 1)[-1].casefold() not in {"csv", "json", "jsonl"}:
            return None
    return result_id, fingerprint


def _native_tool_result_is_error(tool_name: Any, tool_result: Any) -> bool:
    payload = _native_tool_response_payload(tool_name, tool_result)
    if isinstance(payload, str) and payload.lstrip().startswith("HTTP error "):
        return True
    decoded = _decode_tool_error_payload(payload)
    return isinstance(decoded, dict) and (
        isinstance(decoded.get("error"), (str, dict))
        or isinstance(decoded.get("code"), str)
    )


def _successful_request_clarification_payload(
    tool_name: Any, tool_result: Any
) -> dict[str, Any] | None:
    """只認可完整結構化澄清 response，不猜測 assistant 文字。"""
    if tool_name != "requestClarification":
        return None
    payload = _native_tool_response_payload(tool_name, tool_result)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict) or payload.get("status") != "awaiting_clarification":
        return None
    question = payload.get("question")
    options = payload.get("options")
    if not (
        isinstance(question, str)
        and bool(question.strip())
        and isinstance(options, list)
        and len(options) <= 3
        and all(isinstance(option, str) and option.strip() for option in options)
    ):
        return None
    return payload


def is_successful_request_clarification_result(
    tool_name: Any, tool_result: Any
) -> bool:
    """只認可 tool 回傳的完整澄清 response，不猜測 assistant 文字。"""

    return _successful_request_clarification_payload(tool_name, tool_result) is not None


def format_request_clarification_message(tool_name: Any, tool_result: Any) -> str | None:
    """將有效澄清回覆轉成原生聊天室可見的簡短 Markdown。"""

    payload = _successful_request_clarification_payload(tool_name, tool_result)
    if payload is None:
        return None
    lines = [payload["question"].strip()]
    options = [option.strip() for option in payload["options"]]
    if options:
        lines.extend(("", *(f"{index}. {option}" for index, option in enumerate(options, 1))))
    else:
        lines.extend(("", "請直接輸入補充內容。"))
    return "\n".join(lines)


class EvaluationToolBatchGate:
    """在 Open WebUI 逐一 await 工具時，即時停止同批後續呼叫。"""

    def __init__(self, progress_guard: Any = None) -> None:
        self.progress_guard = progress_guard
        self.clarification_message: str | None = None
        self.terminal_message: str | None = None
        self.unexecuted_calls: list[Any] = []
        self.budget_unexecuted_calls: list[Any] = []
        self._unexecuted_call_ids: set[int] = set()
        self._budget_unexecuted_call_ids: set[int] = set()
        self.analysis_budget_reserved = False
        self.executed_render_attempt = False

    @property
    def stopped(self) -> bool:
        return (
            self.clarification_message is not None
            or self.terminal_message is not None
        )

    def should_execute(self, tool_call: Any) -> bool:
        """終止後停止全部呼叫；分析額度用完時只保留其他工具。"""

        call_id = id(tool_call)
        if self.stopped:
            if call_id not in self._unexecuted_call_ids:
                self._unexecuted_call_ids.add(call_id)
                self.unexecuted_calls.append(tool_call)
            return False

        name = (
            tool_call.get("function", {}).get("name", "")
            if isinstance(tool_call, dict)
            else ""
        )
        if (
            name == "runPythonAnalysis"
            and self.progress_guard is not None
            and self.progress_guard.analysis_budget_exhausted
        ):
            if call_id not in self._budget_unexecuted_call_ids:
                self._budget_unexecuted_call_ids.add(call_id)
                self.budget_unexecuted_calls.append(tool_call)
            self.analysis_budget_reserved = True
            return False
        return True

    def was_unexecuted(self, tool_call: Any) -> bool:
        call_id = id(tool_call)
        return (
            call_id in self._unexecuted_call_ids
            or call_id in self._budget_unexecuted_call_ids
        )

    def observe_result(self, tool_name: Any, tool_result: Any) -> None:
        """只檢查已執行工具的結構化結果，不解析一般回答文字。"""

        if tool_name == "renderAnalysisChart":
            self.executed_render_attempt = True
        if self.stopped:
            return
        self.clarification_message = format_request_clarification_message(
            tool_name, tool_result
        )
        if self.clarification_message is None:
            self.terminal_message = terminal_tool_completion_message(
                tool_name, tool_result
            )


class EvaluationToolProgressGuard:
    """以同一 turn 內的精確保存結果或已發布圖表狀態收斂無進展循環。"""

    def __init__(self) -> None:
        self._last_published_result_id: str | None = None
        self.duplicate_streak = 0
        self._last_analysis_fingerprint: str | None = None
        self.analysis_repeat_streak = 0
        self._last_read_segment: tuple[Any, ...] | None = None
        self.read_repeat_streak = 0
        self.has_verified_result = False
        self.analysis_runs_remaining: int | None = None
        self.analysis_failure_unresolved = False
        self.render_failure_unresolved = False
        self.final_attempted = False

    def observe_result(self, tool_name: Any, tool_result: Any) -> None:
        """錯誤、探查及不同工具不會算成正式分析的重複結果。"""

        payload = _decoded_native_tool_payload(tool_name, tool_result)
        read_segment = None
        if tool_name == "readAnalysisResult" and not _native_tool_result_is_error(tool_name, tool_result):
            read_segment = _successful_read_segment(payload)
        if read_segment is not None:
            self.read_repeat_streak = self.read_repeat_streak + 1 if read_segment == self._last_read_segment else 1
        else:
            self.read_repeat_streak = 0
        self._last_read_segment = read_segment
        if tool_name == "runPythonAnalysis":
            is_error = _native_tool_result_is_error(tool_name, tool_result)
            budget_payload = payload
            if is_error:
                # 原生 external tool 會把 HTTP 錯誤 JSON 包在 error 字串中；
                # 額度只從既有有界錯誤解碼器辨識出的本文讀取。
                budget_payload = _decode_tool_error_payload(
                    _native_tool_response_payload(tool_name, tool_result)
                )
            details = budget_payload.get("details") if isinstance(budget_payload, dict) else None
            remaining = budget_payload.get("analysis_runs_remaining") if isinstance(budget_payload, dict) else None
            if remaining is None and isinstance(details, dict):
                remaining = details.get("analysis_runs_remaining")
            if (
                isinstance(remaining, int)
                and not isinstance(remaining, bool)
                and 0 <= remaining <= 12
            ):
                self.analysis_runs_remaining = remaining
            self._last_published_result_id = None
            self.duplicate_streak = 0
            # 即使錯誤本文意外帶有同名欄位，也不可把它當成保存成功。
            saved_result = None if is_error else _valid_saved_result_payload(payload)
            if saved_result is None:
                self._last_analysis_fingerprint = None
                self.analysis_repeat_streak = 0
                if is_error:
                    self.analysis_failure_unresolved = True
                return

            _, fingerprint = saved_result
            self.analysis_failure_unresolved = False
            self.has_verified_result = True
            if fingerprint == self._last_analysis_fingerprint:
                self.analysis_repeat_streak += 1
            else:
                self._last_analysis_fingerprint = fingerprint
                self.analysis_repeat_streak = 1
            return

        self._last_analysis_fingerprint = None
        self.analysis_repeat_streak = 0
        if tool_name != "renderAnalysisChart":
            self._last_published_result_id = None
            self.duplicate_streak = 0
            return

        published_result_id = payload.get("result_id") if isinstance(payload, dict) else None
        chart_count = payload.get("chart_count") if isinstance(payload, dict) else None
        valid_chart_state = (
            isinstance(payload, dict)
            and payload.get("status") in {"embedded", "duplicate_suppressed"}
            and isinstance(published_result_id, str)
            and _SAVED_RESULT_ID_PATTERN.fullmatch(published_result_id) is not None
            and isinstance(chart_count, int)
            and not isinstance(chart_count, bool)
            and 1 <= chart_count <= 4
        )
        if valid_chart_state:
            self.has_verified_result = True
            self.render_failure_unresolved = False
        elif _native_tool_result_is_error(tool_name, tool_result):
            self.render_failure_unresolved = True

        if published_result_id == self._last_published_result_id:
            if valid_chart_state and payload.get("status") == "duplicate_suppressed":
                self.duplicate_streak += 1
            else:
                self._last_published_result_id = None
                self.duplicate_streak = 0
        else:
            if valid_chart_state and payload.get("status") == "duplicate_suppressed":
                self._last_published_result_id = published_result_id
                self.duplicate_streak = 1
            else:
                self._last_published_result_id = None
                self.duplicate_streak = 0

    def should_force_final(self, *, near_limit: bool = False) -> bool:
        """僅對精確重複，或接近上限且有保存證據的回合收尾一次。"""

        if self.final_attempted or self.analysis_failure_unresolved or self.render_failure_unresolved:
            return False
        repeated_render = self.duplicate_streak >= 2
        repeated_analysis = self.analysis_repeat_streak >= 2
        repeated_read = self.read_repeat_streak >= 3
        safe_near_limit = (
            (near_limit or self.analysis_budget_exhausted)
            and self.has_verified_result
        )
        return repeated_render or repeated_analysis or repeated_read or safe_near_limit

    @property
    def analysis_budget_exhausted(self) -> bool:
        return self.analysis_runs_remaining == 0

    def mark_final_attempted(self) -> None:
        self.final_attempted = True


def _successful_read_segment(payload: Any) -> tuple[Any, ...] | None:
    """只辨識 server 回傳的合法單檔分段成功，不把錯誤本文算進展。"""

    if not isinstance(payload, dict) or "error" in payload or "code" in payload:
        return None
    result_id = payload.get("result_id")
    artifacts = payload.get("artifacts")
    if not isinstance(result_id, str) or _SAVED_RESULT_ID_PATTERN.fullmatch(result_id) is None:
        return None
    if not isinstance(artifacts, list) or len(artifacts) != 1 or not isinstance(artifacts[0], dict):
        return None
    artifact = artifacts[0]
    path, text = artifact.get("relative_path"), artifact.get("text_preview")
    offset, next_offset, size = (artifact.get(key) for key in ("preview_offset_bytes", "next_offset_bytes", "size_bytes"))
    if not isinstance(path, str) or not path or not isinstance(text, str):
        return None
    if any(not isinstance(value, int) or isinstance(value, bool) for value in (offset, next_offset, size)):
        return None
    try:
        content = text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if not 0 <= offset <= next_offset <= size or next_offset - offset != len(content) or len(content) > 4096:
        return None
    if artifact.get("has_more") is not (next_offset < size):
        return None
    return result_id, path, offset, next_offset, size, hashlib.sha256(content).digest()


def decide_tool_batch_completion(
    *,
    clarification_message: Any,
    terminal_message: Any,
    pending_tool_calls: Any,
    progress_guard: EvaluationToolProgressGuard,
    near_iteration_limit: bool,
    render_attempted: bool,
) -> str:
    """集中整批完成優先序；不改工具額度、重試或已發布圖表規則。"""

    if clarification_message is not None:
        return "clarification"
    if terminal_message is not None:
        return "terminal"
    if pending_tool_calls:
        return "continue"
    if progress_guard.should_force_final(
        near_limit=near_iteration_limit or progress_guard.analysis_budget_exhausted
    ):
        return "safe_final"
    if progress_guard.analysis_budget_exhausted and not (
        progress_guard.render_failure_unresolved and render_attempted
    ):
        return "analysis_budget_failed"
    return "continue"


def has_new_assistant_text(output: Any, previous_message_ids: Any) -> bool:
    """只認收尾模型新產生的可見文字，不把先前部分輸出當成收尾成功。"""

    previous_ids = (
        previous_message_ids if isinstance(previous_message_ids, set) else set()
    )
    if not isinstance(output, list):
        return False
    for item in output:
        if (
            not isinstance(item, dict)
            or item.get("type") != "message"
            or item.get("role") != "assistant"
            or item.get("id") in previous_ids
        ):
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            return True
        if isinstance(content, list) and any(
            isinstance(part, dict)
            and isinstance(part.get("text"), str)
            and part["text"].strip()
            for part in content
        ):
            return True
    return False


def finish_no_progress_final_failure(
    output: Any,
    pending_batches: Any,
    *,
    message: str,
    message_id_factory: Callable[[], str],
    result_id_factory: Callable[[], str],
) -> bool:
    """收尾失敗時保留既有內容、配對未執行工具並留可見失敗訊息。"""

    if not isinstance(output, list):
        return False
    terminalize_pending_tool_calls(
        output,
        pending_batches,
        reason="無進展收尾已停止；此工具呼叫未執行。",
        id_factory=result_id_factory,
        event_reason="no_progress_final",
    )
    if isinstance(pending_batches, list):
        pending_batches.clear()
    for item in output:
        if isinstance(item, dict) and item.get("status") == "in_progress":
            item["status"] = "failed"
    return append_request_clarification_message(
        output,
        message,
        id_factory=message_id_factory,
    ) is not None


def append_request_clarification_message(
    output: Any,
    text: Any,
    *,
    id_factory: Callable[[], str],
) -> dict[str, Any] | None:
    """將完整澄清文字保存成 Open WebUI 原生 assistant message item。"""

    if not isinstance(output, list) or not isinstance(text, str) or not text.strip():
        return None
    item = {
        "type": "message",
        "id": id_factory(),
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    output.append(item)
    return item


def finish_successful_request_clarification(
    output: Any,
    text: Any,
    pending_batches: Any,
    *,
    message_id_factory: Callable[[], str],
    result_id_factory: Callable[[], str],
) -> bool:
    """保存可見澄清訊息並終結未執行批次；True 代表呼叫端應停止續跑。"""

    if append_request_clarification_message(
        output,
        text,
        id_factory=message_id_factory,
    ) is None:
        return False
    terminalize_pending_tool_calls(
        output,
        pending_batches,
        reason="澄清已提交；本輪其餘工具呼叫未執行。",
        id_factory=result_id_factory,
        event_reason="clarification",
    )
    if isinstance(pending_batches, list):
        pending_batches.clear()
    return True


def terminal_tool_completion_message(tool_name: Any, tool_result: Any) -> str | None:
    """只對明確終止的分析／繪圖錯誤回傳固定可見訊息。"""

    if tool_name not in {"runPythonAnalysis", "renderAnalysisChart"}:
        return None
    payload = _decode_tool_error_payload(
        _native_tool_response_payload(tool_name, tool_result)
    )
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    details = payload.get("details")
    terminal = isinstance(details, dict) and details.get("terminal") is True
    terminal_codes = {
        "analysis_retry_limit",
        "analysis_terminated",
        "analysis_message_limit",
        "sandbox_timeout",
        "sandbox_unavailable",
        "sandbox_execution_failure",
        "render_retry_limit",
        "render_terminated",
        "render_state_unavailable",
        "chart_state_unknown",
        "chart_bridge_unavailable",
        "chart_identity_mismatch",
        "chart_embed_failed",
    }
    if not terminal and code not in terminal_codes:
        return None
    if code in {"chart_state_unknown", "render_state_unavailable"}:
        return "互動圖表的發布狀態無法確認；請先查看原對話是否已有圖表，避免重複發布。"
    if isinstance(code, str) and (
        code.startswith("render_") or code.startswith("chart_")
    ):
        return "互動圖表未能完成發布，已停止自動修正。"
    return "分析未完成；已停止自動修正，本次不提供未驗證的統計數值。"


def finish_terminal_tool_turn(
    output: Any,
    text: Any,
    pending_batches: Any,
    *,
    message_id_factory: Callable[[], str],
    result_id_factory: Callable[[], str],
) -> bool:
    """將明確終止錯誤轉成可見答覆並結束未執行工具批次。"""

    if append_request_clarification_message(
        output,
        text,
        id_factory=message_id_factory,
    ) is None:
        return False
    terminalize_pending_tool_calls(
        output,
        pending_batches,
        reason="本輪已因工具終止狀態停止；工具呼叫未執行。",
        id_factory=result_id_factory,
        event_reason="terminal_tool",
    )
    if isinstance(pending_batches, list):
        pending_batches.clear()
    return True


def finish_tool_iteration_limit_turn(
    output: Any,
    pending_batches: Any,
    *,
    message_id_factory: Callable[[], str],
    result_id_factory: Callable[[], str],
) -> bool:
    """保留既有文字並以失敗狀態收尾迭代上限，避免空白或假成功。"""

    if not isinstance(output, list):
        return False
    terminalize_pending_tool_calls(
        output,
        pending_batches,
        reason="工具呼叫因達到本輪上限而未執行。",
        id_factory=result_id_factory,
        event_reason="tool_iteration_limit",
    )
    if isinstance(pending_batches, list):
        pending_batches.clear()

    has_visible_assistant_text = any(
        isinstance(item, dict)
        and item.get("type") == "message"
        and item.get("role") == "assistant"
        and (
            isinstance(item.get("content"), str)
            and item["content"].strip()
            or isinstance(item.get("content"), list)
            and any(
                isinstance(part, dict)
                and isinstance(part.get("text"), str)
                and part["text"].strip()
                for part in item["content"]
            )
        )
        for item in output
    )
    for item in output:
        if isinstance(item, dict) and item.get("status") == "in_progress":
            item["status"] = "failed"

    if not has_visible_assistant_text:
        placeholder = next(
            (
                item
                for item in reversed(output)
                if isinstance(item, dict)
                and item.get("type") == "message"
                and item.get("role") == "assistant"
            ),
            None,
        )
        if placeholder is not None:
            placeholder["content"] = [
                {"type": "output_text", "text": TOOL_ITERATION_LIMIT_MESSAGE}
            ]
            placeholder["status"] = "completed"
        else:
            append_request_clarification_message(
                output,
                TOOL_ITERATION_LIMIT_MESSAGE,
                id_factory=message_id_factory,
            )
    return True


def _decode_tool_error_payload(value: Any) -> dict[str, Any] | None:
    """相容舊呼叫名稱；共用解碼契約集中於公開 helper。"""

    return decode_tool_error_payload(value)


def terminalize_dangling_function_calls(
    output: Any,
    *,
    reason: str,
    id_factory: Callable[[], str],
    event_reason: str = "stopped",
) -> int:
    """為沒有 result 的 function_call 加 failed output，避免 UI 留在執行中。"""

    if not isinstance(output, list):
        return 0
    result_ids = {
        item.get("call_id")
        for item in output
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    }
    dangling = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id or call_id in result_ids:
            continue
        item["status"] = "failed"
        dangling.append(call_id)
    for call_id in dangling:
        output.append(
            {
                "type": "function_call_output",
                "id": id_factory(),
                "call_id": call_id,
                "output": [{"type": "input_text", "text": reason}],
                "status": "failed",
                "evaluation_event": _unexecuted_tool_event(event_reason),
            }
        )
    return len(dangling)


def terminalize_pending_tool_calls(
    output: Any,
    pending_batches: Any,
    *,
    reason: str,
    id_factory: Callable[[], str],
    event_reason: str = "stopped",
) -> int:
    """將尚未執行的工具呼叫記成 failed，再補齊所有未配對呼叫結果。"""

    if not isinstance(output, list):
        return 0
    result_ids = {
        item.get("call_id")
        for item in output
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    }
    function_ids = {
        item.get("call_id")
        for item in output
        if isinstance(item, dict) and item.get("type") == "function_call"
    }
    batches = pending_batches if isinstance(pending_batches, list) else []
    for batch in batches:
        if not isinstance(batch, list):
            continue
        for call in batch:
            if not isinstance(call, dict):
                continue
            call_id = call.get("id")
            if not isinstance(call_id, str) or not call_id or call_id in result_ids:
                continue
            function = call.get("function")
            function = function if isinstance(function, dict) else {}
            if call_id not in function_ids:
                arguments = function.get("arguments", "{}")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                output.append(
                    {
                        "type": "function_call",
                        "id": call_id,
                        "call_id": call_id,
                        "name": function.get("name", ""),
                        "arguments": arguments,
                        "status": "failed",
                    }
                )
                function_ids.add(call_id)
            else:
                for item in output:
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "function_call"
                        and item.get("call_id") == call_id
                    ):
                        item["status"] = "failed"
                        break
    return terminalize_dangling_function_calls(
        output,
        reason=reason,
        id_factory=id_factory,
        event_reason=event_reason,
    )


def _unexecuted_tool_event(reason: Any) -> dict[str, str]:
    if not isinstance(reason, str) or reason not in _UNEXECUTED_TOOL_REASONS:
        reason = "stopped"
    return {"type": "tool_not_executed", "reason": reason}


def is_evaluation_metadata(metadata: Any) -> bool:
    """只辨認由評測 adapter 明確標記且有 operation ID 的請求。"""

    return (
        isinstance(metadata, dict)
        and metadata.get("badmintonai_evaluation") is True
        and isinstance(metadata.get("badmintonai_operation_id"), str)
        and bool(metadata["badmintonai_operation_id"])
    )


@dataclass(frozen=True)
class BadmintonAIStreamTimeouts:
    """單次 BadmintonAI 上游串流的資料、進展與總時限。"""

    total_seconds: float
    no_data_seconds: float
    no_progress_seconds: float


def _configured_timeout(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and 1 <= value <= 600 else default


def badmintonai_stream_timeouts() -> BadmintonAIStreamTimeouts:
    """讀取 BADMINTON_AI_STREAM_*_SECONDS；預設總時限／無資料／無進展為 180/60/90 秒。"""

    total = _configured_timeout("BADMINTON_AI_STREAM_TOTAL_SECONDS", 180)
    no_data = min(_configured_timeout("BADMINTON_AI_STREAM_NO_DATA_SECONDS", 60), total)
    no_progress = min(
        _configured_timeout("BADMINTON_AI_STREAM_NO_PROGRESS_SECONDS", 90), total
    )
    return BadmintonAIStreamTimeouts(total, no_data, no_progress)


def is_badmintonai_stream_request(model_id: Any, metadata: Any) -> bool:
    """只保護評測 metadata 或 BADMINTON_AI_STREAM_MODEL_IDS 列出的模型 ID。"""

    if is_evaluation_metadata(metadata):
        return True
    configured = os.environ.get("BADMINTON_AI_STREAM_MODEL_IDS", "badmintonai")
    model_ids = {item.strip() for item in configured.split(",") if item.strip()}
    if not model_ids:
        model_ids = {"badmintonai"}
    return isinstance(model_id, str) and model_id in model_ids


def is_badmintonai_stream_failure(metadata: Any) -> bool:
    return badmintonai_stream_failure_code(metadata) is not None


def badmintonai_stream_failure_code(metadata: Any) -> str | None:
    if not isinstance(metadata, dict):
        return None
    code = metadata.get("_badmintonai_stream_failure")
    return code if isinstance(code, str) and code in _BADMINTONAI_STREAM_FAILURE_CODES else None


def badmintonai_stream_failure_message(metadata: Any) -> str:
    code = badmintonai_stream_failure_code(metadata)
    return _BADMINTONAI_STREAM_FAILURE_MESSAGES.get(
        code or "stream_upstream_error",
        _BADMINTONAI_STREAM_FAILURE_MESSAGES["stream_upstream_error"],
    )


def mark_badmintonai_stream_failure(metadata: Any, code: str) -> str:
    if code not in _BADMINTONAI_STREAM_FAILURE_CODES:
        code = "stream_upstream_error"
    if isinstance(metadata, dict):
        metadata["_badmintonai_stream_failure"] = code
    return code


def badmintonai_stream_error_event(code: str) -> bytes:
    if code not in _BADMINTONAI_STREAM_FAILURE_CODES:
        code = "stream_upstream_error"
    payload = {
        "error": {
            "code": f"badmintonai_{code}",
            "message": _BADMINTONAI_STREAM_FAILURE_MESSAGES[code],
        }
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


async def badmintonai_stream_failure_body(code: str) -> AsyncIterator[bytes]:
    yield badmintonai_stream_error_event(code)
    yield b"data: [DONE]\n\n"


def classify_badmintonai_request_failure(
    elapsed_seconds: float,
    timeouts: BadmintonAIStreamTimeouts,
) -> str:
    if elapsed_seconds >= timeouts.total_seconds:
        return "stream_total_timeout"
    if elapsed_seconds >= timeouts.no_data_seconds:
        return "stream_no_data_timeout"
    return "stream_upstream_error"


def apply_badmintonai_stream_timeout(fallback: Any, timeouts: BadmintonAIStreamTimeouts) -> Any:
    """保留 Open WebUI aiohttp timeout 連線設定，只覆寫總時限與讀取間隔。"""

    fields = {
        name: getattr(fallback, name)
        for name in ("connect", "sock_connect", "ceil_threshold")
        if hasattr(fallback, name)
    }
    fields.update(total=timeouts.total_seconds, sock_read=timeouts.no_data_seconds)
    try:
        return type(fallback)(**fields)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("無法套用 BadmintonAI 上游串流 timeout") from exc


def _safe_log_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "-"
    if any(pattern.fullmatch(value) for pattern in _SAFE_LOG_ID_PATTERNS):
        return value
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]
    return f"sha256:{digest}"


def log_evaluation_stage(
    logger: logging.Logger,
    stage: str,
    metadata: Any,
    *,
    duration_ms: int | None = None,
    http_status: int | None = None,
    exception_type: str | None = None,
    outcome: str | None = None,
    reason: str | None = None,
    character_count: int | None = None,
) -> None:
    """僅記錄可安全關聯的 ID、階段、耗時、狀態碼與例外型別。"""

    if not is_evaluation_metadata(metadata):
        return
    if not re.fullmatch(r"[a-z_]{1,48}", stage):
        stage = "invalid_stage"
    if exception_type is not None and not re.fullmatch(
        r"[A-Za-z0-9_.]{1,96}", exception_type
    ):
        exception_type = "Exception"
    logger.info(
        "evaluation_stage stage=%s chat_id=%s user_message_id=%s "
        "message_id=%s task_id=%s operation_id=%s duration_ms=%s "
        "http_status=%s exception_type=%s outcome=%s reason=%s character_count=%s",
        stage,
        _safe_log_id(metadata.get("chat_id")),
        _safe_log_id(metadata.get("user_message_id")),
        _safe_log_id(metadata.get("message_id")),
        _safe_log_id(metadata.get("task_id")),
        _safe_log_id(metadata.get("badmintonai_operation_id")),
        duration_ms if isinstance(duration_ms, int) and duration_ms >= 0 else "-",
        http_status if isinstance(http_status, int) and 100 <= http_status <= 599 else "-",
        exception_type or "-",
        outcome if outcome in _SAFE_OUTCOMES else "-",
        reason if isinstance(reason, str) and reason in _BADMINTONAI_STREAM_FAILURE_CODES else "-",
        character_count if isinstance(character_count, int) and character_count >= 0 else "-",
    )


def safe_http_error_message(status_code: int) -> str:
    """依狀態碼回傳安全錯誤，不讀取或顯示供應商本文。"""

    if status_code == 429:
        return "模型服務目前請求過多或額度受限；本輪已記錄失敗，不會自動重送。"
    if status_code in {401, 403}:
        return "模型服務驗證或權限失敗；請管理員檢查連線設定。"
    if status_code >= 500:
        return "模型服務暫時無法使用；本輪已記錄失敗，不會自動重送。"
    return "模型服務拒絕本次請求；本輪已記錄失敗，不會自動重送。"


def safe_evaluation_error(error: BaseException) -> str:
    """把回合失敗轉成不含上游 URL、本文或答案的安全訊息。"""

    status_code = getattr(error, "status_code", None)
    if status_code == 504 or isinstance(error, (asyncio.TimeoutError, TimeoutError)) or type(error).__name__ in {
        "ServerTimeoutError",
        "SocketTimeoutError",
    }:
        return "模型服務等待逾時；本輪已記錄失敗，不會自動重送。"
    if isinstance(status_code, int) and 400 <= status_code <= 599:
        return safe_http_error_message(status_code)
    return "模型服務回合處理失敗；本輪已記錄安全錯誤，不會自動重送。"


def evaluation_failure_visible_content(
    form_data: Any,
    metadata: Any,
    safe_message: Any,
    *,
    existing_message: Any = None,
    existing_message_known: bool = False,
) -> str | None:
    """只在已確認資料庫中的評測 assistant 訊息為空時提供安全失敗文字。"""

    if (
        not is_evaluation_metadata(metadata)
        or not existing_message_known
        or not isinstance(safe_message, str)
    ):
        return None
    safe_message = safe_message.strip()
    if not safe_message:
        return None

    if _has_visible_assistant_message_content(existing_message):
        return None

    target_ids = {
        value
        for value in (
            metadata.get("assistant_message_id"),
            metadata.get("message_id"),
        )
        if isinstance(value, str) and value
    }
    messages = form_data.get("messages") if isinstance(form_data, dict) else None
    if target_ids and isinstance(messages, list):
        for message in messages:
            if (
                not isinstance(message, dict)
                or message.get("role") != "assistant"
                or message.get("id") not in target_ids
            ):
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return None
            if isinstance(content, list) and any(
                isinstance(part, dict)
                and isinstance(part.get("text"), str)
                and part["text"].strip()
                for part in content
            ):
                return None
    return safe_message


def _has_visible_assistant_message_content(message: Any) -> bool:
    """檢查資料庫 assistant 的 content 或原生 output message 是否已有文字。"""

    def read_field(value: Any, key: str) -> Any:
        if isinstance(value, Mapping):
            return value.get(key)
        return getattr(value, key, None)

    def has_text(value: Any) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, list):
            return any(has_text(part) for part in value)
        if isinstance(value, Mapping):
            text = value.get("text")
            return isinstance(text, str) and bool(text.strip())
        return False

    content = read_field(message, "content")
    if has_text(content):
        return True

    output = read_field(message, "output")
    if not isinstance(output, list):
        return False
    return any(
        isinstance(item, Mapping)
        and item.get("type") == "message"
        and item.get("role") == "assistant"
        and has_text(item.get("content"))
        for item in output
    )


async def logged_evaluation_stream(
    response: Any,
    metadata: Any,
    started_at: float,
    stream_factory: Callable[..., AsyncIterator[bytes]],
    logger: logging.Logger,
) -> AsyncIterator[bytes]:
    """透傳原生上游串流，只在首塊、完成或錯誤時記錄一次。"""

    first_chunk = True
    try:
        async for chunk in stream_factory(response):
            if first_chunk:
                first_chunk = False
                log_evaluation_stage(
                    logger,
                    "upstream_first_chunk",
                    metadata,
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                )
            yield chunk
        log_evaluation_stage(
            logger,
            "upstream_stream_done",
            metadata,
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
    except BaseException as error:
        log_evaluation_stage(
            logger,
            "upstream_stream_error",
            metadata,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            exception_type=type(error).__name__,
        )
        raise


class _StreamProgressObserver:
    """只讀 SSE 的文字/工具參數 delta，原始 bytes 仍逐塊透傳。"""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.discard_long_line = False
        self.terminal = False
        self.finish_reason_seen = False
        self.upstream_error = False

    @staticmethod
    def _text_characters(value: Any) -> int:
        if not isinstance(value, str):
            return 0
        return sum(not character.isspace() for character in value)

    def _read_data_line(self, line: bytes) -> int:
        line = line.removesuffix(b"\r")
        if not line.startswith(b"data:"):
            return 0
        data = line[5:].strip()
        if data == b"[DONE]":
            self.terminal = True
            return 0
        try:
            event = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 0
        if not isinstance(event, dict):
            return 0
        event_error = event.get("error")
        if (isinstance(event_error, (dict, list)) and event_error) or (
            isinstance(event_error, str) and event_error.strip()
        ):
            self.upstream_error = True

        progress = 0
        choices = event.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                delta = choice.get("delta") if isinstance(choice, dict) else None
                if not isinstance(delta, dict):
                    continue
                progress += self._text_characters(delta.get("content"))
                tool_calls = delta.get("tool_calls")
                if isinstance(tool_calls, list):
                    for tool_call in tool_calls:
                        function = (
                            tool_call.get("function")
                            if isinstance(tool_call, dict)
                            else None
                        )
                        if isinstance(function, dict):
                            progress += self._text_characters(function.get("arguments"))
                if isinstance(choice, dict) and choice.get("finish_reason") is not None:
                    self.finish_reason_seen = True

        event_type = event.get("type")
        if event_type in {
            "response.output_text.delta",
            "response.function_call_arguments.delta",
        }:
            progress += self._text_characters(event.get("delta"))
        elif event_type in {
            "response.completed",
            "response.failed",
            "response.incomplete",
        }:
            self.finish_reason_seen = True
            if event_type == "response.failed":
                self.upstream_error = True
        return progress

    def feed(self, chunk: bytes | str) -> int:
        raw = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        if not isinstance(raw, bytes):
            return 0
        self.buffer.extend(raw)
        progress = 0
        while True:
            newline = self.buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(self.buffer[:newline])
            del self.buffer[: newline + 1]
            if self.discard_long_line:
                self.discard_long_line = False
                continue
            progress += self._read_data_line(line)
        if len(self.buffer) > _MAX_STREAM_PROGRESS_LINE_BYTES:
            self.buffer.clear()
            self.discard_long_line = True
        return progress


def _expired_stream_timeout(
    now: float,
    started_at: float,
    last_data_at: float,
    last_progress_at: float,
    timeouts: BadmintonAIStreamTimeouts,
) -> str | None:
    deadlines = (
        (started_at + timeouts.total_seconds, "stream_total_timeout"),
        (last_data_at + timeouts.no_data_seconds, "stream_no_data_timeout"),
        (last_progress_at + timeouts.no_progress_seconds, "stream_no_progress_timeout"),
    )
    expired = [item for item in deadlines if now >= item[0]]
    return min(expired)[1] if expired else None


def log_badmintonai_stream_failure(
    logger: logging.Logger,
    metadata: Any,
    code: str,
    *,
    duration_ms: int,
    character_count: int,
    exception_type: str | None = None,
) -> None:
    fields = {
        "duration_ms": max(0, duration_ms),
        "character_count": max(0, character_count),
        "reason": code,
    }
    if is_evaluation_metadata(metadata):
        log_evaluation_stage(
            logger,
            "upstream_stream_error",
            metadata,
            **fields,
            outcome="timeout" if code.endswith("timeout") else "error",
            exception_type=exception_type,
        )
        return
    logger.warning(
        "badmintonai_stream_terminated reason=%s duration_ms=%s character_count=%s "
        "chat_id=%s user_message_id=%s message_id=%s task_id=%s",
        code,
        fields["duration_ms"],
        fields["character_count"],
        _safe_log_id(metadata.get("chat_id") if isinstance(metadata, dict) else None),
        _safe_log_id(metadata.get("user_message_id") if isinstance(metadata, dict) else None),
        _safe_log_id(metadata.get("message_id") if isinstance(metadata, dict) else None),
        _safe_log_id(metadata.get("task_id") if isinstance(metadata, dict) else None),
    )


async def guarded_badmintonai_stream(
    response: Any,
    metadata: Any,
    started_at: float,
    stream_factory: Callable[..., AsyncIterator[bytes]],
    logger: logging.Logger,
    timeouts: BadmintonAIStreamTimeouts,
    *,
    clock: Callable[[], float] = time.monotonic,
    wait_for: Callable[..., Any] | None = None,
) -> AsyncIterator[bytes]:
    """有限保護 BadmintonAI 串流；錯誤以原生 SSE error 與 DONE 收尾。"""

    iterator = stream_factory(response).__aiter__()
    waiter = wait_for or asyncio.wait_for
    observer = _StreamProgressObserver()
    last_data_at = started_at
    last_progress_at = started_at
    effective_characters = 0
    first_chunk = True
    failure_code: str | None = None
    exception_type: str | None = None

    async def close_upstream() -> None:
        closer = getattr(iterator, "aclose", None)
        if closer is not None:
            try:
                await closer()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    try:
        while True:
            now = clock()
            failure_code = _expired_stream_timeout(
                now, started_at, last_data_at, last_progress_at, timeouts
            )
            if failure_code is not None:
                break
            nearest_deadline = min(
                started_at + timeouts.total_seconds,
                last_data_at + timeouts.no_data_seconds,
                last_progress_at + timeouts.no_progress_seconds,
            )
            try:
                chunk = await waiter(
                    iterator.__anext__(), timeout=max(0.001, nearest_deadline - now)
                )
            except StopAsyncIteration:
                if observer.terminal or observer.finish_reason_seen:
                    break
                failure_code = "stream_incomplete"
                break
            except (asyncio.TimeoutError, TimeoutError) as error:
                now = clock()
                failure_code = _expired_stream_timeout(
                    now, started_at, last_data_at, last_progress_at, timeouts
                ) or "stream_no_data_timeout"
                exception_type = type(error).__name__
                break

            now = clock()
            if now >= started_at + timeouts.total_seconds:
                failure_code = "stream_total_timeout"
                break
            last_data_at = now
            new_characters = observer.feed(chunk)
            if new_characters:
                effective_characters += new_characters
                last_progress_at = now
            if observer.upstream_error:
                failure_code = "stream_upstream_error"
            failure_code = _expired_stream_timeout(
                now, started_at, last_data_at, last_progress_at, timeouts
            ) or failure_code
            if first_chunk:
                first_chunk = False
                log_evaluation_stage(
                    logger,
                    "upstream_first_chunk",
                    metadata,
                    duration_ms=int((now - started_at) * 1000),
                )
            if not observer.upstream_error:
                yield chunk
            if failure_code is not None:
                break
            if observer.terminal:
                break
        if failure_code is None:
            log_evaluation_stage(
                logger,
                "upstream_stream_done",
                metadata,
                duration_ms=int((clock() - started_at) * 1000),
            )
            return
    except asyncio.CancelledError:
        raise
    except Exception as error:
        failure_code = _expired_stream_timeout(
            clock(), started_at, last_data_at, last_progress_at, timeouts
        ) or "stream_upstream_error"
        exception_type = type(error).__name__
    finally:
        if failure_code is None:
            await close_upstream()

    failure_code = mark_badmintonai_stream_failure(metadata, failure_code)
    await close_upstream()
    duration_ms = int((clock() - started_at) * 1000)
    log_badmintonai_stream_failure(
        logger,
        metadata,
        failure_code,
        duration_ms=duration_ms,
        character_count=effective_characters,
        exception_type=exception_type,
    )
    yield badmintonai_stream_error_event(failure_code)
    yield b"data: [DONE]\n\n"
