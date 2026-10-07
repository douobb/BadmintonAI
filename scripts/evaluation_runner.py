"""可測試、可續跑的 Open WebUI 逐題評測核心。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from scripts.evaluation_usage import (
    summarize_attempts,
    summarize_question,
    summarize_questions,
)


class EvaluationError(ValueError):
    """評測題目、執行狀態或紀錄不符合契約。"""


class EvaluationStoreError(EvaluationError):
    """評測紀錄無法讀取或不符合支援的格式。"""


@dataclass(frozen=True)
class Question:
    """由原始編號 TXT 題檔解析出的單題；prompt 保留原始文字。"""

    id: str
    prompt: str
    source_line: int


@dataclass
class TurnResult:
    """Open WebUI client 回報的一次實際回應。usage 缺漏時保持 None。"""

    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    charts: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    awaiting_clarification: bool = False
    clarification_signal: str = "none"
    clarification_review_reason: str | None = None
    error: dict[str, Any] | None = None


class OpenWebUIClient(Protocol):
    """runner 使用的最小 client 介面。

    支援 idempotency 的 client 可用 key/operation_id 安全重試。沒有 server-side
    idempotency 的 client 必須在狀態不確定時拋出 uncertain=True 的例外，並透過
    recover_* 查詢；recover_conversation 回傳 None 只可代表已確認建立未發生，
    recover_turn 回傳 None 只可代表已確認該回合未執行，否則必須拋出例外。
    """

    def recover_conversation(
        self, run_id: str, question_id: str, idempotency_key: str
    ) -> str | None: ...

    def create_conversation(
        self,
        run_id: str,
        question_id: str,
        idempotency_key: str,
        model_snapshot: Any,
    ) -> str: ...

    def send_turn(
        self,
        conversation_id: str,
        content: str,
        operation_id: str,
    ) -> TurnResult: ...

    def recover_turn(
        self, conversation_id: str, operation_id: str
    ) -> TurnResult | None: ...


_QUESTION_LINE = re.compile(r"(?P<id>\d+): (?P<prompt>.*)")
_SENSITIVE_SNAPSHOT_KEYS = {
    "api_key",
    "api-key",
    "authorization",
    "access_token",
    "access-token",
    "token",
    "secret",
    "password",
    "credential",
    "key",
}
_EXECUTION_LOCK = threading.RLock()


def parse_numbered_questions(text: str) -> list[Question]:
    """解析 `N: 原題` TXT；只移除題號與其後一個分隔空格。"""

    questions: list[Question] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            continue
        match = _QUESTION_LINE.fullmatch(line)
        if match is None:
            raise EvaluationError(
                f"第 {line_number} 行格式錯誤；預期為 `題號: 原題文字`"
            )
        question_id = match.group("id")
        prompt = match.group("prompt")
        if not prompt.strip():
            raise EvaluationError(f"第 {line_number} 行沒有題目文字")
        if question_id in seen_ids:
            raise EvaluationError(f"題號重複：{question_id}")
        seen_ids.add(question_id)
        questions.append(
            Question(id=question_id, prompt=prompt, source_line=line_number)
        )

    if not questions:
        raise EvaluationError("題目檔沒有可執行題目")
    return questions


def select_question_ids(
    source_question_ids: list[str], selected_question_ids: Any = None
) -> list[str]:
    """驗證選題並依原始來源順序回傳題號；未指定時沿用全題行為。"""

    if selected_question_ids is None:
        return list(source_question_ids)
    if not isinstance(selected_question_ids, list):
        raise EvaluationError("題號選取格式無效")
    if not selected_question_ids:
        raise EvaluationError("至少選取一題才能開始評測")
    if any(
        not isinstance(question_id, str) or not question_id
        for question_id in selected_question_ids
    ):
        raise EvaluationError("題號選取格式無效")
    if len(set(selected_question_ids)) != len(selected_question_ids):
        raise EvaluationError("選取題號不可重複")

    available = set(source_question_ids)
    if any(question_id not in available for question_id in selected_question_ids):
        raise EvaluationError("選取題號不存在於目前題目來源")
    selected = set(selected_question_ids)
    return [
        question_id for question_id in source_question_ids if question_id in selected
    ]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def manual_retry_succeeded(question: dict[str, Any]) -> bool:
    """只投影有明確人工重試紀錄且該回合成功的完成題。"""
    return question.get("status") == "completed" and any(
        turn.get("manual_retry") is True
        and turn.get("kind") == "retry"
        and isinstance(turn.get("result"), dict)
        and not turn["result"].get("error")
        and not turn["result"].get("awaiting_clarification")
        for turn in question.get("turns", [])
    )


def _elapsed_ms(started_at: str, finished_at: str) -> int:
    start = datetime.fromisoformat(started_at)
    finish = datetime.fromisoformat(finished_at)
    return max(0, int((finish - start).total_seconds() * 1000))


def _json_copy(value: Any, *, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise EvaluationError(f"{label} 必須可序列化為標準 JSON") from exc


def _validate_snapshot(value: Any, *, label: str) -> Any:
    """只接受版本快照資料，拒絕把設定憑證寫進評測紀錄。"""

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = str(key).casefold()
                compact = re.sub(r"[^a-z0-9]", "", normalized)
                if (
                    normalized in _SENSITIVE_SNAPSHOT_KEYS
                    or compact
                    in {
                        "apikey",
                        "authorization",
                        "accesstoken",
                        "token",
                        "secret",
                        "password",
                        "credential",
                        "key",
                    }
                    or compact.endswith(
                        (
                            "apikey",
                            "accesstoken",
                            "authorization",
                            "secret",
                            "password",
                            "credential",
                        )
                    )
                ):
                    raise EvaluationError(f"{label} 不可包含憑證欄位：{key}")
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return _json_copy(value, label=label)


def _error_payload(exc: Exception) -> dict[str, Any]:
    message = _redact_text(str(exc))
    return {
        "type": type(exc).__name__,
        "message": message,
        "retryable": isinstance(exc, (TimeoutError, ConnectionError))
        or bool(getattr(exc, "retryable", False)),
        "uncertain": bool(getattr(exc, "uncertain", False)),
    }


def _redact_text(message: str) -> str:
    message = re.sub(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", message)
    message = re.sub(
        r"(?i)((?:api[_-]?key|authorization|access[_-]?token)\s*[:=]\s*)"
        r"[^\s,;]+",
        r"\1[REDACTED]",
        message,
    )
    return message


def _normalise_result(result: TurnResult) -> dict[str, Any]:
    if not isinstance(result, TurnResult):
        raise EvaluationError("Open WebUI client 必須回傳 TurnResult")
    if not isinstance(result.awaiting_clarification, bool):
        raise EvaluationError("awaiting_clarification 必須是布林值")
    if not isinstance(
        result.clarification_signal, str
    ) or result.clarification_signal not in {
        "none",
        "event",
        "text_candidate",
        "event_conflict",
        "tool_failed",
    }:
        raise EvaluationError("clarification_signal 格式無效")
    if result.clarification_review_reason is not None and not isinstance(
        result.clarification_review_reason, str
    ):
        raise EvaluationError("clarification_review_reason 必須是字串或 null")
    payload = asdict(result)
    if result.usage is None:
        payload["usage"] = None
    else:
        if not isinstance(result.usage, dict):
            raise EvaluationError("usage 必須是物件或 null")
        payload["usage"] = _json_copy(result.usage, label="usage")
    if result.error is not None:
        if not isinstance(result.error, dict):
            raise EvaluationError("error 必須是物件或 null")
        error = _json_copy(result.error, label="error")
        if isinstance(error.get("message"), str):
            error["message"] = _redact_text(error["message"])
        payload["error"] = error
    return _json_copy(payload, label="Open WebUI 回應")


def _retryable_result(result: dict[str, Any]) -> bool:
    error = result.get("error")
    if not isinstance(error, dict) or error.get("retryable") is not True:
        return False
    return not any(result.get(field) for field in ("messages", "tool_calls", "charts"))


def _sum_usage(attempts: list[dict[str, Any]]) -> dict[str, int | None]:
    """保留 checkpoint 舊欄位的嚴格完整語意。"""

    return summarize_attempts(attempts)["token_totals"]


class EvaluationRunner:
    """逐題建立獨立對話、checkpoint、復原未完成回合並記錄實際 usage。"""

    SCHEMA_VERSION = 1
    FOLDER_ORGANIZATION_VERSION = "openwebui_native_folders_v1"
    CLARIFICATION_RECOVERY_BACKOFF_INITIAL_SECONDS = 1.0
    CLARIFICATION_RECOVERY_BACKOFF_MAX_SECONDS = 30.0

    def __init__(
        self,
        client: OpenWebUIClient,
        store_path: Path,
        state: dict[str, Any],
    ) -> None:
        self.client = client
        self.store_path = Path(store_path)
        self._state = state
        # 單一 Python process 同時只執行一個 run / 題目操作。
        self._lock = _EXECUTION_LOCK
        self._validate_state()

    @classmethod
    def start(
        cls,
        client: OpenWebUIClient,
        *,
        question_file: Path,
        store_path: Path,
        model_snapshot: Any,
        data_snapshot: Any,
        max_attempts: int = 2,
        run_id: str | None = None,
        selected_question_ids: list[str] | None = None,
    ) -> EvaluationRunner:
        """建立新 run，或在來源與版本完全相同時開啟既有 run。"""

        if max_attempts < 1:
            raise EvaluationError("max_attempts 至少為 1")
        if run_id is not None and re.fullmatch(r"[a-f0-9]{32}", run_id) is None:
            raise EvaluationError("run_id 必須是 32 字元小寫十六進位識別碼")
        question_path = Path(question_file)
        try:
            question_bytes = question_path.read_bytes()
            question_text = question_bytes.decode("utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise EvaluationError(
                f"無法讀取 UTF-8 題目檔：{question_path.name}"
            ) from exc
        source_questions = parse_numbered_questions(question_text)
        ordered_question_ids = select_question_ids(
            [question.id for question in source_questions], selected_question_ids
        )
        selected_ids = set(ordered_question_ids)
        questions = [
            question for question in source_questions if question.id in selected_ids
        ]
        question_source = {
            "name": question_path.name,
            "sha256": hashlib.sha256(question_bytes).hexdigest(),
            "question_count": len(source_questions),
        }
        if selected_question_ids is not None:
            question_source["selected_question_ids"] = ordered_question_ids
        manifest = {
            "question_source": question_source,
            "model_snapshot": _validate_snapshot(
                model_snapshot, label="model_snapshot"
            ),
            "data_snapshot": _validate_snapshot(data_snapshot, label="data_snapshot"),
            "max_attempts": max_attempts,
        }
        path = Path(store_path)
        if path.exists():
            state = cls._read_store(path)
            if state.get("manifest") != manifest:
                raise EvaluationError(
                    "既有 run 的題目檔、模型／資料快照或重試設定不同，拒絕混用"
                )
        else:
            state = cls._new_state(manifest, questions, run_id=run_id)
        runner = cls(client, path, state)
        if not path.exists():
            runner._save()
        return runner

    @classmethod
    def resume(cls, client: OpenWebUIClient, *, store_path: Path) -> EvaluationRunner:
        """只靠逐題紀錄恢復 run；已完成題目不需重新載入來源檔。"""

        path = Path(store_path)
        return cls(client, path, cls._read_store(path))

    @staticmethod
    def _new_state(
        manifest: dict[str, Any],
        questions: list[Question],
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": EvaluationRunner.SCHEMA_VERSION,
            "conversation_organization": EvaluationRunner.FOLDER_ORGANIZATION_VERSION,
            "clarification_policy_version": "requestClarification-event-v1",
            "run_id": run_id or uuid.uuid4().hex,
            "created_at": _now(),
            "updated_at": None,
            "manifest": manifest,
            "questions": [
                {
                    "id": question.id,
                    "prompt": question.prompt,
                    "source_line": question.source_line,
                    "status": "pending",
                    "conversation_id": None,
                    "conversation_key": None,
                    "creation_cycle": 1,
                    "creation_attempts": [],
                    "started_at": None,
                    "completed_at": None,
                    "elapsed_ms": None,
                    "processing_elapsed_ms": None,
                    "user_wait_ms": 0,
                    "waiting_since": None,
                    "retry_count": 0,
                    "errors": [],
                    "turns": [],
                    "pending_turn": None,
                    "usage_totals": {
                        "input_tokens": None,
                        "output_tokens": None,
                        "total_tokens": None,
                    },
                }
                for question in questions
            ],
        }

    @staticmethod
    def _read_store(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvaluationStoreError(f"無法讀取評測紀錄：{path.name}") from exc
        if not isinstance(payload, dict):
            raise EvaluationStoreError("評測紀錄根節點必須是 JSON object")
        return payload

    def _validate_state(self) -> None:
        if self._state.get("schema_version") != self.SCHEMA_VERSION:
            raise EvaluationStoreError("不支援的評測紀錄 schema_version")
        questions = self._state.get("questions")
        if not isinstance(questions, list):
            raise EvaluationStoreError("評測紀錄缺少 questions array")
        ids = [item.get("id") for item in questions if isinstance(item, dict)]
        if len(ids) != len(questions) or len(ids) != len(set(ids)):
            raise EvaluationStoreError("評測紀錄題目格式錯誤或 ID 重複")

    def _save(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self._state["updated_at"] = _now()
        data = (
            json.dumps(self._state, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n"
        )
        temporary = self.store_path.with_name(
            f".{self.store_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(5):
                try:
                    temporary.replace(self.store_path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.01 * (attempt + 1))
        finally:
            temporary.unlink(missing_ok=True)

    def snapshot(self) -> dict[str, Any]:
        """回傳可供 UI 顯示的深拷貝狀態。"""

        with self._lock:
            self._reload_from_store()
            return _json_copy(self._state, label="run state")

    def _reload_from_store(self) -> None:
        if self.store_path.exists():
            self._state = self._read_store(self.store_path)
            self._validate_state()

    def run_pending(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
        at_question_boundary: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """依序處理待執行題，並在題目邊界提供序列工作排程點。"""

        with self._lock:
            self._reload_from_store()
            question_ids = [item["id"] for item in self._state["questions"]]
            for question_id in question_ids:
                if stop_requested is not None and stop_requested():
                    break
                # 邊界 callback 可能透過另一個 runner 更新相同 checkpoint；
                # 每題重新讀取，避免持有舊 dict 覆寫其更新。
                self._reload_from_store()
                question = self._find_question(question_id)
                if question["status"] not in {"pending", "running"}:
                    if at_question_boundary is not None:
                        at_question_boundary()
                    continue
                self._run_question(question)
                if at_question_boundary is not None:
                    at_question_boundary()
            return self.snapshot()

    def run_queued_question(self, question_id: str) -> dict[str, Any]:
        """只執行 queue 指定的原題，不掃描同 run 其他待執行題。"""

        with self._lock:
            self._reload_from_store()
            question = self._find_question(question_id)
            if question["status"] in {"pending", "running"}:
                self._run_question(question)
            return self.snapshot()

    def _run_question(self, question: dict[str, Any]) -> None:
        if question["started_at"] is None:
            question["started_at"] = _now()
        question["status"] = "running"
        self._save()
        if not self._ensure_conversation(question):
            return
        if question["pending_turn"] is None:
            question["pending_turn"] = self._new_pending_turn(
                question["prompt"], kind="question"
            )
            self._save()
        self._execute_pending_turn(question)

    def _ensure_conversation(self, question: dict[str, Any]) -> bool:
        if question["conversation_id"]:
            return True
        question_id = question["id"]
        run_id = self._state["run_id"]
        key = question["conversation_key"] or f"{run_id}:{question_id}:conversation"
        question["conversation_key"] = key
        uses_native_folders = (
            self._state.get("conversation_organization")
            == self.FOLDER_ORGANIZATION_VERSION
        )
        all_attempts = question["creation_attempts"]
        cycle = question.get("creation_cycle", 1)
        attempts = [item for item in all_attempts if item.get("cycle") == cycle]
        while True:
            if attempts and attempts[-1].get("finished_at") is None:
                attempt = attempts[-1]
                recover_method = (
                    "recover_conversation_in_folder"
                    if uses_native_folders
                    else "recover_conversation"
                )
                recover_conversation = getattr(self.client, recover_method, None)
                if callable(recover_conversation):
                    try:
                        if uses_native_folders:
                            recovered_id = recover_conversation(
                                run_id,
                                question_id,
                                key,
                                created_at=self._state["created_at"],
                                question_count=len(self._state["questions"]),
                            )
                        else:
                            recovered_id = recover_conversation(
                                run_id, question_id, key
                            )
                        if recovered_id is not None:
                            if not isinstance(recovered_id, str) or not recovered_id:
                                raise EvaluationError(
                                    "client 未回傳有效 conversation_id"
                                )
                            question["conversation_id"] = recovered_id
                            attempt["recovered"] = True
                            attempt["finished_at"] = _now()
                            attempt["duration_ms"] = None
                            self._save()
                            return True
                    except Exception as exc:
                        recovery_error = _error_payload(exc)
                        attempt["recovery_error"] = recovery_error
                        question["errors"].append(
                            {
                                "stage": "recover_conversation",
                                **recovery_error,
                            }
                        )
                        self._save()
                        return False
                elif uses_native_folders:
                    recovery_error = _error_payload(
                        EvaluationError("client 不支援此評測 run 所需的原生資料夾讀回")
                    )
                    attempt["recovery_error"] = recovery_error
                    question["errors"].append(
                        {"stage": "recover_conversation", **recovery_error}
                    )
                    self._save()
                    return False
            elif len(attempts) < self._state["manifest"]["max_attempts"]:
                attempt = {
                    "cycle": cycle,
                    "idempotency_key": key,
                    "started_at": _now(),
                    "finished_at": None,
                    "duration_ms": None,
                    "error": None,
                }
                all_attempts.append(attempt)
                attempts.append(attempt)
            else:
                break
            self._save()
            started = time.monotonic()
            try:
                if uses_native_folders:
                    create_conversation = getattr(
                        self.client, "create_conversation_in_folder", None
                    )
                    if not callable(create_conversation):
                        raise EvaluationError(
                            "client 不支援此評測 run 所需的原生資料夾建立"
                        )
                    conversation_id = create_conversation(
                        run_id,
                        question_id,
                        key,
                        self._state["manifest"]["model_snapshot"],
                        created_at=self._state["created_at"],
                        question_count=len(self._state["questions"]),
                    )
                else:
                    conversation_id = self.client.create_conversation(
                        run_id,
                        question_id,
                        key,
                        self._state["manifest"]["model_snapshot"],
                    )
                if not isinstance(conversation_id, str) or not conversation_id:
                    raise EvaluationError("client 未回傳有效 conversation_id")
                attempt["finished_at"] = _now()
                attempt["duration_ms"] = int((time.monotonic() - started) * 1000)
                question["conversation_id"] = conversation_id
                self._save()
                return True
            except Exception as exc:
                attempt["duration_ms"] = int((time.monotonic() - started) * 1000)
                attempt["error"] = _error_payload(exc)
                question["errors"].append(
                    {"stage": "create_conversation", **attempt["error"]}
                )
                if attempt["error"]["uncertain"]:
                    self._save()
                    return False
                attempt["finished_at"] = _now()
                if (
                    attempt["error"]["retryable"]
                    and len(attempts) < self._state["manifest"]["max_attempts"]
                ):
                    question["retry_count"] += 1
                self._save()
                if not attempt["error"]["retryable"]:
                    break
        question["status"] = "failed"
        self._complete_question(question)
        self._save()
        return False

    @staticmethod
    def _new_pending_turn(content: str, *, kind: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "request": content,
            "operation_id": uuid.uuid4().hex,
            "created_at": _now(),
            "attempts": [],
        }

    def _execute_pending_turn(self, question: dict[str, Any]) -> None:
        turn = question["pending_turn"]
        max_attempts = (
            1 if turn.get("manual_retry") else self._state["manifest"]["max_attempts"]
        )
        while True:
            attempts = turn["attempts"]
            attempt_to_send = None
            if attempts:
                last = attempts[-1]
                if last.get("result") is not None:
                    if (
                        _retryable_result(last["result"])
                        and len(attempts) < max_attempts
                    ):
                        question["retry_count"] += 1
                        self._save()
                    else:
                        self._finish_turn(question, turn, last["result"])
                        return
                elif last.get("error") is not None:
                    if not last.get("recovery_checked", False):
                        if not self._clarification_recovery_is_due(turn, last):
                            return
                        recovered, recovery_error = self._recover_turn(question, last)
                        if recovery_error is not None:
                            self._defer_clarification_recovery(turn, last)
                            self._record_recovery_error(question, last, recovery_error)
                            self._save()
                            return
                        if recovered is not None:
                            result = _normalise_result(recovered)
                            last["result"] = result
                            last["recovered"] = True
                            last["finished_at"] = _now()
                            last["duration_ms"] = None
                            last["error"] = None
                            last.pop("recovery_not_before", None)
                            last.pop("recovery_error", None)
                            self._save()
                            continue
                        last["recovery_checked"] = True
                        last.pop("recovery_not_before", None)
                        self._save()
                    if len(attempts) >= max_attempts or not last["error"].get(
                        "retryable", False
                    ):
                        self._finish_turn(question, turn, None)
                        return
                    question["retry_count"] += 1
                    self._save()
                elif last.get("finished_at") is None:
                    if not self._clarification_recovery_is_due(turn, last):
                        return
                    recovered, recovery_error = self._recover_turn(question, last)
                    if recovery_error is not None:
                        self._defer_clarification_recovery(turn, last)
                        self._record_recovery_error(question, last, recovery_error)
                        self._save()
                        return
                    if recovered is not None:
                        result = _normalise_result(recovered)
                        last["result"] = result
                        last["recovered"] = True
                        last["finished_at"] = _now()
                        last["duration_ms"] = None
                        last.pop("recovery_not_before", None)
                        last.pop("recovery_error", None)
                        self._save()
                        continue
                    # client 明確回報尚無完成回合時，以相同 operation_id 再送，
                    # 依 client 契約由 operation_id 防止重複執行。
                    last["recovery_checked"] = True
                    last.pop("recovery_not_before", None)
                    attempt_to_send = last
                else:
                    self._finish_turn(question, turn, None)
                    return

            if attempt_to_send is None:
                attempt_to_send = {
                    "operation_id": f"{turn['operation_id']}:{len(attempts) + 1}",
                    "started_at": _now(),
                    "finished_at": None,
                    "duration_ms": None,
                    "error": None,
                    "result": None,
                    "recovery_checked": False,
                }
                attempts.append(attempt_to_send)
            attempt = attempt_to_send
            self._save()
            started = time.monotonic()
            try:
                response = self.client.send_turn(
                    question["conversation_id"],
                    turn["request"],
                    attempt["operation_id"],
                )
                result = _normalise_result(response)
                attempt["result"] = result
                attempt["finished_at"] = _now()
                attempt["duration_ms"] = int((time.monotonic() - started) * 1000)
                if result.get("error"):
                    error = result["error"]
                    question["errors"].append(
                        {
                            "stage": "send_turn",
                            "operation_id": attempt["operation_id"],
                            **error,
                        }
                    )
                self._save()
            except Exception as exc:
                attempt["finished_at"] = _now()
                attempt["duration_ms"] = int((time.monotonic() - started) * 1000)
                attempt["error"] = _error_payload(exc)
                question["errors"].append(
                    {
                        "stage": "send_turn",
                        "operation_id": attempt["operation_id"],
                        **attempt["error"],
                    }
                )
                self._save()
                if (
                    not attempt["error"]["uncertain"]
                    and not attempt["error"]["retryable"]
                ):
                    self._finish_turn(question, turn, None)
                    return
                continue

            if not _retryable_result(result) or len(attempts) >= max_attempts:
                self._finish_turn(question, turn, result)
                return

    @classmethod
    def _clarification_recovery_is_due(
        cls, turn: dict[str, Any], attempt: dict[str, Any]
    ) -> bool:
        """佇列補答的不確定回合依持久化時間退避；一般題目維持既有恢復行為。"""

        if turn.get("kind") != "clarification" or not turn.get("queue_id"):
            return True
        retry_at = attempt.get("recovery_not_before")
        if not isinstance(retry_at, str):
            return True
        try:
            retry_time = datetime.fromisoformat(retry_at)
            if retry_time.tzinfo is None:
                retry_time = retry_time.replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        return retry_time <= datetime.now(timezone.utc)

    @classmethod
    def _defer_clarification_recovery(
        cls, turn: dict[str, Any], attempt: dict[str, Any]
    ) -> None:
        if turn.get("kind") != "clarification" or not turn.get("queue_id"):
            return
        try:
            failure_count = max(0, int(attempt.get("recovery_failure_count", 0))) + 1
        except (TypeError, ValueError):
            failure_count = 1
        delay = min(
            cls.CLARIFICATION_RECOVERY_BACKOFF_MAX_SECONDS,
            cls.CLARIFICATION_RECOVERY_BACKOFF_INITIAL_SECONDS
            * (2 ** min(failure_count - 1, 10)),
        )
        attempt["recovery_failure_count"] = failure_count
        attempt["recovery_not_before"] = (
            datetime.now(timezone.utc) + timedelta(seconds=delay)
        ).isoformat(timespec="milliseconds")

    @staticmethod
    def _record_recovery_error(
        question: dict[str, Any],
        attempt: dict[str, Any],
        error: dict[str, Any],
    ) -> None:
        operation_id = attempt.get("operation_id")
        error_type = error.get("type")
        if any(
            isinstance(existing, dict)
            and existing.get("stage") == "recover_turn"
            and existing.get("operation_id") == operation_id
            and existing.get("type") == error_type
            for existing in question["errors"]
        ):
            return
        question["errors"].append(
            {
                "stage": "recover_turn",
                "operation_id": operation_id,
                **error,
            }
        )

    def _recover_turn(
        self,
        question: dict[str, Any],
        attempt: dict[str, Any],
    ) -> tuple[TurnResult | None, dict[str, Any] | None]:
        try:
            result = self.client.recover_turn(
                question["conversation_id"], attempt["operation_id"]
            )
            return result, None
        except Exception as exc:
            error = _error_payload(exc)
            attempt["recovery_error"] = error
            return None, error

    def _finish_turn(
        self,
        question: dict[str, Any],
        turn: dict[str, Any],
        final_result: dict[str, Any] | None,
    ) -> None:
        turn["finished_at"] = _now()
        turn["result"] = final_result
        question["turns"].append(turn)
        question["pending_turn"] = None
        attempts = [
            attempt
            for saved_turn in question["turns"]
            for attempt in saved_turn["attempts"]
        ]
        question["usage_totals"] = _sum_usage(attempts)
        event_policy = (
            self._state.get("clarification_policy_version")
            == "requestClarification-event-v1"
        )
        clarification_signal = (
            final_result.get("clarification_signal")
            if isinstance(final_result, dict)
            else None
        )
        if (
            event_policy
            and clarification_signal == "event"
            and final_result
            and final_result.get("awaiting_clarification")
        ):
            # 澄清事件表示題目正在等待補答；保留同回合的分析錯誤於 turn result，
            # 但不把等待中的題目結案或誤報為分析成功。
            question["status"] = "awaiting_clarification"
            question["waiting_since"] = _now()
        elif event_policy and clarification_signal in {
            "text_candidate",
            "event_conflict",
            "tool_failed",
        }:
            question["status"] = "needs_review"
            question["clarification_review_reason"] = (
                final_result.get("clarification_review_reason")
                or "無法由已保存事件確定本輪是否等待補答"
            )
            self._complete_question(question)
        elif (
            event_policy
            and final_result
            and final_result.get("awaiting_clarification")
            and clarification_signal != "event"
        ):
            question["status"] = "needs_review"
            question["clarification_review_reason"] = (
                "client 回報等待澄清，但缺少 completed requestClarification 事件"
            )
            self._complete_question(question)
        elif (
            event_policy
            and clarification_signal == "event"
            and not final_result.get("awaiting_clarification")
        ):
            question["status"] = "needs_review"
            question["clarification_review_reason"] = (
                "已保存 requestClarification 事件，但 client 未回報等待補答"
            )
            self._complete_question(question)
        elif final_result is None or final_result.get("error"):
            question["status"] = "failed"
            self._complete_question(question)
        elif final_result.get("awaiting_clarification") or (
            event_policy and clarification_signal == "event"
        ):
            question["status"] = "awaiting_clarification"
            question["waiting_since"] = _now()
        else:
            question["status"] = "completed"
            self._complete_question(question)
        self._save()

    @staticmethod
    def _complete_question(question: dict[str, Any]) -> None:
        if question["completed_at"] is None:
            question["completed_at"] = _now()
        if question["started_at"] is not None:
            question["elapsed_ms"] = _elapsed_ms(
                question["started_at"], question["completed_at"]
            )
        attempts = list(question["creation_attempts"])
        attempts.extend(
            attempt for turn in question["turns"] for attempt in turn["attempts"]
        )
        durations = [attempt.get("duration_ms") for attempt in attempts]
        question["processing_elapsed_ms"] = (
            sum(durations)
            if durations and all(isinstance(value, int) for value in durations)
            else None
        )

    def submit_clarification(
        self,
        question_id: str,
        answer: str,
        *,
        queue_id: str | None = None,
        expected_conversation_id: str | None = None,
        expected_source_operation_id: str | None = None,
        submitted_at: str | None = None,
        allow_legacy_event_conflict: bool = False,
    ) -> dict[str, Any]:
        """將補答送回其原澄清對話；佇列呼叫可安全重入及拒絕過期身份。"""

        if not isinstance(answer, str) or not answer.strip():
            raise EvaluationError("補答內容不可為空")
        with self._lock:
            self._reload_from_store()
            question = self._find_question(question_id)
            if (
                expected_conversation_id is not None
                and question.get("conversation_id") != expected_conversation_id
            ):
                raise EvaluationError("補答對應的原對話已變更或過期")

            pending = question.get("pending_turn")
            if (
                queue_id is not None
                and question.get("status") == "running"
                and isinstance(pending, dict)
                and pending.get("kind") == "clarification"
                and pending.get("queue_id") == queue_id
            ):
                # 上次程序可能在 checkpoint 已保存、佇列尚未確認完成時中斷。
                self._execute_pending_turn(question)
                return self.snapshot()

            if queue_id is not None and any(
                isinstance(turn, dict) and turn.get("queue_id") == queue_id
                for turn in question.get("turns", [])
            ):
                # 已完成回合但佇列項目尚未移除；由 service 完成清理即可。
                return self.snapshot()

            legacy_projection = (
                allow_legacy_event_conflict
                and self._has_legacy_event_conflict_clarification(question)
            )
            if question["status"] != "awaiting_clarification" and not legacy_projection:
                raise EvaluationError("補答對應的澄清已過期或不再等待")
            turns = question.get("turns")
            source_turn = turns[-1] if isinstance(turns, list) and turns else None
            if expected_source_operation_id is not None and (
                not isinstance(source_turn, dict)
                or source_turn.get("operation_id") != expected_source_operation_id
            ):
                raise EvaluationError("補答對應的澄清回合已變更或過期")

            if legacy_projection:
                # 僅在實際補答回合即將執行時才遷移狀態；GET/排隊都不寫 checkpoint。
                question["clarification_legacy_projection"] = "legacy_event_conflict"
                question.pop("clarification_review_reason", None)
                question["status"] = "awaiting_clarification"

            waiting_since = question.pop("waiting_since", None)
            if waiting_since:
                wait_end = submitted_at or _now()
                try:
                    question["user_wait_ms"] += _elapsed_ms(waiting_since, wait_end)
                except (TypeError, ValueError):
                    raise EvaluationError("補答保存時間格式無效") from None
            question["completed_at"] = None
            question["elapsed_ms"] = None
            question["status"] = "running"
            pending = self._new_pending_turn(answer, kind="clarification")
            if queue_id is not None:
                pending["queue_id"] = queue_id
            question["pending_turn"] = pending
            self._save()
            self._execute_pending_turn(question)
            return self.snapshot()

    def _has_legacy_event_conflict_clarification(
        self, question: dict[str, Any]
    ) -> bool:
        """確認舊 checkpoint 同一回合有 completed 澄清事件，不猜測回答文字。"""

        if (
            self._state.get("clarification_policy_version")
            != "requestClarification-event-v1"
            or question.get("status") != "needs_review"
            or isinstance(question.get("pending_turn"), dict)
        ):
            return False
        turns = question.get("turns")
        if not isinstance(turns, list) or not turns or not isinstance(turns[-1], dict):
            return False
        result = turns[-1].get("result")
        if (
            not isinstance(result, dict)
            or result.get("clarification_signal") != "event_conflict"
        ):
            return False
        calls: list[Any] = []
        tool_calls = result.get("tool_calls")
        if isinstance(tool_calls, list):
            calls.extend(tool_calls)
        messages = result.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict) and message.get("role") == "assistant":
                    for key in ("tool_calls", "output"):
                        if isinstance(message.get(key), list):
                            calls.extend(message[key])
        for call in calls:
            if (
                not isinstance(call, dict)
                or call.get("name") != "requestClarification"
                or call.get("status") != "completed"
            ):
                continue
            arguments = call.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    continue
            if (
                isinstance(arguments, dict)
                and isinstance(arguments.get("question"), str)
                and arguments["question"].strip()
            ):
                return True
        return False

    def _find_question(self, question_id: str) -> dict[str, Any]:
        for question in self._state["questions"]:
            if question["id"] == str(question_id):
                return question
        raise EvaluationError(f"未知題目 ID：{question_id}")

    def retry_failed(self, question_id: str, *, execute: bool = True) -> dict[str, Any]:
        """人工重送最後失敗 request；先確認舊回合不再執行。"""

        with self._lock:
            self._reload_from_store()
            question = self._find_question(question_id)
            if question["status"] != "failed":
                raise EvaluationError(f"題目 {question_id} 不是失敗狀態")
            if question.get("pending_turn") is not None:
                raise EvaluationError("舊回合尚待讀回，請先續跑復原")
            last_turn = question["turns"][-1] if question["turns"] else None
            if question["conversation_id"] is not None and last_turn is None:
                raise EvaluationError("無法確認原對話失敗回合，未重送")
            if question["conversation_id"] is not None and last_turn:
                idle_check = getattr(self.client, "assert_retry_idle", None)
                if callable(idle_check):
                    try:
                        idle_check(question["conversation_id"])
                    except Exception:
                        raise EvaluationError(
                            "舊回合執行狀態未知或仍執行，未重送"
                        ) from None
                attempts = last_turn.get("attempts", [])
                if not attempts:
                    raise EvaluationError("無法確認失敗回合的執行狀態")
                recovered, error = self._recover_turn(question, attempts[-1])
                if error is not None:
                    raise EvaluationError("舊回合執行狀態未知或仍執行，未重送")
                if recovered is not None and not recovered.error:
                    raise EvaluationError("舊回合已有完成結果，請先讀回復原，未重送")
            elif any(item.get("uncertain") for item in question.get("errors", [])):
                raise EvaluationError("對話建立狀態未知，請先讀回復原")
            question["manual_retry_count"] = question.get("manual_retry_count", 0) + 1
            question["status"] = "running"
            question["completed_at"] = None
            question["elapsed_ms"] = None
            question["processing_elapsed_ms"] = None
            if question["conversation_id"] is None:
                question["creation_cycle"] = question.get("creation_cycle", 1) + 1
                question["status"] = "pending"
            question["pending_turn"] = self._new_pending_turn(
                last_turn["request"] if last_turn else question["prompt"],
                kind="retry",
            )
            question["pending_turn"]["manual_retry"] = True
            self._save()
            if execute:
                self._run_question(question)
            return self.snapshot()

    def execute_reserved_retry(self, question_id: str) -> None:
        """執行已持久保存的人工重試，不連帶重跑其他題。"""
        with self._lock:
            self._reload_from_store()
            question = self._find_question(question_id)
            if question["status"] not in {"pending", "running"}:
                raise EvaluationError("人工重試已不在待執行狀態")
            self._run_question(question)

    def summary(self) -> dict[str, Any]:
        """輸出不含題目逐輪細節的彙總統計；token 欄位只加總完整實測值。"""

        with self._lock:
            self._reload_from_store()
            questions = self._state["questions"]
            counts: dict[str, int] = {}
            for question in questions:
                counts[question["status"]] = counts.get(question["status"], 0) + 1
            usage_summary = summarize_questions(questions)
            return {
                "run_id": self._state["run_id"],
                "manifest": _json_copy(self._state["manifest"], label="manifest"),
                "question_count": len(questions),
                "status_counts": counts,
                "manual_retry_succeeded_count": sum(
                    manual_retry_succeeded(q) for q in questions
                ),
                "token_totals": usage_summary["token_totals"],
                "token_observed_totals": usage_summary["observed_totals"],
                "token_coverage": usage_summary["fields"],
                "usage_attempt_count": usage_summary["attempt_count"],
                "questions": [
                    {
                        "id": question["id"],
                        "status": question["status"],
                        "retry_count": question["retry_count"],
                        "elapsed_ms": question["elapsed_ms"],
                        "processing_elapsed_ms": question["processing_elapsed_ms"],
                        "user_wait_ms": question["user_wait_ms"],
                        "usage": question["usage_totals"],
                        "usage_observed_totals": summarize_question(question)[
                            "observed_totals"
                        ],
                        "usage_coverage": summarize_question(question)["fields"],
                        "usage_attempt_count": summarize_question(question)[
                            "attempt_count"
                        ],
                    }
                    for question in questions
                ],
            }
