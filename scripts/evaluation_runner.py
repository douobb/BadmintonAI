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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol


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
_USAGE_KEYS = {
    "input_tokens": ("input_tokens", "prompt_tokens"),
    "output_tokens": ("output_tokens", "completion_tokens"),
    "total_tokens": ("total_tokens",),
}
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


def _canonical_usage(usage: Any, key: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    for source_key in _USAGE_KEYS[key]:
        value = usage.get(source_key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _sum_usage(attempts: list[dict[str, Any]]) -> dict[str, int | None]:
    """只加總每個回合實際回報的欄位，不由其他欄位推算。"""

    totals: dict[str, int | None] = {}
    for key in _USAGE_KEYS:
        values = [
            _canonical_usage(attempt["result"].get("usage"), key)
            if attempt.get("result") is not None
            else None
            for attempt in attempts
        ]
        totals[key] = (
            sum(values) if values and all(v is not None for v in values) else None
        )
    return totals


class EvaluationRunner:
    """逐題建立獨立對話、checkpoint、復原未完成回合並記錄實際 usage。"""

    SCHEMA_VERSION = 1
    FOLDER_ORGANIZATION_VERSION = "openwebui_native_folders_v1"

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
        self, *, stop_requested: Callable[[], bool] | None = None
    ) -> dict[str, Any]:
        """依序處理待執行題；等待澄清、已完成及失敗題不阻塞其他題。"""

        with self._lock:
            self._reload_from_store()
            for question in self._state["questions"]:
                if stop_requested is not None and stop_requested():
                    break
                if question["status"] not in {"pending", "running"}:
                    continue
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
        max_attempts = self._state["manifest"]["max_attempts"]
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
                        recovered, recovery_error = self._recover_turn(question, last)
                        if recovery_error is not None:
                            question["errors"].append(
                                {
                                    "stage": "recover_turn",
                                    "operation_id": last["operation_id"],
                                    **recovery_error,
                                }
                            )
                            self._save()
                            return
                        if recovered is not None:
                            result = _normalise_result(recovered)
                            last["result"] = result
                            last["recovered"] = True
                            last["finished_at"] = _now()
                            last["duration_ms"] = None
                            last["error"] = None
                            self._save()
                            continue
                        last["recovery_checked"] = True
                        self._save()
                    if len(attempts) >= max_attempts or not last["error"].get(
                        "retryable", False
                    ):
                        self._finish_turn(question, turn, None)
                        return
                    question["retry_count"] += 1
                    self._save()
                elif last.get("finished_at") is None:
                    recovered, recovery_error = self._recover_turn(question, last)
                    if recovery_error is not None:
                        question["errors"].append(
                            {
                                "stage": "recover_turn",
                                "operation_id": last["operation_id"],
                                **recovery_error,
                            }
                        )
                        self._save()
                        return
                    if recovered is not None:
                        result = _normalise_result(recovered)
                        last["result"] = result
                        last["recovered"] = True
                        last["finished_at"] = _now()
                        last["duration_ms"] = None
                        self._save()
                        continue
                    # client 明確回報尚無完成回合時，以相同 operation_id 再送，
                    # 依 client 契約由 operation_id 防止重複執行。
                    last["recovery_checked"] = True
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
        if event_policy and clarification_signal in {
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

    def submit_clarification(self, question_id: str, answer: str) -> dict[str, Any]:
        """將使用者補答送回原題 conversation，之後只恢復該題。"""

        if not isinstance(answer, str) or not answer.strip():
            raise EvaluationError("補答內容不可為空")
        with self._lock:
            self._reload_from_store()
            question = self._find_question(question_id)
            if question["status"] != "awaiting_clarification":
                raise EvaluationError(f"題目 {question_id} 目前不等待澄清")
            waiting_since = question.pop("waiting_since", None)
            if waiting_since:
                question["user_wait_ms"] += _elapsed_ms(waiting_since, _now())
            question["completed_at"] = None
            question["elapsed_ms"] = None
            question["status"] = "running"
            question["pending_turn"] = self._new_pending_turn(
                answer, kind="clarification"
            )
            self._save()
            self._execute_pending_turn(question)
            return self.snapshot()

    def _find_question(self, question_id: str) -> dict[str, Any]:
        for question in self._state["questions"]:
            if question["id"] == str(question_id):
                return question
        raise EvaluationError(f"未知題目 ID：{question_id}")

    def retry_failed(self, question_id: str) -> dict[str, Any]:
        """明確重試失敗題；沿用原對話並重新送出原題。"""

        with self._lock:
            self._reload_from_store()
            question = self._find_question(question_id)
            if question["status"] != "failed":
                raise EvaluationError(f"題目 {question_id} 不是失敗狀態")
            question["status"] = "running"
            question["completed_at"] = None
            question["elapsed_ms"] = None
            question["processing_elapsed_ms"] = None
            if question["conversation_id"] is None:
                question["creation_cycle"] = question.get("creation_cycle", 1) + 1
                question["status"] = "pending"
                self._save()
                self._run_question(question)
                return self.snapshot()
            last_turn = question["turns"][-1] if question["turns"] else None
            question["pending_turn"] = self._new_pending_turn(
                last_turn["request"] if last_turn else question["prompt"],
                kind="retry",
            )
            self._save()
            if self._ensure_conversation(question):
                self._execute_pending_turn(question)
            return self.snapshot()

    def summary(self) -> dict[str, Any]:
        """輸出不含題目逐輪細節的彙總統計；token 欄位只加總完整實測值。"""

        with self._lock:
            self._reload_from_store()
            questions = self._state["questions"]
            counts: dict[str, int] = {}
            for question in questions:
                counts[question["status"]] = counts.get(question["status"], 0) + 1
            token_totals: dict[str, int | None] = {}
            for key in _USAGE_KEYS:
                values = [question["usage_totals"].get(key) for question in questions]
                token_totals[key] = (
                    sum(values)
                    if values and all(value is not None for value in values)
                    else None
                )
            return {
                "run_id": self._state["run_id"],
                "manifest": _json_copy(self._state["manifest"], label="manifest"),
                "question_count": len(questions),
                "status_counts": counts,
                "token_totals": token_totals,
                "questions": [
                    {
                        "id": question["id"],
                        "status": question["status"],
                        "retry_count": question["retry_count"],
                        "elapsed_ms": question["elapsed_ms"],
                        "processing_elapsed_ms": question["processing_elapsed_ms"],
                        "user_wait_ms": question["user_wait_ms"],
                        "usage": question["usage_totals"],
                    }
                    for question in questions
                ],
            }
