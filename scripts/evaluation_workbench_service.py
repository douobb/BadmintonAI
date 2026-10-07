"""TASK-031 評測工作台 server-side API 與單一 run 協調器。"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import threading
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from scripts.evaluation_openwebui_client import (
    OpenWebUIEvaluationClient,
    _analysis_completion_error,
    clarification_text_candidate,
)
from scripts.evaluation_report_html import (
    REPORT_CSP,
    EvaluationReportError,
    build_evaluation_report_html,
    fetch_plotly_javascript,
)
from scripts.evaluation_runner import (
    EvaluationError,
    EvaluationRunner,
    TurnResult,
    manual_retry_succeeded,
    parse_numbered_questions,
    select_question_ids,
)
from scripts.evaluation_usage import (
    format_usage_summary,
    summarize_question,
    summarize_questions,
)
from scripts.export_chat_html import ChatExportError, build_chat_export_html
from scripts.html_to_pdf import PDFRenderError, render_html_to_pdf

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows 測試使用 process-local lock
    fcntl = None


SOURCE_ID = "v2-natural-100"
SOURCE_FILENAME = "評估問題_v2.txt"
DEFAULT_SOURCE_PATH = Path("/opt/badmintonai-evaluation/questions/評估問題_v2.txt")
DEFAULT_STORAGE_DIR = Path("/var/lib/badminton-ai/evaluation")
DEFAULT_TOOL_SERVER_URL = "http://tool-server:8000"
DEFAULT_OPEN_WEBUI_URL = "http://127.0.0.1:8080"
DEFAULT_MODEL_ID = "badmintonai"
MAX_UPLOAD_BYTES = 256 * 1024
MAX_JSON_BYTES = 16 * 1024
MAX_SELECTION_HEADER_BYTES = 16 * 1024
MAX_CONVERSATION_HTML_BYTES = 64 * 1024 * 1024
MAX_QUESTIONS = 200
MAX_ANSWER_CHARS = 8000
MAX_UI_MESSAGE_CHARS = 5000
MAX_CLASSIFICATION_REASON_CHARS = 1000
RUN_ID_PATTERN = re.compile(r"[a-f0-9]{32}\Z")
SHA256_PATTERN = re.compile(r"[a-f0-9]{64}\Z")
CONVERSATION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
ROUTES: dict[str, str] = {
    "badmintonai_evaluation_status": "/badmintonai/evaluation/api/status",
    "badmintonai_evaluation_sources": "/badmintonai/evaluation/api/sources",
    "badmintonai_evaluation_preview": "/badmintonai/evaluation/api/preview",
    "badmintonai_evaluation_preview_upload": "/badmintonai/evaluation/api/preview-upload",
    "badmintonai_evaluation_start": "/badmintonai/evaluation/api/start",
    "badmintonai_evaluation_start_upload": "/badmintonai/evaluation/api/start-upload",
    "badmintonai_evaluation_stop": "/badmintonai/evaluation/api/stop",
    "badmintonai_evaluation_resume": "/badmintonai/evaluation/api/resume",
    "badmintonai_evaluation_clarify": "/badmintonai/evaluation/api/clarify",
    "badmintonai_evaluation_retry": "/badmintonai/evaluation/api/runs/{run_id}/questions/{question_id}/retry",
    "badmintonai_evaluation_clarification_queue_update": (
        "/badmintonai/evaluation/api/clarification-queue/{run_id}/{question_id}"
    ),
    "badmintonai_evaluation_clarification_queue_cancel": (
        "/badmintonai/evaluation/api/clarification-queue/{run_id}/{question_id}"
    ),
    "badmintonai_evaluation_runs": "/badmintonai/evaluation/api/runs",
    "badmintonai_evaluation_run": "/badmintonai/evaluation/api/runs/{run_id}",
    "badmintonai_evaluation_classification_annotation": (
        "/badmintonai/evaluation/api/runs/{run_id}/questions/{question_id}/classification-annotation"
    ),
    "badmintonai_evaluation_conversation_html": (
        "/badmintonai/evaluation/api/runs/{run_id}/questions/{question_id}/conversation.html"
    ),
    "badmintonai_evaluation_download_json": "/badmintonai/evaluation/api/runs/{run_id}/json",
    "badmintonai_evaluation_download_summary": "/badmintonai/evaluation/api/runs/{run_id}/summary.txt",
    "badmintonai_evaluation_download_html": "/badmintonai/evaluation/api/runs/{run_id}/report.html",
    "badmintonai_evaluation_print_report": "/badmintonai/evaluation/api/runs/{run_id}/report/print",
    "badmintonai_evaluation_download_pdf": "/badmintonai/evaluation/api/runs/{run_id}/report.pdf",
    "badmintonai_chat_export_html": "/badmintonai/evaluation/api/chats/{chat_id}/export.html",
    "badmintonai_chat_export_pdf": "/badmintonai/evaluation/api/chats/{chat_id}/export.pdf",
}
ROUTE_NAMES = frozenset(ROUTES)
ROUTE_PATHS = frozenset(ROUTES.values())
CONVERSATION_IFRAME_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; connect-src 'none'; worker-src blob:; "
    "base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'self'"
)
CHAT_EXPORT_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; connect-src 'none'; worker-src blob:; "
    "base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'none'"
)


def _positive_seconds_environment(name: str, default: float) -> float:
    """讀取有界的正秒數環境設定，避免無限等待或無效部署設定。"""

    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as error:
        raise WorkbenchError(f"評測逾時設定 {name} 無效", status_code=503) from error
    if not 1 <= value <= 86400:
        raise WorkbenchError(f"評測逾時設定 {name} 超出允許範圍", status_code=503)
    return value


_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class WorkbenchError(ValueError):
    """安全回傳給工作台 API 的領域錯誤。"""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class _ExecutionLease:
    """跨執行緒、跨 worker process 的單一工作鎖。"""

    def __init__(self, path: Path, process_lock: threading.Lock) -> None:
        self._process_lock = process_lock
        self._file = path.open("a+b")
        if fcntl is not None:
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._file.close()
                self._process_lock.release()
                raise WorkbenchError(
                    "另一個評測工作正在執行", status_code=409
                ) from None
            except OSError:
                self._file.close()
                raise

    def close(self) -> None:
        try:
            if fcntl is not None:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
        finally:
            self._process_lock.release()


class _KeyRedactingClient:
    """在 runner checkpoint 前移除後端 key 的任何意外回顯。"""

    def __init__(self, client: Any, secret: str) -> None:
        self._client = client
        self._secret = secret

    def recover_conversation(self, *args: Any) -> Any:
        return self._client.recover_conversation(*args)

    def create_conversation(self, *args: Any) -> Any:
        return self._client.create_conversation(*args)

    def recover_conversation_in_folder(self, *args: Any, **kwargs: Any) -> Any:
        return self._client.recover_conversation_in_folder(*args, **kwargs)

    def create_conversation_in_folder(self, *args: Any, **kwargs: Any) -> Any:
        return self._client.create_conversation_in_folder(*args, **kwargs)

    def send_turn(self, *args: Any) -> TurnResult:
        return self._redact_result(self._client.send_turn(*args))

    def recover_turn(self, *args: Any) -> TurnResult | None:
        result = self._client.recover_turn(*args)
        return self._redact_result(result) if result is not None else None

    def assert_retry_idle(self, conversation_id: str) -> None:
        check = getattr(self._client, "assert_retry_idle", None)
        if callable(check):
            check(conversation_id)

    def _redact_result(self, result: TurnResult) -> TurnResult:
        return TurnResult(
            messages=_redact_secret_values(result.messages, self._secret),
            tool_calls=_redact_secret_values(result.tool_calls, self._secret),
            charts=_redact_secret_values(result.charts, self._secret),
            usage=_redact_secret_values(result.usage, self._secret),
            awaiting_clarification=result.awaiting_clarification,
            clarification_signal=result.clarification_signal,
            clarification_review_reason=_redact_secret_values(
                result.clarification_review_reason, self._secret
            ),
            error=_redact_secret_values(result.error, self._secret),
        )


class EvaluationWorkbenchService:
    """管理單一可續跑 run；所有執行資料只寫入專用私有 volume。"""

    def __init__(
        self,
        *,
        storage_dir: Path,
        question_file: Path = DEFAULT_SOURCE_PATH,
        client_factory: Callable[[], Any] | None = None,
        data_snapshot_provider: Callable[[], dict[str, Any]] | None = None,
        model_snapshot_provider: Callable[[Any], dict[str, Any]] | None = None,
        api_key_provider: Callable[[], str] | None = None,
        plotly_javascript_provider: Callable[[], str] | None = None,
        max_upload_bytes: int = MAX_UPLOAD_BYTES,
        max_questions: int = MAX_QUESTIONS,
    ) -> None:
        self.storage_dir = Path(storage_dir).resolve()
        self.question_file = Path(question_file)
        self._client_factory = client_factory or self._client_from_environment
        self._data_snapshot_provider = data_snapshot_provider or (
            self._data_snapshot_from_environment
        )
        self._model_snapshot_provider = model_snapshot_provider or (
            lambda client: client.get_model_snapshot()
        )
        self._api_key_provider = api_key_provider or (
            lambda: os.environ.get("BADMINTON_AI_OPEN_WEBUI_API_KEY", "")
        )
        self._plotly_javascript_provider = (
            plotly_javascript_provider or fetch_plotly_javascript
        )
        self.max_upload_bytes = max_upload_bytes
        self.max_questions = max_questions
        self.runs_dir = self.storage_dir / "runs"
        self.staging_dir = self.storage_dir / "staging"
        self.annotations_dir = self.storage_dir / "annotations"
        self._pointer_path = self.storage_dir / "current.json"
        self._pointer_lock_path = self.storage_dir / "current.lock"
        self._lease_path = self.storage_dir / "worker.lock"
        # 沿用既有持久化檔案，讓舊補答佇列可在明確操作時重入共享 queue。
        self._clarification_queue_path = self.storage_dir / "clarification-queue.json"
        self._clarification_queue_lock_path = (
            self.storage_dir / "clarification-queue.lock"
        )
        self._meta_lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._lease: _ExecutionLease | None = None
        self._worker_run_id: str | None = None
        self._worker_stop_event: threading.Event | None = None
        self._runner: EvaluationRunner | None = None
        self._current_run_id: str | None = None
        self._last_worker_error: str | None = None

    @classmethod
    def from_environment(cls) -> EvaluationWorkbenchService:
        return cls(
            storage_dir=Path(
                os.environ.get(
                    "BADMINTON_AI_EVALUATION_STORAGE_DIR", str(DEFAULT_STORAGE_DIR)
                )
            ),
            question_file=DEFAULT_SOURCE_PATH,
        )

    @staticmethod
    def _client_from_environment() -> OpenWebUIEvaluationClient:
        key = os.environ.get("BADMINTON_AI_OPEN_WEBUI_API_KEY", "")
        if not key.strip():
            raise WorkbenchError("後端評測連線尚未設定", status_code=503)
        upstream_timeout = _positive_seconds_environment("AIOHTTP_CLIENT_TIMEOUT", 1200)
        adapter_wait_grace = _positive_seconds_environment(
            "BADMINTON_AI_EVALUATION_ADAPTER_WAIT_GRACE_SECONDS", 30
        )
        adapter_wait = upstream_timeout + adapter_wait_grace
        return OpenWebUIEvaluationClient(
            base_url=DEFAULT_OPEN_WEBUI_URL,
            api_key=key,
            model_id=os.environ.get(
                "BADMINTON_AI_EVALUATION_MODEL_ID", DEFAULT_MODEL_ID
            ),
            timeout_seconds=20,
            poll_interval_seconds=0.5,
            # 上游單次 HTTP request 上限與 adapter 整輪讀回上限是不同層級；
            # adapter 可另行設定，預設比上游多 30 秒供錯誤持久化與讀回。
            max_wait_seconds=adapter_wait,
        )

    @staticmethod
    def _data_snapshot_from_environment() -> dict[str, Any]:
        base_url = os.environ.get(
            "BADMINTON_AI_EVALUATION_TOOL_SERVER_URL", DEFAULT_TOOL_SERVER_URL
        ).rstrip("/")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise WorkbenchError("資料快照服務設定無效", status_code=503)

        def fetch(path: str) -> dict[str, Any]:
            request = urllib.request.Request(
                f"{base_url}{path}", headers={"Accept": "application/json"}
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    data = response.read(128 * 1024 + 1)
            except (urllib.error.URLError, TimeoutError, OSError):
                raise WorkbenchError(
                    "無法讀取資料快照；評測尚未開始", status_code=503
                ) from None
            if len(data) > 128 * 1024:
                raise WorkbenchError("資料快照回應超過上限", status_code=503)
            try:
                payload = json.loads(data)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise WorkbenchError("資料快照回應格式無效", status_code=503) from None
            if not isinstance(payload, dict):
                raise WorkbenchError("資料快照回應格式無效", status_code=503)
            return payload

        health = fetch("/health")
        summary = fetch("/tools/dataset-summary")
        snapshot_id = summary.get("snapshot_id")
        if (
            health.get("status") != "ok"
            or health.get("data_available") is not True
            or not isinstance(snapshot_id, str)
            or not snapshot_id
        ):
            raise WorkbenchError("目前資料集不可用或缺少版本識別", status_code=503)
        return {
            "tool_server_version": health.get("version"),
            "snapshot_id": snapshot_id,
            "snapshot_version": summary.get("snapshot_version"),
            "source": _safe_basename(summary.get("source")),
            "row_count": _nonnegative_int(summary.get("row_count")),
            "column_count": _nonnegative_int(summary.get("column_count")),
            "match_count": _nonnegative_int(summary.get("match_count")),
            "captured_at": _utc_now(),
        }

    def _ensure_storage(self) -> None:
        for directory in (
            self.storage_dir,
            self.runs_dir,
            self.staging_dir,
            self.annotations_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            _chmod_private(directory, 0o700)

    @contextmanager
    def _clarification_queue_guard(self):
        """序列化佇列讀寫，並支援同機多執行緒與多 worker process。"""

        self._ensure_storage()
        lock_key = str(self._clarification_queue_lock_path)
        with _PROCESS_LOCKS_GUARD:
            process_lock = _PROCESS_LOCKS.setdefault(lock_key, threading.Lock())
        process_lock.acquire()
        lock_file = None
        try:
            lock_file = self._clarification_queue_lock_path.open("a+b")
            _chmod_private(self._clarification_queue_lock_path, 0o600)
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield
        except OSError:
            raise WorkbenchError("無法存取補答佇列", status_code=500) from None
        finally:
            if lock_file is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_file.close()
            process_lock.release()

    def _read_queue_payload_unlocked(self) -> dict[str, Any]:
        path = self._clarification_queue_path
        if path.is_symlink():
            raise WorkbenchError("評測佇列檔案無效", status_code=500)
        if not path.exists():
            return {"schema_version": 2, "items": [], "paused_run_ids": []}
        if not path.is_file():
            raise WorkbenchError("評測佇列檔案無效", status_code=500)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise WorkbenchError("評測佇列無法讀取", status_code=500) from None
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") not in {1, 2}
            or not isinstance(payload.get("items"), list)
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("queue_id"), str)
                or not isinstance(item.get("run_id"), str)
                or RUN_ID_PATTERN.fullmatch(item["run_id"]) is None
                or not isinstance(item.get("question_id"), str)
                or item.get("status") not in {"queued", "executing", "continuation"}
                or self._queue_work_type(item)
                not in {"question", "clarification", "retry"}
                or (
                    self._queue_work_type(item) == "clarification"
                    and (
                        not isinstance(item.get("conversation_id"), str)
                        or not isinstance(item.get("clarification_id"), str)
                        or not isinstance(item.get("source_operation_id"), str)
                        or not isinstance(item.get("answer"), str)
                    )
                )
                or (
                    self._queue_work_type(item) == "retry"
                    and (
                        type(item.get("expected_retry_count")) is not int
                        or item["expected_retry_count"] < 0
                    )
                )
                for item in payload["items"]
            )
            or (
                payload.get("schema_version") == 2
                and (
                    not isinstance(payload.get("paused_run_ids"), list)
                    or any(
                        not isinstance(run_id, str)
                        or RUN_ID_PATTERN.fullmatch(run_id) is None
                        for run_id in payload["paused_run_ids"]
                    )
                )
            )
        ):
            raise WorkbenchError("評測佇列格式無效", status_code=500)
        return {
            "schema_version": 2,
            "items": payload["items"],
            "paused_run_ids": payload.get("paused_run_ids", []),
        }

    @staticmethod
    def _queue_work_type(item: dict[str, Any]) -> str:
        """舊 schema 未記工作類型時，視為既有補答工作。"""

        return item.get("work_type") or "clarification"

    def _read_clarification_queue_unlocked(self) -> list[dict[str, Any]]:
        return self._read_queue_payload_unlocked()["items"]

    def _write_clarification_queue_unlocked(
        self,
        items: list[dict[str, Any]],
        *,
        paused_run_ids: list[str] | None = None,
    ) -> None:
        if paused_run_ids is None:
            paused_run_ids = self._read_queue_payload_unlocked()["paused_run_ids"]
        payload = (
            json.dumps(
                {
                    "schema_version": 2,
                    "items": items,
                    "paused_run_ids": sorted(set(paused_run_ids)),
                },
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        )
        self._atomic_write(self._clarification_queue_path, payload.encode("utf-8"))

    def _clarification_queue_items(
        self, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._clarification_queue_guard():
            items = self._read_clarification_queue_unlocked()
            selected = [
                item
                for item in items
                if item.get("status") in {"queued", "executing"}
                and (run_id is None or item.get("run_id") == run_id)
            ]
            return json.loads(json.dumps(selected, ensure_ascii=False))

    def _migrate_legacy_queue_unlocked(self) -> list[dict[str, Any]]:
        """在明確排程操作中將 v1 補答項目轉成共享 work type。"""

        payload = self._read_queue_payload_unlocked()
        items = payload["items"]
        updated: list[dict[str, Any]] = []
        changed = payload["schema_version"] != 2
        for item in items:
            if item.get("status") == "continuation":
                # 舊 continuation 只代表 runner.run_pending 的排程點；
                # 明確 resume 會依 checkpoint 重建尚未完成的原題工作。
                changed = True
                continue
            if "work_type" not in item:
                item["work_type"] = "clarification"
                changed = True
            updated.append(item)
        if changed:
            self._write_clarification_queue_unlocked(
                updated, paused_run_ids=payload["paused_run_ids"]
            )
        return updated

    def _ensure_run_work_enqueued_unlocked(
        self, run_id: str, state: dict[str, Any], items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """依 checkpoint 順序補入尚未排程的原題，重入時不重複建立。"""

        scheduled = {
            (item.get("run_id"), item.get("question_id"))
            for item in items
            if item.get("status") in {"queued", "executing"}
        }
        for question in state.get("questions", []):
            if not isinstance(question, dict) or question.get("status") not in {
                "pending",
                "running",
            }:
                continue
            key = (run_id, str(question.get("id")))
            if key in scheduled:
                continue
            items.append(
                {
                    "queue_id": uuid.uuid4().hex,
                    "work_type": "question",
                    "run_id": run_id,
                    "question_id": key[1],
                    "status": "queued",
                    "accepted_at": _utc_now_milliseconds(),
                }
            )
            scheduled.add(key)
        return items

    def _queue_has_work(self, run_id: str | None = None) -> bool:
        with self._clarification_queue_guard():
            return any(
                item.get("status") in {"queued", "executing"}
                and (run_id is None or item.get("run_id") == run_id)
                for item in self._read_clarification_queue_unlocked()
            )

    def _run_is_paused(self, run_id: str) -> bool:
        with self._clarification_queue_guard():
            payload = self._read_queue_payload_unlocked()
            if run_id in payload["paused_run_ids"]:
                return True
        pointer = self._read_pointer()
        return bool(pointer and pointer["run_id"] == run_id and pointer["paused"])

    def _set_run_paused(self, run_id: str, paused: bool) -> None:
        with self._clarification_queue_guard():
            payload = self._read_queue_payload_unlocked()
            paused_ids = set(payload["paused_run_ids"])
            if paused:
                paused_ids.add(run_id)
            else:
                paused_ids.discard(run_id)
            self._write_clarification_queue_unlocked(
                payload["items"], paused_run_ids=list(paused_ids)
            )

    @staticmethod
    def _clarification_retry_at(item: dict[str, Any]) -> datetime | None:
        value = item.get("next_attempt_at")
        if not isinstance(value, str):
            return None
        try:
            retry_at = datetime.fromisoformat(value)
        except ValueError:
            return None
        return (
            retry_at.replace(tzinfo=timezone.utc)
            if retry_at.tzinfo is None
            else retry_at
        )

    @staticmethod
    def _clarification_identity(
        run_id: str,
        question: dict[str, Any],
        *,
        allow_legacy_event_conflict: bool = False,
    ) -> dict[str, str] | None:
        """以 run、原對話及發出澄清的回合綁定一次可回答的提示。"""

        legacy_projection = False
        if question.get("status") == "awaiting_clarification":
            pass
        elif question.get("status") == "needs_review" and allow_legacy_event_conflict:
            legacy_projection = (
                _legacy_event_conflict_clarification(question) is not None
            )
            if not legacy_projection:
                return None
        else:
            return None
        turns = question.get("turns")
        if not isinstance(turns, list) or not turns or not isinstance(turns[-1], dict):
            return None
        source_turn = turns[-1]
        operation_id = source_turn.get("operation_id")
        conversation_id = question.get("conversation_id")
        assistant = _latest_assistant(question)
        clarification = (
            _legacy_event_conflict_clarification(question)["clarification"]
            if legacy_projection
            else _structured_clarification(assistant)
            if assistant
            else None
        )
        if (
            not isinstance(operation_id, str)
            or not operation_id
            or not isinstance(conversation_id, str)
            or not conversation_id
            or clarification is None
        ):
            return None
        identity_payload = json.dumps(
            {
                "run_id": run_id,
                "question_id": str(question.get("id")),
                "conversation_id": conversation_id,
                "source_operation_id": operation_id,
                "clarification": clarification,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return {
            "clarification_id": hashlib.sha256(
                identity_payload.encode("utf-8")
            ).hexdigest(),
            "conversation_id": conversation_id,
            "source_operation_id": operation_id,
            "legacy_event_conflict": "true" if legacy_projection else "false",
        }

    def _try_lease(self) -> _ExecutionLease | None:
        self._ensure_storage()
        lock_key = str(self.storage_dir)
        with _PROCESS_LOCKS_GUARD:
            process_lock = _PROCESS_LOCKS.setdefault(lock_key, threading.Lock())
        if not process_lock.acquire(blocking=False):
            return None
        try:
            return _ExecutionLease(self._lease_path, process_lock)
        except WorkbenchError:
            return None
        except OSError:
            process_lock.release()
            raise WorkbenchError("無法取得評測執行鎖", status_code=503) from None

    @staticmethod
    def _release_lease(lease: _ExecutionLease | None) -> None:
        if lease is not None:
            lease.close()

    def _read_pointer(self) -> dict[str, Any] | None:
        if not self._pointer_path.exists():
            return None
        if self._pointer_path.is_symlink():
            raise WorkbenchError("評測狀態索引無效", status_code=500)
        try:
            pointer = json.loads(self._pointer_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise WorkbenchError("評測狀態索引無法讀取", status_code=500) from None
        if (
            not isinstance(pointer, dict)
            or not isinstance(pointer.get("run_id"), str)
            or RUN_ID_PATTERN.fullmatch(pointer["run_id"]) is None
            or not isinstance(pointer.get("paused"), bool)
        ):
            raise WorkbenchError("評測狀態索引格式無效", status_code=500)
        return pointer

    def _write_pointer(
        self,
        run_id: str,
        *,
        paused: bool,
        expected_current_run_id: str | None = None,
    ) -> None:
        self._ensure_storage()
        lock_key = str(self._pointer_lock_path)
        with _PROCESS_LOCKS_GUARD:
            process_lock = _PROCESS_LOCKS.setdefault(lock_key, threading.Lock())
        process_lock.acquire()
        lock_file = None
        try:
            lock_file = self._pointer_lock_path.open("a+b")
            _chmod_private(self._pointer_lock_path, 0o600)
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            if expected_current_run_id is not None:
                current = self._read_pointer()
                if current is None or current["run_id"] != expected_current_run_id:
                    raise WorkbenchError(
                        "目前 run 已切換，請重新載入狀態", status_code=409
                    )
            payload = (
                json.dumps({"run_id": run_id, "paused": paused}, ensure_ascii=False)
                + "\n"
            )
            self._atomic_write(self._pointer_path, payload.encode("utf-8"))
        except OSError:
            raise WorkbenchError("無法更新評測狀態索引", status_code=500) from None
        finally:
            if lock_file is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_file.close()
            process_lock.release()

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _chmod_private(temporary, 0o600)
            temporary.replace(path)
        except OSError:
            raise WorkbenchError("無法保存評測狀態", status_code=500) from None
        finally:
            temporary.unlink(missing_ok=True)

    def _state_path(self, run_id: str) -> Path:
        if RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise WorkbenchError("run ID 格式無效", status_code=404)
        return self.runs_dir / f"{run_id}.json"

    def _annotation_path(self, run_id: str) -> Path:
        if RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise WorkbenchError("run ID 格式無效", status_code=404)
        return self.annotations_dir / f"{run_id}.json"

    def _read_annotations(self, run_id: str) -> dict[str, Any]:
        path = self._annotation_path(run_id)
        if not path.exists():
            return {"schema_version": 1, "run_id": run_id, "annotations": []}
        if path.is_symlink() or not path.is_file():
            raise WorkbenchError("人工分類註記檔無效", status_code=500)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise WorkbenchError("人工分類註記檔無法讀取", status_code=500) from None
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("run_id") != run_id
            or not isinstance(payload.get("annotations"), list)
            or any(not isinstance(item, dict) for item in payload["annotations"])
        ):
            raise WorkbenchError("人工分類註記檔格式無效", status_code=500)
        return payload

    def _read_state(self, run_id: str) -> dict[str, Any]:
        path = self._state_path(run_id)
        if path.is_symlink() or not path.is_file():
            raise WorkbenchError("找不到評測紀錄", status_code=404)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise WorkbenchError("評測紀錄無法讀取", status_code=500) from None
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != EvaluationRunner.SCHEMA_VERSION
            or state.get("run_id") != run_id
            or not isinstance(state.get("questions"), list)
        ):
            raise WorkbenchError("評測紀錄格式無效", status_code=500)
        return state

    def get_run(self, run_id: str) -> dict[str, Any]:
        """回傳 checkpoint 的唯讀投影及獨立保存的管理員註記。"""

        state = self._read_state(run_id)
        annotations = self._read_annotations(run_id)
        projected = _state_for_ui(
            state,
            annotations=annotations["annotations"],
            clarification_queue=self._clarification_queue_items(run_id),
        )
        projected = _redact_secret_values(projected, self._api_key_provider())
        return {
            "run": projected,
            "run_id": run_id,
            "run_status": _run_status(
                state, worker_running=False, paused=self._run_is_paused(run_id)
            ),
        }

    def conversation_html(self, run_id: str, question_id: str) -> str:
        """只依 run checkpoint 的 question ID 讀取已綁定的 Open WebUI 對話。"""

        if not isinstance(question_id, str) or len(question_id) > 128:
            raise WorkbenchError("找不到評測題目", status_code=404)
        state = self._read_state(run_id)
        question = next(
            (
                item
                for item in state["questions"]
                if isinstance(item, dict) and item.get("id") == question_id
            ),
            None,
        )
        if question is None:
            raise WorkbenchError("找不到評測題目", status_code=404)
        conversation_id = _safe_conversation_id(question.get("conversation_id"))
        if conversation_id is None:
            raise WorkbenchError("此題沒有可讀取的原對話", status_code=404)

        client = self._client_factory()
        exporter = getattr(client, "export_chat_html", None)
        if not callable(exporter):
            raise WorkbenchError("原對話檢視功能目前不可用", status_code=503)
        try:
            secret = self._api_key_provider()
            document = exporter(
                conversation_id,
                theme="auto",
                redact_value=lambda value: _redact_secret_values(value, secret),
            )
        except Exception:
            raise WorkbenchError(
                "原對話目前無法讀取或安全呈現", status_code=502
            ) from None
        if not isinstance(document, str) or not document.lstrip().lower().startswith(
            "<!doctype html"
        ):
            raise WorkbenchError("原對話匯出結果格式無效", status_code=502)
        try:
            document_size = len(document.encode("utf-8"))
        except UnicodeEncodeError:
            raise WorkbenchError("原對話匯出結果格式無效", status_code=502) from None
        if document_size > MAX_CONVERSATION_HTML_BYTES:
            raise WorkbenchError("原對話匯出結果格式無效", status_code=502)
        return document

    def annotate_classification(
        self,
        run_id: str,
        question_id: str,
        classification: str,
        reason: str,
        actor: str,
    ) -> dict[str, Any]:
        """在 checkpoint 外追加管理員標註，保留原始狀態與所有回合。"""

        allowed = {"missed_clarification", "answered_with_followup", "needs_review"}
        if classification not in allowed:
            raise WorkbenchError("人工分類類型無效", status_code=400)
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > MAX_CLASSIFICATION_REASON_CHARS
        ):
            raise WorkbenchError(
                f"人工複核理由不可空白且最多 {MAX_CLASSIFICATION_REASON_CHARS} 字",
                status_code=400,
            )
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 160:
            raise WorkbenchError("管理員識別資料無效", status_code=400)

        state = self._read_state(run_id)
        question = next(
            (
                item
                for item in state["questions"]
                if isinstance(item, dict) and item.get("id") == str(question_id)
            ),
            None,
        )
        if question is None:
            raise WorkbenchError("找不到評測題目", status_code=404)
        if question.get("status") not in {
            "completed",
            "failed",
            "needs_review",
            "awaiting_clarification",
        }:
            raise WorkbenchError("題目尚未產生可複核回合", status_code=409)

        lease = self._try_lease()
        if lease is None:
            raise WorkbenchError(
                "評測仍在執行，請完成後再保存人工註記", status_code=409
            )
        try:
            # 取得執行鎖後重新讀取，避免與 worker 同時寫入時使用舊狀態。
            state = self._read_state(run_id)
            question = next(
                (
                    item
                    for item in state["questions"]
                    if isinstance(item, dict) and item.get("id") == str(question_id)
                ),
                None,
            )
            if question is None or question.get("status") not in {
                "completed",
                "failed",
                "needs_review",
                "awaiting_clarification",
            }:
                raise WorkbenchError(
                    "題目狀態已變更，請重新載入後複核", status_code=409
                )
            annotations = self._read_annotations(run_id)
            annotations["annotations"].append(
                {
                    "question_id": str(question_id),
                    "classification": classification,
                    "original_status": question.get("status"),
                    "evidence_source": _clarification_review_source(question, state)
                    or "manual_review",
                    "actor": _redact_secret_values(
                        actor.strip(), self._api_key_provider()
                    ),
                    "reason": _redact_secret_values(
                        reason.strip(), self._api_key_provider()
                    ),
                    "recorded_at": _utc_now(),
                }
            )
            payload = (
                json.dumps(annotations, ensure_ascii=False, indent=2, allow_nan=False)
                + "\n"
            )
            self._ensure_storage()
            self._atomic_write(self._annotation_path(run_id), payload.encode("utf-8"))
        finally:
            self._release_lease(lease)
        return self.get_run(run_id)

    def _current_state(self) -> tuple[str, dict[str, Any], bool] | None:
        pointer = self._read_pointer()
        if pointer is None:
            return None
        run_id = pointer["run_id"]
        return run_id, self._read_state(run_id), pointer["paused"]

    def _load_runner(self, run_id: str) -> EvaluationRunner:
        if self._runner is not None and self._current_run_id == run_id:
            return self._runner
        try:
            runner = EvaluationRunner.resume(
                _KeyRedactingClient(self._client_factory(), self._api_key_provider()),
                store_path=self._state_path(run_id),
            )
        except WorkbenchError:
            raise
        except Exception:
            raise WorkbenchError(
                "無法連接評測模型；紀錄已保留", status_code=503
            ) from None
        self._runner = runner
        self._current_run_id = run_id
        return runner

    def sources(self) -> dict[str, Any]:
        try:
            preview = self._preview_bytes(
                SOURCE_FILENAME, self._read_source_file(), source_id=SOURCE_ID
            )
        except WorkbenchError as exc:
            if exc.status_code == 404:
                return {
                    "sources": [
                        {
                            "id": SOURCE_ID,
                            "name": SOURCE_FILENAME,
                            "available": False,
                        }
                    ],
                    "upload_max_bytes": self.max_upload_bytes,
                }
            raise
        return {
            "sources": [
                {
                    "id": SOURCE_ID,
                    "name": SOURCE_FILENAME,
                    "available": True,
                    "question_count": preview["question_count"],
                    "sha256": preview["sha256"],
                }
            ],
            "upload_max_bytes": self.max_upload_bytes,
        }

    def _read_source_file(self) -> bytes:
        try:
            if self.question_file.is_symlink() or not self.question_file.is_file():
                raise FileNotFoundError
            data = self.question_file.read_bytes()
        except OSError:
            raise WorkbenchError("核准題目檔目前不可讀取", status_code=404) from None
        if len(data) > self.max_upload_bytes:
            raise WorkbenchError("核准題目檔超過允許大小", status_code=413)
        return data

    def preview_source(self, source_id: str) -> dict[str, Any]:
        if source_id != SOURCE_ID:
            raise WorkbenchError("題目來源不在允許清單", status_code=400)
        data = self._read_source_file()
        return self._preview_bytes(SOURCE_FILENAME, data, source_id=source_id)

    def preview_upload(self, filename: str, data: bytes) -> dict[str, Any]:
        safe_name = _validate_upload_name(filename)
        return self._preview_bytes(safe_name, data, source_id="upload")

    def _preview_bytes(
        self, filename: str, data: bytes, *, source_id: str
    ) -> dict[str, Any]:
        if not isinstance(data, bytes) or len(data) > self.max_upload_bytes:
            raise WorkbenchError("題目檔超過允許大小", status_code=413)
        if b"\x00" in data:
            raise WorkbenchError("題目檔不可包含 NUL byte", status_code=400)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise WorkbenchError("題目檔必須是 UTF-8 編碼", status_code=400) from None
        try:
            questions = parse_numbered_questions(text)
        except EvaluationError as exc:
            raise WorkbenchError(str(exc), status_code=400) from None
        if len(questions) > self.max_questions:
            raise WorkbenchError(
                f"題目數不可超過 {self.max_questions} 題", status_code=413
            )
        return {
            "source_id": source_id,
            "filename": filename,
            "sha256": hashlib.sha256(data).hexdigest(),
            "question_count": len(questions),
            "questions": [
                {"id": item.id, "prompt": item.prompt, "source_line": item.source_line}
                for item in questions
            ],
        }

    def start_source(
        self,
        source_id: str,
        expected_sha256: str,
        selected_question_ids: Any = None,
    ) -> dict[str, Any]:
        if source_id != SOURCE_ID:
            raise WorkbenchError("題目來源不在允許清單", status_code=400)
        data = self._read_source_file()
        return self._start_run(
            SOURCE_FILENAME,
            data,
            expected_sha256=expected_sha256,
            selected_question_ids=selected_question_ids,
        )

    def start_upload(
        self,
        filename: str,
        data: bytes,
        expected_sha256: str,
        selected_question_ids: Any = None,
    ) -> dict[str, Any]:
        safe_name = _validate_upload_name(filename)
        return self._start_run(
            safe_name,
            data,
            expected_sha256=expected_sha256,
            selected_question_ids=selected_question_ids,
        )

    def _start_run(
        self,
        filename: str,
        data: bytes,
        *,
        expected_sha256: str,
        selected_question_ids: Any = None,
    ) -> dict[str, Any]:
        if SHA256_PATTERN.fullmatch(expected_sha256) is None:
            raise WorkbenchError("需先預覽相同題目檔後才能開始", status_code=400)
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_sha256:
            raise WorkbenchError("題目檔在預覽後已改變，請重新預覽", status_code=409)
        preview = self._preview_bytes(filename, data, source_id="selected")
        try:
            selected_ids = select_question_ids(
                [question["id"] for question in preview["questions"]],
                selected_question_ids,
            )
        except EvaluationError as exc:
            raise WorkbenchError(str(exc), status_code=400) from None
        self._ensure_storage()
        lease = None
        staging_path: Path | None = None
        try:
            try:
                source_client = self._client_factory()
                model_snapshot = self._model_snapshot_provider(source_client)
                data_snapshot = self._data_snapshot_provider()
            except WorkbenchError:
                raise
            except Exception:
                raise WorkbenchError(
                    "無法取得模型或資料快照；評測尚未開始", status_code=503
                ) from None
            if not isinstance(model_snapshot, dict) or not isinstance(
                data_snapshot, dict
            ):
                raise WorkbenchError("模型或資料快照格式無效", status_code=503)

            run_id = uuid.uuid4().hex
            staging_path = self._write_staged_questions(run_id, filename, data)
            store_path = self._state_path(run_id)
            runner = EvaluationRunner.start(
                _KeyRedactingClient(source_client, self._api_key_provider()),
                question_file=staging_path,
                store_path=store_path,
                model_snapshot=model_snapshot,
                data_snapshot=data_snapshot,
                run_id=run_id,
                selected_question_ids=selected_ids,
                max_attempts=1,
            )
            # 新 run 的 checkpoint 已由 start 寫入；不要呼叫 snapshot，因其會
            # 取得 runner 的全域執行鎖，忙碌時便無法先保存佇列工作。
            state = self._read_state(run_id)
            question_source = state.get("manifest", {}).get("question_source", {})
            if (
                question_source.get("sha256") != preview["sha256"]
                or question_source.get("question_count") != preview["question_count"]
                or question_source.get("selected_question_ids") != selected_ids
                or [item.get("id") for item in state.get("questions", [])]
                != selected_ids
            ):
                store_path.unlink(missing_ok=True)
                raise WorkbenchError("題目檔快照不一致；評測尚未開始", status_code=409)
            previous_pointer = self._read_pointer()
            with self._clarification_queue_guard():
                payload = self._read_queue_payload_unlocked()
                items = self._migrate_legacy_queue_unlocked()
                items = self._ensure_run_work_enqueued_unlocked(run_id, state, items)
                paused_ids = set(payload["paused_run_ids"])
                if previous_pointer and previous_pointer["paused"]:
                    paused_ids.add(previous_pointer["run_id"])
                paused_ids.discard(run_id)
                paused_ids = [value for value in paused_ids]
                self._write_clarification_queue_unlocked(
                    items, paused_run_ids=paused_ids
                )
            self._runner = runner
            self._current_run_id = run_id
            self._last_worker_error = None
            self._launch_worker_if_available(run_id, runner)
            return self.status(run_id)
        except EvaluationError as exc:
            raise WorkbenchError(str(exc), status_code=400) from None
        finally:
            if staging_path is not None:
                staging_path.unlink(missing_ok=True)
                try:
                    staging_path.parent.rmdir()
                except OSError:
                    pass
            self._release_lease(lease)

    def _write_staged_questions(self, run_id: str, filename: str, data: bytes) -> Path:
        folder = self.staging_dir / run_id
        folder.mkdir(mode=0o700)
        _chmod_private(folder, 0o700)
        path = folder / filename
        try:
            with path.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _chmod_private(path, 0o600)
        except OSError:
            path.unlink(missing_ok=True)
            try:
                folder.rmdir()
            except OSError:
                pass
            raise WorkbenchError("無法暫存題目檔", status_code=500) from None
        return path

    def stop(self, expected_run_id: str | None = None) -> dict[str, Any]:
        current = (
            self._selected_state(expected_run_id)
            if expected_run_id is not None
            else self._current_state()
        )
        if current is None:
            raise WorkbenchError("目前沒有可停止的 run", status_code=404)
        run_id, _state, paused = current
        if paused:
            pointer = self._read_pointer()
            if (
                expected_run_id is not None
                and pointer is not None
                and pointer["run_id"] != run_id
            ):
                raise WorkbenchError("目前 run 已切換，請重新載入狀態", status_code=409)
            raise WorkbenchError("選取的 run 已停止", status_code=409)
        if not self._queue_has_work(run_id):
            raise WorkbenchError("選取的 run 目前沒有可停止工作", status_code=409)
        self._set_run_paused(run_id, True)
        pointer = self._read_pointer()
        if pointer is not None and pointer["run_id"] == run_id:
            self._write_pointer(
                run_id,
                paused=True,
                expected_current_run_id=run_id,
            )
        return self.status(run_id)

    def _selected_state(
        self, run_id: str | None
    ) -> tuple[str, dict[str, Any], bool] | None:
        if run_id is None:
            return self._current_state()
        if RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise WorkbenchError("run ID 格式無效", status_code=404)
        current = self._current_state()
        state = self._read_state(run_id)
        is_current = current is not None and current[0] == run_id
        return run_id, state, current[2] if is_current else self._run_is_paused(run_id)

    def resume(self, run_id: str | None = None) -> dict[str, Any]:
        current = self._selected_state(run_id)
        if current is None:
            raise WorkbenchError("目前沒有可續跑的 run", status_code=404)
        run_id, state, _paused = current
        waiting = any(
            item.get("status") == "awaiting_clarification"
            for item in state["questions"]
        )
        runnable = any(
            item.get("status") in {"pending", "running"} for item in state["questions"]
        )
        has_queued_clarification = self._queue_has_work(run_id)
        if _is_terminal(state) and not has_queued_clarification:
            raise WorkbenchError("選取的 run 已完成", status_code=409)
        if not runnable and not has_queued_clarification:
            message = "請先補答待澄清題目" if waiting else "目前沒有可續跑題目"
            raise WorkbenchError(message, status_code=409)
        runner = self._load_runner(run_id)
        with self._clarification_queue_guard():
            payload = self._read_queue_payload_unlocked()
            items = self._migrate_legacy_queue_unlocked()
            items = self._ensure_run_work_enqueued_unlocked(run_id, state, items)
            paused_ids = [
                value for value in payload["paused_run_ids"] if value != run_id
            ]
            self._write_clarification_queue_unlocked(items, paused_run_ids=paused_ids)
            pointer = self._read_pointer()
            if pointer and pointer["run_id"] == run_id and pointer["paused"]:
                self._write_pointer(
                    run_id,
                    paused=False,
                    expected_current_run_id=run_id,
                )
            self._last_worker_error = None
            self._launch_worker_if_available(run_id, runner)
        return self.status(run_id)

    def clarify(
        self, question_id: str, answer: str, *, run_id: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(question_id, str) or not question_id or len(question_id) > 32:
            raise WorkbenchError("題目 ID 格式無效", status_code=400)
        if (
            not isinstance(answer, str)
            or not answer.strip()
            or len(answer) > MAX_ANSWER_CHARS
        ):
            raise WorkbenchError(
                f"補答不可空白且最多 {MAX_ANSWER_CHARS} 字", status_code=400
            )
        current = self._selected_state(run_id)
        if current is None:
            raise WorkbenchError("目前沒有可補答的 run", status_code=404)
        run_id, state, _paused = current
        question = next(
            (item for item in state["questions"] if item.get("id") == question_id),
            None,
        )
        identity = (
            self._clarification_identity(
                run_id,
                question,
                allow_legacy_event_conflict=(
                    state.get("clarification_policy_version")
                    == "requestClarification-event-v1"
                ),
            )
            if isinstance(question, dict)
            else None
        )
        accepted = False
        with self._clarification_queue_guard():
            items = self._migrate_legacy_queue_unlocked()
            active_for_question = [
                item
                for item in items
                if item.get("run_id") == run_id
                and item.get("question_id") == question_id
                and self._queue_work_type(item) == "clarification"
                and item.get("status") in {"queued", "executing"}
            ]
            duplicate = next(
                (
                    item
                    for item in active_for_question
                    if identity is None
                    or item.get("clarification_id") == identity["clarification_id"]
                ),
                None,
            )
            if duplicate is not None:
                accepted = True
            if any(item.get("status") == "executing" for item in active_for_question):
                accepted = True
            if accepted:
                pass
            elif identity is None:
                raise WorkbenchError("該題目前不等待澄清", status_code=409)
            else:
                # 同來源回合至多保留一個未開始答案；過期草稿不會送到原對話。
                items = [item for item in items if item not in active_for_question]
                now = _utc_now_milliseconds()
                items.append(
                    {
                        "queue_id": uuid.uuid4().hex,
                        "work_type": "clarification",
                        "run_id": run_id,
                        "question_id": question_id,
                        "conversation_id": identity["conversation_id"],
                        "clarification_id": identity["clarification_id"],
                        "source_operation_id": identity["source_operation_id"],
                        "legacy_event_conflict": identity["legacy_event_conflict"]
                        == "true",
                        "answer": answer.strip(),
                        "status": "queued",
                        "created_at": now,
                        "submitted_at": now,
                        "started_at": None,
                    }
                )
                self._write_clarification_queue_unlocked(items)
                self._launch_worker_if_available(run_id)
        result = self.status(run_id)
        result["already_queued"] = accepted
        return result

    def update_queued_clarification(
        self, run_id: str, question_id: str, answer: str
    ) -> dict[str, Any]:
        self._validate_queued_answer(answer)
        state = self._read_state(run_id)
        question = next(
            (item for item in state["questions"] if item.get("id") == question_id),
            None,
        )
        identity = (
            self._clarification_identity(
                run_id,
                question,
                allow_legacy_event_conflict=(
                    state.get("clarification_policy_version")
                    == "requestClarification-event-v1"
                ),
            )
            if isinstance(question, dict)
            else None
        )
        with self._clarification_queue_guard():
            items = self._read_clarification_queue_unlocked()
            entry = next(
                (
                    item
                    for item in items
                    if item.get("run_id") == run_id
                    and item.get("question_id") == question_id
                    and self._queue_work_type(item) == "clarification"
                    and item.get("status") in {"queued", "executing"}
                ),
                None,
            )
            if entry is None:
                raise WorkbenchError("找不到尚未執行的補答", status_code=404)
            if entry.get("status") != "queued":
                raise WorkbenchError("補答已開始執行，無法修改", status_code=409)
            if identity is None or identity["clarification_id"] != entry.get(
                "clarification_id"
            ):
                raise WorkbenchError(
                    "補答對應的澄清已過期，請重新載入", status_code=409
                )
            entry["answer"] = answer.strip()
            entry["submitted_at"] = _utc_now_milliseconds()
            self._write_clarification_queue_unlocked(items)
        return self.status(run_id)

    def cancel_queued_clarification(
        self, run_id: str, question_id: str
    ) -> dict[str, Any]:
        with self._clarification_queue_guard():
            items = self._read_clarification_queue_unlocked()
            entry = next(
                (
                    item
                    for item in items
                    if item.get("run_id") == run_id
                    and item.get("question_id") == question_id
                    and self._queue_work_type(item) == "clarification"
                    and item.get("status") in {"queued", "executing"}
                ),
                None,
            )
            if entry is None:
                raise WorkbenchError("找不到尚未執行的補答", status_code=404)
            if entry.get("status") != "queued":
                raise WorkbenchError("補答已開始執行，無法取消", status_code=409)
            items.remove(entry)
            self._write_clarification_queue_unlocked(items)
        return self.status(run_id)

    @staticmethod
    def _validate_queued_answer(answer: str) -> None:
        if (
            not isinstance(answer, str)
            or not answer.strip()
            or len(answer) > MAX_ANSWER_CHARS
        ):
            raise WorkbenchError(
                f"補答不可空白且最多 {MAX_ANSWER_CHARS} 字", status_code=400
            )

    def recover_after_restart(self) -> bool:
        """只讀回 queue 已開始且 checkpoint 已保存 operation 的請求。"""

        try:
            with self._clarification_queue_guard():
                payload = self._read_queue_payload_unlocked()
                paused_ids = set(payload["paused_run_ids"])
                pointer = self._read_pointer()
                if pointer and pointer["paused"]:
                    paused_ids.add(pointer["run_id"])
                recoverable = next(
                    (
                        item
                        for item in payload["items"]
                        if item.get("status") == "executing"
                        and item.get("run_id") not in paused_ids
                        and self._queue_item_has_inflight_request(item)
                    ),
                    None,
                )
                if recoverable is None:
                    return False
                return self._launch_worker_if_available(
                    recoverable["run_id"], recovery_only=True
                )
        except WorkbenchError:
            return False

    def shutdown(self) -> None:
        """停用或程序關閉時保留 checkpoint，阻止開始下一題。"""

        with self._meta_lock:
            if self._worker_stop_event is not None:
                self._worker_stop_event.set()

    def status(self, run_id: str | None = None) -> dict[str, Any]:
        current = self._current_state()
        local_worker_running = self._worker_is_running()
        busy_elsewhere = False if local_worker_running else self._has_other_worker()
        worker_running = local_worker_running or busy_elsewhere
        if current is None:
            if run_id is not None:
                selected = self._selected_state(run_id)
                if selected is None:
                    raise WorkbenchError("找不到評測紀錄", status_code=404)
                selected_id, selected_state, selected_paused = selected
                annotations = self._read_annotations(selected_id)
                selected_has_queue_work = self._queue_has_work(selected_id)
                selected_has_runnable_questions = any(
                    item.get("status") in {"pending", "running"}
                    for item in selected_state["questions"]
                )
                if selected_paused and selected_has_queue_work:
                    selected_run_status = "stopping" if worker_running else "stopped"
                elif worker_running and not selected_paused and selected_has_queue_work:
                    selected_run_status = "queued"
                else:
                    selected_run_status = _run_status(
                        selected_state,
                        worker_running=False,
                        paused=selected_paused,
                    )
                return {
                    "run": _redact_secret_values(
                        _state_for_ui(
                            selected_state,
                            annotations=annotations["annotations"],
                            clarification_queue=self._clarification_queue_items(
                                selected_id
                            ),
                        ),
                        self._api_key_provider(),
                    ),
                    "run_status": selected_run_status,
                    "worker_running": worker_running,
                    "busy_elsewhere": busy_elsewhere,
                    "can_start": True,
                    "can_stop": not selected_paused and selected_has_queue_work,
                    "can_resume": (
                        (
                            selected_paused
                            or not selected_has_queue_work
                            or not worker_running
                        )
                        and (
                            selected_has_queue_work
                            or (
                                not _is_terminal(selected_state)
                                and selected_has_runnable_questions
                            )
                        )
                    ),
                    "run_id": selected_id,
                    "active_run_id": None,
                    "last_error": None,
                }
            return {
                "run": None,
                "run_status": "idle",
                "worker_running": worker_running,
                "busy_elsewhere": busy_elsewhere,
                "can_start": True,
                "can_stop": False,
                "can_resume": False,
                "last_error": self._last_worker_error,
            }
        active_run_id, active_state, _active_paused = current
        selected_run_id = run_id or active_run_id
        selected_state = (
            active_state
            if selected_run_id == active_run_id
            else self._read_state(selected_run_id)
        )
        selected_is_active = selected_run_id == active_run_id
        selected_paused = self._run_is_paused(selected_run_id)
        selected_has_queue_work = self._queue_has_work(selected_run_id)
        selected_has_runnable_questions = any(
            item.get("status") in {"pending", "running"}
            for item in selected_state["questions"]
        )
        selected_worker_running = worker_running if selected_is_active else False
        if selected_paused and selected_has_queue_work:
            run_status = "stopping" if selected_worker_running else "stopped"
        elif (
            not selected_is_active
            and worker_running
            and not selected_paused
            and selected_has_queue_work
        ):
            run_status = "queued"
        else:
            run_status = _run_status(
                selected_state,
                worker_running=selected_worker_running,
                paused=selected_paused,
            )
        annotations = self._read_annotations(selected_run_id)
        return {
            "run": _redact_secret_values(
                _state_for_ui(
                    selected_state,
                    annotations=annotations["annotations"],
                    clarification_queue=self._clarification_queue_items(
                        selected_run_id
                    ),
                ),
                self._api_key_provider(),
            ),
            "run_status": run_status,
            "worker_running": worker_running,
            "busy_elsewhere": busy_elsewhere,
            "can_start": True,
            "can_stop": not selected_paused and selected_has_queue_work,
            "can_resume": (
                (selected_paused or not selected_has_queue_work or not worker_running)
                and (
                    selected_has_queue_work
                    or (
                        not _is_terminal(selected_state)
                        and selected_has_runnable_questions
                    )
                )
            ),
            "run_id": selected_run_id,
            "active_run_id": active_run_id,
            "last_error": self._last_worker_error if selected_is_active else None,
        }

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        self._ensure_storage()
        work_queue = self._clarification_queue_items()
        paths = sorted(
            (
                path
                for path in self.runs_dir.glob("*.json")
                if RUN_ID_PATTERN.fullmatch(path.stem) and not path.is_symlink()
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[: max(1, min(limit, 50))]
        runs = []
        for path in paths:
            try:
                state = self._read_state(path.stem)
            except WorkbenchError:
                continue
            overview = _state_overview(
                state,
                work_queue=[
                    item for item in work_queue if item.get("run_id") == state["run_id"]
                ],
            )
            overview["classification_annotation_count"] = len(
                self._read_annotations(state["run_id"])["annotations"]
            )
            runs.append(_redact_secret_values(overview, self._api_key_provider()))
        return runs

    def download_json(self, run_id: str) -> bytes:
        state = self._read_state(run_id)
        state["classification_annotations"] = self._read_annotations(run_id)[
            "annotations"
        ]
        state = _redact_secret_values(state, self._api_key_provider())
        return (
            json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")

    def download_summary(self, run_id: str) -> bytes:
        state = self._read_state(run_id)
        state["classification_annotations"] = self._read_annotations(run_id)[
            "annotations"
        ]
        summary = _render_summary(state)
        summary = _redact_secret_values(summary, self._api_key_provider())
        return summary.encode("utf-8")

    def report_html(self, run_id: str) -> bytes:
        state = self._read_state(run_id)
        state = _redact_secret_values(state, self._api_key_provider())
        try:
            document, _chart_count = build_evaluation_report_html(
                state, plotly_js_provider=self._plotly_javascript_provider
            )
        except EvaluationReportError as exc:
            raise WorkbenchError(str(exc), status_code=503) from None
        return document.encode("utf-8")

    def _has_other_worker(self) -> bool:
        lease = self._try_lease()
        if lease is None:
            return True
        self._release_lease(lease)
        return False

    def _worker_is_running(self) -> bool:
        with self._meta_lock:
            return self._worker is not None and self._worker.is_alive()

    def _queue_item_is_ready(
        self, item: dict[str, Any], *, now: datetime | None = None
    ) -> bool:
        if item.get("status") == "queued":
            return True
        if item.get("status") != "executing":
            return False
        retry_at = self._clarification_retry_at(item)
        return retry_at is None or retry_at <= (now or datetime.now(timezone.utc))

    def _queue_item_has_inflight_request(self, item: dict[str, Any]) -> bool:
        """重啟復原只挑已有 pending_turn/operation，不取未開始 queue 項目。"""

        try:
            state = self._read_state(item["run_id"])
        except WorkbenchError:
            return False
        question = next(
            (q for q in state["questions"] if q.get("id") == item["question_id"]),
            None,
        )
        if not isinstance(question, dict):
            return False
        pending = question.get("pending_turn")
        if not isinstance(pending, dict):
            return self._queue_item_already_finished(item, question)
        attempts = pending.get("attempts")
        return bool(
            pending.get("operation_id") and isinstance(attempts, list) and attempts
        )

    @staticmethod
    def _queue_item_already_finished(
        item: dict[str, Any], question: dict[str, Any]
    ) -> bool:
        queue_id = item.get("queue_id")
        return any(
            isinstance(turn, dict) and turn.get("queue_id") == queue_id
            for turn in question.get("turns", [])
        )

    def _claim_next_work_unlocked(
        self, *, recovery_only: bool = False
    ) -> dict[str, Any] | None:
        payload = self._read_queue_payload_unlocked()
        items = payload["items"]
        paused_ids = set(payload["paused_run_ids"])
        pointer = self._read_pointer()
        if pointer and pointer["paused"]:
            paused_ids.add(pointer["run_id"])
        now = datetime.now(timezone.utc)
        for item in items:
            if item.get("status") not in {"queued", "executing"}:
                continue
            if recovery_only and item.get("status") != "executing":
                continue
            if item.get("run_id") in paused_ids or not self._queue_item_is_ready(
                item, now=now
            ):
                continue
            if recovery_only and not self._queue_item_has_inflight_request(item):
                continue
            if "work_type" not in item:
                item["work_type"] = "clarification"
            if item.get("status") == "queued":
                item["status"] = "executing"
                item["started_at"] = _utc_now()
            item.pop("next_attempt_at", None)
            self._write_clarification_queue_unlocked(
                items, paused_run_ids=list(paused_ids)
            )
            return json.loads(json.dumps(item, ensure_ascii=False))
        return None

    def _next_work_wait_seconds(self, *, recovery_only: bool = False) -> float | None:
        with self._clarification_queue_guard():
            return self._next_work_wait_seconds_unlocked(recovery_only=recovery_only)

    def _next_work_wait_seconds_unlocked(
        self, *, recovery_only: bool = False
    ) -> float | None:
        now = datetime.now(timezone.utc)
        payload = self._read_queue_payload_unlocked()
        paused_ids = set(payload["paused_run_ids"])
        pointer = self._read_pointer()
        if pointer and pointer["paused"]:
            paused_ids.add(pointer["run_id"])
        retry_times = [
            retry_at
            for item in payload["items"]
            if item.get("status") == "executing"
            and item.get("run_id") not in paused_ids
            and (not recovery_only or self._queue_item_has_inflight_request(item))
            and (retry_at := self._clarification_retry_at(item)) is not None
            and retry_at > now
        ]
        return min(
            ((retry_at - now).total_seconds() for retry_at in retry_times),
            default=None,
        )

    def _remove_clarification(self, queue_id: str) -> None:
        self._remove_work_item(queue_id)

    def _remove_work_item(self, queue_id: str) -> None:
        with self._clarification_queue_guard():
            payload = self._read_queue_payload_unlocked()
            remaining = [
                item for item in payload["items"] if item.get("queue_id") != queue_id
            ]
            if len(remaining) != len(payload["items"]):
                self._write_clarification_queue_unlocked(
                    remaining, paused_run_ids=payload["paused_run_ids"]
                )

    def _defer_work_item(self, queue_id: str, next_attempt_at: str | None) -> None:
        with self._clarification_queue_guard():
            payload = self._read_queue_payload_unlocked()
            item = next(
                (
                    value
                    for value in payload["items"]
                    if value.get("queue_id") == queue_id
                ),
                None,
            )
            if item is None:
                return
            item["status"] = "executing"
            item["next_attempt_at"] = next_attempt_at or (
                datetime.now(timezone.utc) + timedelta(seconds=1)
            ).isoformat(timespec="milliseconds")
            self._write_clarification_queue_unlocked(
                payload["items"], paused_run_ids=payload["paused_run_ids"]
            )

    def _work_item_delay(self, item: dict[str, Any], question: dict[str, Any]) -> str:
        pending = question.get("pending_turn")
        attempts = pending.get("attempts") if isinstance(pending, dict) else None
        latest = attempts[-1] if isinstance(attempts, list) and attempts else None
        if isinstance(latest, dict) and isinstance(
            latest.get("recovery_not_before"), str
        ):
            return latest["recovery_not_before"]
        return (
            datetime.now(timezone.utc)
            + timedelta(
                seconds=EvaluationRunner.CLARIFICATION_RECOVERY_BACKOFF_INITIAL_SECONDS
            )
        ).isoformat(timespec="milliseconds")

    def _execute_retry_item(
        self, item: dict[str, Any], runner: EvaluationRunner
    ) -> None:
        state = runner.snapshot()
        question = next(
            (q for q in state["questions"] if q.get("id") == item["question_id"]),
            None,
        )
        if not isinstance(question, dict):
            raise EvaluationError("找不到人工重試題目")
        expected_count = item["expected_retry_count"]
        if (
            question.get("conversation_id") != item.get("expected_conversation_id")
            or not question.get("turns")
            or question["turns"][-1].get("operation_id")
            != item.get("expected_operation_id")
        ):
            raise EvaluationError("原對話或失敗回合已變更，取消人工重試")
        if (
            question.get("status") == "failed"
            and question.get("manual_retry_count", 0) == expected_count
        ):
            runner.retry_failed(item["question_id"], execute=False)
        elif not (
            question.get("status") == "running"
            and question.get("manual_retry_count", 0) == expected_count + 1
            and isinstance(question.get("pending_turn"), dict)
            and question["pending_turn"].get("manual_retry") is True
        ):
            # 完成／失敗的已預約 retry 是重啟後的終態；不會再建立第二次。
            if question.get("manual_retry_count", 0) == expected_count + 1 and any(
                turn.get("manual_retry") is True and turn.get("kind") == "retry"
                for turn in question.get("turns", [])
                if isinstance(turn, dict)
            ):
                return
            raise EvaluationError("人工重試已過期或狀態不符")
        runner.execute_reserved_retry(item["question_id"])

    def _execute_work_item(self, item: dict[str, Any]) -> None:
        run_id = item["run_id"]
        question_id = item["question_id"]
        queue_id = item["queue_id"]
        work_type = self._queue_work_type(item)
        try:
            runner = self._load_runner(run_id)
            if work_type == "question":
                runner.run_queued_question(question_id)
            elif work_type == "retry":
                self._execute_retry_item(item, runner)
            elif work_type == "clarification":
                state = self._read_state(run_id)
                question = next(
                    (q for q in state["questions"] if q.get("id") == question_id),
                    None,
                )
                already_started = (
                    isinstance(question, dict)
                    and isinstance(question.get("pending_turn"), dict)
                    and question["pending_turn"].get("queue_id") == queue_id
                )
                already_finished = isinstance(
                    question, dict
                ) and self._queue_item_already_finished(item, question)
                identity = (
                    self._clarification_identity(
                        run_id,
                        question,
                        allow_legacy_event_conflict=(
                            state.get("clarification_policy_version")
                            == "requestClarification-event-v1"
                        ),
                    )
                    if isinstance(question, dict)
                    else None
                )
                if (
                    not already_started
                    and not already_finished
                    and (
                        identity is None
                        or identity.get("clarification_id")
                        != item.get("clarification_id")
                    )
                ):
                    raise EvaluationError("補答對應的澄清已過期")
                runner.submit_clarification(
                    question_id,
                    item["answer"],
                    queue_id=queue_id,
                    expected_conversation_id=item["conversation_id"],
                    expected_source_operation_id=item["source_operation_id"],
                    submitted_at=item["submitted_at"],
                    allow_legacy_event_conflict=(
                        item.get("legacy_event_conflict") is True
                        and state.get("clarification_policy_version")
                        == "requestClarification-event-v1"
                    ),
                )
            state = self._read_state(run_id)
            question = next(
                (q for q in state["questions"] if q.get("id") == question_id),
                None,
            )
            if not isinstance(question, dict):
                self._remove_work_item(queue_id)
                return
            pending = question.get("pending_turn")
            if question.get("status") == "running" and isinstance(pending, dict):
                self._defer_work_item(queue_id, self._work_item_delay(item, question))
            elif question.get("status") == "running" and work_type == "question":
                self._defer_work_item(queue_id, self._work_item_delay(item, question))
            else:
                self._remove_work_item(queue_id)
        except EvaluationError:
            self._remove_work_item(queue_id)
        except WorkbenchError as exc:
            self._last_worker_error = type(exc).__name__
            if exc.status_code != 404:
                try:
                    state = self._read_state(run_id)
                    question = next(
                        (q for q in state["questions"] if q.get("id") == question_id),
                        None,
                    )
                    if (
                        isinstance(question, dict)
                        and question.get("status") == "running"
                        and isinstance(question.get("pending_turn"), dict)
                        and question["pending_turn"].get("attempts")
                    ):
                        self._defer_work_item(
                            queue_id, self._work_item_delay(item, question)
                        )
                        return
                except WorkbenchError:
                    pass
            self._remove_work_item(queue_id)
        except Exception as exc:
            self._last_worker_error = type(exc).__name__
            try:
                state = self._read_state(run_id)
                question = next(
                    (q for q in state["questions"] if q.get("id") == question_id),
                    None,
                )
                if (
                    isinstance(question, dict)
                    and question.get("status") == "running"
                    and isinstance(question.get("pending_turn"), dict)
                    and question["pending_turn"].get("attempts")
                ):
                    self._defer_work_item(
                        queue_id, self._work_item_delay(item, question)
                    )
                    return
            except WorkbenchError:
                pass
            self._remove_work_item(queue_id)

    def retry_failed(
        self, run_id: str, question_id: str, expected_retry_count: int
    ) -> dict[str, Any]:
        """將人工 retry 持久追加到共享 FIFO，重複要求回報已排隊。"""
        with self._clarification_queue_guard():
            items = self._migrate_legacy_queue_unlocked()
            existing = next(
                (
                    item
                    for item in items
                    if item.get("run_id") == run_id
                    and item.get("question_id") == question_id
                    and self._queue_work_type(item) == "retry"
                    and item.get("status") in {"queued", "executing"}
                    and item.get("expected_retry_count") == expected_retry_count
                ),
                None,
            )
            if existing is not None:
                return {
                    "accepted": True,
                    "already_queued": True,
                    "run_id": run_id,
                    "question_id": question_id,
                    "work_type": "retry",
                }

            # 在共享鎖內重讀 checkpoint，避免舊頁面雙擊時把已開始或已完成的
            # retry 再次排入；工作身份固定綁定失敗 turn 的 operation。
            state = self._read_state(run_id)
            question = next(
                (q for q in state["questions"] if q.get("id") == question_id), None
            )
            if (
                question is None
                or question.get("status") != "failed"
                or question.get("pending_turn") is not None
            ):
                raise WorkbenchError("此 run 的題目不是可重試失敗題", status_code=409)
            if question.get("manual_retry_count", 0) != expected_retry_count:
                raise WorkbenchError(
                    "此失敗回合已要求重試，請重新載入", status_code=409
                )
            turns = question.get("turns")
            last_turn = turns[-1] if isinstance(turns, list) and turns else None
            if (
                not isinstance(last_turn, dict)
                or not isinstance(question.get("conversation_id"), str)
                or not isinstance(last_turn.get("operation_id"), str)
            ):
                raise WorkbenchError(
                    "無法確認失敗回合身份，未排入人工重試", status_code=409
                )
            items.append(
                {
                    "queue_id": uuid.uuid4().hex,
                    "work_type": "retry",
                    "run_id": run_id,
                    "question_id": question_id,
                    "expected_retry_count": expected_retry_count,
                    "expected_conversation_id": question["conversation_id"],
                    "expected_operation_id": last_turn["operation_id"],
                    "status": "queued",
                    "accepted_at": _utc_now_milliseconds(),
                }
            )
            self._write_clarification_queue_unlocked(items)
            self._launch_worker_if_available(run_id)
        return {
            "accepted": True,
            "already_queued": False,
            "run_id": run_id,
            "question_id": question_id,
            "work_type": "retry",
        }

    def _launch_worker_if_available(
        self,
        run_id: str,
        runner: EvaluationRunner | None = None,
        *,
        recovery_only: bool = False,
    ) -> bool:
        lease = self._try_lease()
        if lease is None:
            return False
        try:
            if runner is not None:
                self._runner = runner
            self._write_pointer(run_id, paused=False)
            self._last_worker_error = None
            self._launch_worker(lease, run_id, recovery_only=recovery_only)
            lease = None
            return True
        finally:
            self._release_lease(lease)

    def _launch_worker(
        self,
        lease: _ExecutionLease,
        run_id: str,
        *,
        recovery_only: bool = False,
    ) -> None:
        with self._meta_lock:
            if self._worker is not None and self._worker.is_alive():
                raise WorkbenchError("另一個評測工作正在執行", status_code=409)
            if not isinstance(run_id, str) or RUN_ID_PATTERN.fullmatch(run_id) is None:
                raise WorkbenchError("評測 run ID 無效", status_code=500)
            stop_event = threading.Event()
            self._lease = lease
            worker = threading.Thread(
                target=self._worker_main,
                args=(lease, run_id, stop_event, recovery_only),
                daemon=True,
                name="badmintonai-evaluation-queue",
            )
            self._worker = worker
            self._worker_run_id = run_id
            self._worker_stop_event = stop_event
            worker.start()

    def _worker_main(
        self,
        lease: _ExecutionLease,
        initial_run_id: str,
        stop_event: threading.Event,
        recovery_only: bool,
    ) -> None:
        try:
            while not stop_event.is_set():
                with self._clarification_queue_guard():
                    item = self._claim_next_work_unlocked(recovery_only=recovery_only)
                    wait_seconds = self._next_work_wait_seconds_unlocked(
                        recovery_only=recovery_only
                    )
                    if item is None and wait_seconds is None:
                        # 和入佇列共用鎖：新工作要麼先被看見，要麼會在 lease
                        # 釋放後自行啟動 worker，不會落入 lost-wakeup 窗口。
                        self._clear_worker_and_release_lease(initial_run_id, lease)
                        lease = None
                        return
                if item is not None:
                    # 指標跟著真正取得執行權的 run 移動；僅被接受排隊的 run
                    # 不會遮蔽目前執行輪次的停止／續跑控制。
                    self._write_pointer(item["run_id"], paused=False)
                    with self._meta_lock:
                        self._worker_run_id = item["run_id"]
                    self._execute_work_item(item)
                    if recovery_only:
                        # 只回復啟動前已存在的模型操作；普通 queued 工作等明確續跑。
                        continue
                    continue
                if wait_seconds is not None:
                    stop_event.wait(min(max(wait_seconds, 0.01), 0.25))
        except Exception as exc:
            self._last_worker_error = type(exc).__name__
        finally:
            if lease is not None:
                with self._clarification_queue_guard():
                    self._clear_worker_and_release_lease(initial_run_id, lease)

    def _clear_worker_and_release_lease(
        self, run_id: str, lease: _ExecutionLease
    ) -> None:
        with self._meta_lock:
            if self._worker is threading.current_thread():
                self._worker_run_id = None
                self._worker_stop_event = None
            if self._lease is lease:
                self._lease = None
            if self._worker is threading.current_thread():
                self._worker = None
        self._release_lease(lease)


def create_api_router(
    workbench: EvaluationWorkbenchService,
    admin_dependency: Callable[..., Any],
    *,
    user_dependency: Callable[..., Any] | None = None,
    chat_provider: Any | None = None,
    chat_export_permission_checker: Callable[[Any], Any] | None = None,
    pdf_renderer: Callable[[str], Any] | None = None,
    plotly_javascript_provider: Callable[[], str] | None = None,
) -> APIRouter:
    """建立工作台管理員 API 與依原使用者權限保護的聊天匯出路由。"""

    router = APIRouter()
    current_user_dependency = user_dependency or admin_dependency
    render_pdf = pdf_renderer or render_html_to_pdf
    provide_plotly_javascript = plotly_javascript_provider or fetch_plotly_javascript

    async def require_chat_export_permission(user: Any) -> None:
        role = (
            user.get("role") if isinstance(user, dict) else getattr(user, "role", None)
        )
        if role == "admin":
            return

        if chat_export_permission_checker is not None:
            allowed = chat_export_permission_checker(user)
            if hasattr(allowed, "__await__"):
                allowed = await allowed
        else:
            try:
                from open_webui.config import Config
                from open_webui.utils.access_control import has_permission

                user_id = (
                    user.get("id")
                    if isinstance(user, dict)
                    else getattr(user, "id", None)
                )
                if not isinstance(user_id, str) or not user_id:
                    raise ValueError("目前登入者識別無效")
                defaults = await Config.get("user.permissions")
                allowed = await has_permission(user_id, "chat.export", defaults)
            except Exception:
                raise HTTPException(
                    status_code=503, detail="目前無法確認聊天匯出權限"
                ) from None

        if not allowed:
            raise HTTPException(status_code=403, detail="沒有匯出此對話的權限")

    async def load_authorized_chat(chat_id: str, user: Any) -> dict[str, Any]:
        if not CONVERSATION_ID_PATTERN.fullmatch(chat_id):
            raise HTTPException(status_code=404, detail="找不到此對話")
        provider = chat_provider
        if provider is None:
            try:
                from open_webui.models.chats import Chats
            except ImportError:
                raise HTTPException(
                    status_code=503, detail="對話匯出服務目前無法使用"
                ) from None
            provider = Chats
        try:
            chat_record = await provider.get_chat_by_id_for_user(chat_id, user)
        except Exception:
            raise HTTPException(
                status_code=503, detail="目前無法安全讀取已授權的對話"
            ) from None
        if chat_record is None:
            raise HTTPException(status_code=404, detail="找不到此對話")
        chat_payload = getattr(chat_record, "chat", None)
        if not isinstance(chat_payload, dict):
            raise HTTPException(status_code=422, detail="對話內容格式無法安全匯出")
        return chat_payload

    async def build_authorized_chat_html(chat_id: str, user: Any, *, theme: str) -> str:
        chat_payload = await load_authorized_chat(chat_id, user)
        try:
            document, _message_count, _chart_count, _warnings = await run_in_threadpool(
                build_chat_export_html,
                chat_payload,
                theme=theme,
                native_validation=False,
                plotly_javascript_provider=provide_plotly_javascript,
            )
        except ChatExportError:
            raise HTTPException(
                status_code=422,
                detail="對話內容目前無法安全匯出，請稍後重試或聯絡管理員",
            ) from None
        except Exception:
            raise HTTPException(
                status_code=502, detail="對話 HTML 產生失敗，請稍後重試"
            ) from None
        return document

    async def render_pdf_response(document: str, filename: str) -> Response:
        try:
            pdf_bytes = await render_pdf(document)
        except PDFRenderError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
        except Exception:
            raise HTTPException(
                status_code=502, detail="PDF 產生失敗，請稍後重試"
            ) from None
        return Response(
            pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            },
        )

    @router.get(
        ROUTES["badmintonai_evaluation_status"], name="badmintonai_evaluation_status"
    )
    async def status(
        run_id: str | None = None,
        user: Any = Depends(admin_dependency),
    ) -> dict[str, Any]:
        del user
        try:
            return await run_in_threadpool(workbench.status, run_id)
        except WorkbenchError as exc:
            _raise_http(exc)

    @router.get(
        ROUTES["badmintonai_evaluation_sources"], name="badmintonai_evaluation_sources"
    )
    async def sources(user: Any = Depends(admin_dependency)) -> dict[str, Any]:
        del user
        try:
            return await run_in_threadpool(workbench.sources)
        except WorkbenchError as exc:
            _raise_http(exc)

    @router.post(
        ROUTES["badmintonai_evaluation_preview"], name="badmintonai_evaluation_preview"
    )
    async def preview(
        request: Request,
        user: Any = Depends(admin_dependency),
    ) -> dict[str, Any]:
        del user
        _require_same_origin(request)
        payload = await _read_json_body(request)
        source_id = payload.get("source")
        if not isinstance(source_id, str):
            raise HTTPException(status_code=400, detail="題目來源格式無效")
        try:
            return await run_in_threadpool(workbench.preview_source, source_id)
        except WorkbenchError as exc:
            _raise_http(exc)

    @router.post(
        ROUTES["badmintonai_evaluation_preview_upload"],
        name="badmintonai_evaluation_preview_upload",
    )
    async def preview_upload(
        request: Request,
        user: Any = Depends(admin_dependency),
    ) -> dict[str, Any]:
        del user
        _require_same_origin(request)
        try:
            filename = _request_upload_filename(request)
            data = await _read_limited_body(request, workbench.max_upload_bytes)
            return await run_in_threadpool(workbench.preview_upload, filename, data)
        except WorkbenchError as exc:
            _raise_http(exc)

    @router.post(
        ROUTES["badmintonai_evaluation_start"], name="badmintonai_evaluation_start"
    )
    async def start(
        request: Request,
        user: Any = Depends(admin_dependency),
    ) -> dict[str, Any]:
        del user
        _require_same_origin(request)
        payload = await _read_json_body(request)
        source_id = payload.get("source")
        expected_sha256 = payload.get("sha256")
        if not isinstance(source_id, str) or not isinstance(expected_sha256, str):
            raise HTTPException(status_code=400, detail="開始評測參數格式無效")
        if "question_ids" in payload and not isinstance(payload["question_ids"], list):
            raise HTTPException(status_code=400, detail="題號選取格式無效")
        try:
            arguments = [source_id, expected_sha256]
            if "question_ids" in payload:
                arguments.append(payload["question_ids"])
            return await run_in_threadpool(workbench.start_source, *arguments)
        except WorkbenchError as exc:
            _raise_http(exc)

    @router.post(
        ROUTES["badmintonai_evaluation_start_upload"],
        name="badmintonai_evaluation_start_upload",
    )
    async def start_upload(
        request: Request,
        user: Any = Depends(admin_dependency),
    ) -> dict[str, Any]:
        del user
        _require_same_origin(request)
        try:
            filename = _request_upload_filename(request)
            expected_sha256 = request.headers.get("x-expected-sha256", "")
            selected_question_ids = _request_selected_question_ids(request)
            data = await _read_limited_body(request, workbench.max_upload_bytes)
            arguments = [filename, data, expected_sha256]
            if selected_question_ids is not None:
                arguments.append(selected_question_ids)
            return await run_in_threadpool(workbench.start_upload, *arguments)
        except WorkbenchError as exc:
            _raise_http(exc)

    @router.post(
        ROUTES["badmintonai_evaluation_stop"], name="badmintonai_evaluation_stop"
    )
    async def stop(
        request: Request,
        user: Any = Depends(admin_dependency),
    ) -> dict[str, Any]:
        del user
        _require_same_origin(request)
        try:
            expected_run_id = await _read_optional_run_id(request)
            return await run_in_threadpool(workbench.stop, expected_run_id)
        except WorkbenchError as exc:
            _raise_http(exc)

    @router.post(
        ROUTES["badmintonai_evaluation_resume"], name="badmintonai_evaluation_resume"
    )
    async def resume(
        request: Request,
        user: Any = Depends(admin_dependency),
    ) -> dict[str, Any]:
        del user
        _require_same_origin(request)
        try:
            run_id = await _read_optional_run_id(request)
            return await run_in_threadpool(workbench.resume, run_id)
        except WorkbenchError as exc:
            _raise_http(exc)

    @router.post(
        ROUTES["badmintonai_evaluation_retry"], name="badmintonai_evaluation_retry"
    )
    async def retry_failed(
        run_id: str,
        question_id: str,
        request: Request,
        user: Any = Depends(admin_dependency),
    ) -> dict[str, Any]:
        del user
        _require_same_origin(request)
        payload = await _read_json_body(request)
        expected = payload.get("expected_retry_count")
        if type(expected) is not int or expected < 0:
            raise HTTPException(status_code=400, detail="需提供目前人工重試次數")
        try:
            return await run_in_threadpool(
                workbench.retry_failed, run_id, question_id, expected
            )
        except WorkbenchError as exc:
            _raise_http(exc)

    @router.post(
        ROUTES["badmintonai_evaluation_clarify"], name="badmintonai_evaluation_clarify"
    )
    async def clarify(
        request: Request,
        user: Any = Depends(admin_dependency),
    ) -> dict[str, Any]:
        del user
        _require_same_origin(request)
        payload = await _read_json_body(request)
        question_id = payload.get("question_id")
        answer = payload.get("answer")
        run_id = payload.get("run_id")
        if (
            not isinstance(question_id, str)
            or not isinstance(answer, str)
            or (run_id is not None and not isinstance(run_id, str))
        ):
            raise HTTPException(status_code=400, detail="補答內容格式無效")
        try:
            return await run_in_threadpool(
                workbench.clarify, question_id, answer, run_id=run_id
            )
        except WorkbenchError as exc:
            _raise_http(exc)

    @router.patch(
        ROUTES["badmintonai_evaluation_clarification_queue_update"],
        name="badmintonai_evaluation_clarification_queue_update",
    )
    async def update_queued_clarification(
        run_id: str,
        question_id: str,
        request: Request,
        user: Any = Depends(admin_dependency),
    ) -> dict[str, Any]:
        del user
        _require_same_origin(request)
        payload = await _read_json_body(request)
        answer = payload.get("answer")
        if not isinstance(answer, str):
            raise HTTPException(status_code=400, detail="補答內容格式無效")
        try:
            return await run_in_threadpool(
                workbench.update_queued_clarification,
                run_id,
                question_id,
                answer,
            )
        except WorkbenchError as exc:
            _raise_http(exc)

    @router.delete(
        ROUTES["badmintonai_evaluation_clarification_queue_cancel"],
        name="badmintonai_evaluation_clarification_queue_cancel",
    )
    async def cancel_queued_clarification(
        run_id: str,
        question_id: str,
        request: Request,
        user: Any = Depends(admin_dependency),
    ) -> dict[str, Any]:
        del user
        _require_same_origin(request)
        try:
            return await run_in_threadpool(
                workbench.cancel_queued_clarification, run_id, question_id
            )
        except WorkbenchError as exc:
            _raise_http(exc)

    @router.get(
        ROUTES["badmintonai_evaluation_run"],
        name="badmintonai_evaluation_run",
    )
    async def run_detail(
        run_id: str,
        user: Any = Depends(admin_dependency),
    ) -> dict[str, Any]:
        del user
        try:
            return await run_in_threadpool(workbench.get_run, run_id)
        except WorkbenchError as exc:
            _raise_http(exc)

    @router.post(
        ROUTES["badmintonai_evaluation_classification_annotation"],
        name="badmintonai_evaluation_classification_annotation",
    )
    async def classification_annotation(
        run_id: str,
        question_id: str,
        request: Request,
        user: Any = Depends(admin_dependency),
    ) -> dict[str, Any]:
        _require_same_origin(request)
        payload = await _read_json_body(request)
        classification = payload.get("classification")
        reason = payload.get("reason")
        if not isinstance(classification, str) or not isinstance(reason, str):
            raise HTTPException(status_code=400, detail="人工分類註記格式無效")
        try:
            return await run_in_threadpool(
                workbench.annotate_classification,
                run_id,
                question_id,
                classification,
                reason,
                _admin_actor(user),
            )
        except WorkbenchError as exc:
            _raise_http(exc)

    @router.get(
        ROUTES["badmintonai_evaluation_conversation_html"],
        name="badmintonai_evaluation_conversation_html",
    )
    async def conversation_html(
        run_id: str,
        question_id: str,
        user: Any = Depends(admin_dependency),
    ) -> Response:
        del user
        try:
            body = await run_in_threadpool(
                workbench.conversation_html,
                run_id,
                question_id,
            )
        except WorkbenchError as exc:
            if exc.status_code == 404:
                message = "找不到此題的原對話，請使用「開啟原對話」連結確認。"
            elif exc.status_code == 503:
                message = "原對話預覽目前無法使用，請稍後重試或使用「開啟原對話」連結。"
            else:
                message = "原對話讀取失敗或內容無法安全呈現，請稍後重試或使用「開啟原對話」連結。"
            body = (
                '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                "<title>原對話無法顯示</title><style>"
                ":root{color-scheme:light dark;font-family:system-ui,sans-serif;}"
                "body{margin:0;padding:1.25rem;background:Canvas;color:CanvasText;}"
                "main{max-width:48rem;margin:0 auto;line-height:1.6;}"
                "</style></head><body><main><h1>原對話無法顯示</h1><p>"
                f"{html.escape(message)}</p></main></body></html>"
            )
            return Response(
                body,
                status_code=exc.status_code,
                media_type="text/html; charset=utf-8",
                headers={
                    "Content-Security-Policy": CONVERSATION_IFRAME_CSP,
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "SAMEORIGIN",
                    "Referrer-Policy": "no-referrer",
                    "Cache-Control": "no-store",
                },
            )
        return Response(
            body,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Security-Policy": CONVERSATION_IFRAME_CSP,
                "Content-Disposition": 'inline; filename="evaluation-conversation.html"',
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "SAMEORIGIN",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            },
        )

    @router.get(
        ROUTES["badmintonai_evaluation_runs"], name="badmintonai_evaluation_runs"
    )
    async def runs(user: Any = Depends(admin_dependency)) -> dict[str, Any]:
        del user
        try:
            return {"runs": await run_in_threadpool(workbench.recent_runs)}
        except WorkbenchError as exc:
            _raise_http(exc)

    @router.get(
        ROUTES["badmintonai_evaluation_download_json"],
        name="badmintonai_evaluation_download_json",
    )
    async def download_json(
        run_id: str,
        user: Any = Depends(admin_dependency),
    ) -> Response:
        del user
        try:
            body = await run_in_threadpool(workbench.download_json, run_id)
        except WorkbenchError as exc:
            _raise_http(exc)
        return Response(
            body,
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="evaluation-{run_id}.json"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    @router.get(
        ROUTES["badmintonai_evaluation_download_summary"],
        name="badmintonai_evaluation_download_summary",
    )
    async def download_summary(
        run_id: str,
        user: Any = Depends(admin_dependency),
    ) -> Response:
        del user
        try:
            body = await run_in_threadpool(workbench.download_summary, run_id)
        except WorkbenchError as exc:
            _raise_http(exc)
        return Response(
            body,
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="evaluation-{run_id}-summary.txt"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    @router.get(
        ROUTES["badmintonai_evaluation_download_html"],
        name="badmintonai_evaluation_download_html",
    )
    async def download_report_html(
        run_id: str,
        user: Any = Depends(admin_dependency),
    ) -> Response:
        del user
        try:
            body = await run_in_threadpool(workbench.report_html, run_id)
        except WorkbenchError as exc:
            _raise_http(exc)
        return Response(
            body,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="evaluation-{run_id}.html"',
                "Content-Security-Policy": REPORT_CSP + "; frame-ancestors 'none'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            },
        )

    @router.get(
        ROUTES["badmintonai_evaluation_print_report"],
        name="badmintonai_evaluation_print_report",
    )
    async def print_report(
        run_id: str,
        user: Any = Depends(admin_dependency),
    ) -> Response:
        del user
        try:
            body = await run_in_threadpool(workbench.report_html, run_id)
        except WorkbenchError as exc:
            _raise_http(exc)
        return Response(
            body,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": f'inline; filename="evaluation-{run_id}-print.html"',
                "Content-Security-Policy": REPORT_CSP + "; frame-ancestors 'none'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            },
        )

    @router.get(
        ROUTES["badmintonai_evaluation_download_pdf"],
        name="badmintonai_evaluation_download_pdf",
    )
    async def download_report_pdf(
        run_id: str,
        user: Any = Depends(admin_dependency),
    ) -> Response:
        del user
        try:
            report = await run_in_threadpool(workbench.report_html, run_id)
            document = report.decode("utf-8")
        except WorkbenchError as exc:
            _raise_http(exc)
        except UnicodeDecodeError:
            raise HTTPException(status_code=502, detail="HTML 報告編碼無效") from None
        return await render_pdf_response(document, f"evaluation-{run_id}.pdf")

    @router.get(
        ROUTES["badmintonai_chat_export_html"],
        name="badmintonai_chat_export_html",
    )
    async def download_chat_html(
        chat_id: str,
        user: Any = Depends(current_user_dependency),
    ) -> Response:
        await require_chat_export_permission(user)
        document = await build_authorized_chat_html(chat_id, user, theme="auto")
        return Response(
            document,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="badmintonai-chat-{chat_id}.html"',
                "Content-Security-Policy": CHAT_EXPORT_CSP,
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            },
        )

    @router.get(
        ROUTES["badmintonai_chat_export_pdf"],
        name="badmintonai_chat_export_pdf",
    )
    async def download_chat_pdf(
        chat_id: str,
        user: Any = Depends(current_user_dependency),
    ) -> Response:
        await require_chat_export_permission(user)
        document = await build_authorized_chat_html(chat_id, user, theme="light")
        return await render_pdf_response(document, f"badmintonai-chat-{chat_id}.pdf")

    return router


def _require_same_origin(request: Request) -> None:
    origin_value = request.headers.get("origin")
    host = request.headers.get("host")
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    expected_scheme = (
        forwarded_proto.split(",", 1)[0].strip().casefold()
        if forwarded_proto
        else request.url.scheme.casefold()
    )
    try:
        origin = urlsplit(origin_value or "")
        valid = (
            origin.scheme.casefold() in {"http", "https"}
            and origin.scheme.casefold() == expected_scheme
            and origin.netloc.casefold() == (host or "").casefold()
            and origin.path == ""
            and not origin.query
            and not origin.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(status_code=403, detail="拒絕跨來源請求")


async def _read_limited_body(request: Request, max_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared = int(content_length)
        except ValueError:
            raise WorkbenchError("Content-Length 格式無效", status_code=400) from None
        if declared < 0 or declared > max_bytes:
            raise WorkbenchError("請求內容超過允許大小", status_code=413)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise WorkbenchError("請求內容超過允許大小", status_code=413)
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_json_body(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="需要 application/json")
    try:
        data = await _read_limited_body(request, MAX_JSON_BYTES)
        payload = json.loads(data)
    except WorkbenchError as exc:
        _raise_http(exc)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="JSON 格式無效") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON 根節點必須是 object")
    return payload


async def _read_optional_run_id(request: Request) -> str | None:
    """相容舊版空 body，同時允許操作明確選取的 run。"""

    try:
        data = await _read_limited_body(request, MAX_JSON_BYTES)
    except WorkbenchError as exc:
        _raise_http(exc)
    if not data:
        return None
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="需要 application/json")
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="JSON 格式無效") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON 根節點必須是 object")
    run_id = payload.get("run_id")
    if run_id is not None and not isinstance(run_id, str):
        raise HTTPException(status_code=400, detail="run ID 格式無效")
    return run_id


def _request_upload_filename(request: Request) -> str:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type not in {"text/plain", "application/octet-stream"}:
        raise HTTPException(status_code=415, detail="上傳檔案必須是純文字 .txt")
    encoded_name = request.headers.get("x-upload-filename", "")
    try:
        filename = unquote(encoded_name, encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="檔名格式無效") from None
    return _validate_upload_name(filename)


def _request_selected_question_ids(request: Request) -> list[Any] | None:
    """讀取上傳開始 API 的可選 JSON 題號 header。"""

    raw = request.headers.get("x-selected-question-ids")
    if raw is None:
        return None
    if len(raw.encode("utf-8")) > MAX_SELECTION_HEADER_BYTES:
        raise HTTPException(status_code=413, detail="題號選取資料超過允許大小")
    try:
        selected_question_ids = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="題號選取格式無效") from None
    if not isinstance(selected_question_ids, list):
        raise HTTPException(status_code=400, detail="題號選取格式無效")
    return selected_question_ids


def _validate_upload_name(filename: str) -> str:
    if (
        not isinstance(filename, str)
        or not filename
        or len(filename) > 120
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or Path(filename).name != filename
        or Path(filename).suffix.casefold() != ".txt"
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
    ):
        raise WorkbenchError("檔名僅能是單一 .txt 檔名", status_code=400)
    return filename


def _raise_http(exc: WorkbenchError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None


def _admin_actor(user: Any) -> str:
    """只保存可追溯識別，不把完整的 Open WebUI user 物件寫入註記。"""

    for key in ("username", "id", "user_id", "sub"):
        value = user.get(key) if isinstance(user, dict) else getattr(user, key, None)
        if isinstance(value, str) and value.strip():
            return re.sub(r"[\r\n\t\x00-\x1f]", " ", value.strip())[:160]
    role = user.get("role") if isinstance(user, dict) else getattr(user, "role", None)
    if isinstance(role, str) and role.strip():
        return f"{role.strip()} (識別未提供)"[:160]
    raise WorkbenchError("無法確認管理員識別資料", status_code=403)


def _is_terminal(state: dict[str, Any]) -> bool:
    questions = state.get("questions")
    return isinstance(questions, list) and all(
        isinstance(item, dict)
        and _question_effective_status(item, state)
        in {"completed", "failed", "needs_review"}
        for item in questions
    )


def _run_status(state: dict[str, Any], *, worker_running: bool, paused: bool) -> str:
    statuses = [
        _question_effective_status(item, state)
        for item in state.get("questions", [])
        if isinstance(item, dict)
    ]
    if paused and worker_running:
        return "stopping"
    if worker_running:
        return "running"
    if statuses and all(
        status in {"completed", "failed", "needs_review"} for status in statuses
    ):
        if "needs_review" in statuses:
            return "needs_review"
        return (
            "completed"
            if all(status == "completed" for status in statuses)
            else "finished_with_errors"
        )
    if paused:
        return "stopped"
    if "awaiting_clarification" in statuses:
        return "awaiting_clarification"
    if "needs_review" in statuses:
        return "needs_review"
    return "paused"


def _question_effective_status(question: dict[str, Any], state: dict[str, Any]) -> Any:
    """舊版有效事件只在讀取時投影；不修改 checkpoint。"""

    if (
        _legacy_event_conflict_clarification(question) is not None
        and state.get("clarification_policy_version") == "requestClarification-event-v1"
    ):
        return "awaiting_clarification"
    return question.get("status")


_UI_QUESTION_STATUSES = (
    "queued",
    "running",
    "awaiting_clarification",
    "completed",
    "failed",
    "needs_review",
)


def _ui_question_status(
    question: dict[str, Any],
    *,
    review_status: Any = None,
    queued_work: dict[str, Any] | None = None,
) -> str:
    if isinstance(queued_work, dict):
        return "running" if queued_work.get("status") == "executing" else "queued"
    status = question.get("status")
    if review_status == "needs_review" or status == "needs_review":
        return "needs_review"
    if status in {
        "pending",
        "running",
        "awaiting_clarification",
        "completed",
        "failed",
    }:
        return "queued" if status == "pending" else status
    return "needs_review"


def _state_overview(
    state: dict[str, Any],
    *,
    work_queue: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    queued_by_question = {
        str(item.get("question_id")): item
        for item in work_queue or []
        if isinstance(item, dict)
    }
    counts = {status: 0 for status in _UI_QUESTION_STATUSES}
    for item in state["questions"]:
        effective = _question_effective_status(item, state)
        _failure, review_status = _failure_message_for_ui({**item, "status": effective})
        status = _ui_question_status(
            {**item, "status": effective},
            review_status=review_status,
            queued_work=queued_by_question.get(str(item.get("id"))),
        )
        counts[status] += 1
    manifest = state.get("manifest", {})
    question_source = manifest.get("question_source", {})
    model = manifest.get("model_snapshot", {})
    data = manifest.get("data_snapshot", {})
    usage_summary = summarize_questions(state["questions"])
    return {
        "run_id": state.get("run_id"),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "question_source": question_source,
        "model_snapshot": model,
        "data_snapshot": data,
        "question_count": len(state["questions"]),
        "status_counts": counts,
        "manual_retry_succeeded_count": sum(
            manual_retry_succeeded(q) for q in state["questions"]
        ),
        "token_totals": usage_summary["token_totals"],
        "token_observed_totals": usage_summary["observed_totals"],
        "token_coverage": usage_summary["fields"],
        "usage_attempt_count": usage_summary["attempt_count"],
        "token_summary": format_usage_summary(usage_summary),
    }


def _state_for_ui(
    state: dict[str, Any],
    *,
    annotations: list[dict[str, Any]] | None = None,
    clarification_queue: list[dict[str, Any]] | None = None,
    work_queue: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    active_queue = work_queue if work_queue is not None else clarification_queue or []
    overview = _state_overview(state, work_queue=active_queue)
    latest_annotations = {
        str(item.get("question_id")): item
        for item in annotations or []
        if isinstance(item, dict) and isinstance(item.get("question_id"), str)
    }
    queued_by_question = {
        str(item.get("question_id")): item
        for item in active_queue
        if isinstance(item, dict) and item.get("status") in {"queued", "executing"}
    }
    questions = []
    for item in state["questions"]:
        assistant = _latest_assistant(item)
        legacy_projection = (
            _legacy_event_conflict_clarification(item)
            if state.get("clarification_policy_version")
            == "requestClarification-event-v1"
            else None
        )
        effective_status = (
            "awaiting_clarification"
            if legacy_projection is not None
            else item.get("status")
        )
        display_question = (
            {**item, "status": effective_status}
            if legacy_projection is not None
            else item
        )
        review_source = _clarification_review_source(display_question, state)
        failure_message, review_status = _failure_message_for_ui(display_question)
        queued_answer = queued_by_question.get(str(item.get("id")))
        display_status = _ui_question_status(
            display_question,
            review_status=review_status,
            queued_work=queued_answer,
        )
        clarification = (
            legacy_projection["clarification"]
            if legacy_projection is not None
            else _structured_clarification(assistant)
            if assistant and effective_status == "awaiting_clarification"
            else None
        )
        turns = item.get("turns", [])
        work_types: list[str] = []
        for index, turn in enumerate(turns if isinstance(turns, list) else [], start=1):
            if not isinstance(turn, dict):
                continue
            if turn.get("manual_retry") is True or turn.get("kind") == "retry":
                label = "retry"
            elif turn.get("kind") == "clarification":
                label = "補答"
            elif turn.get("kind") == "question" or index == 1:
                label = "原題"
            else:
                label = "補答"
            if label not in work_types:
                work_types.append(label)
        active_work_type = (
            EvaluationWorkbenchService._queue_work_type(queued_answer)
            if isinstance(queued_answer, dict)
            else None
        )
        if active_work_type == "question" and "原題" not in work_types:
            work_types.append("原題")
        elif active_work_type == "clarification" and "補答" not in work_types:
            work_types.append("補答")
        elif active_work_type == "retry" and "retry" not in work_types:
            work_types.append("retry")
        usage_summary = summarize_question(item)
        tool_calls = sum(
            len(turn.get("result", {}).get("tool_calls", []))
            for turn in turns
            if isinstance(turn, dict) and isinstance(turn.get("result"), dict)
        )
        charts = sum(
            len(turn.get("result", {}).get("charts", []))
            for turn in turns
            if isinstance(turn, dict) and isinstance(turn.get("result"), dict)
        )
        questions.append(
            {
                "id": item.get("id"),
                "prompt": item.get("prompt"),
                "status": effective_status,
                "checkpoint_status": (
                    item.get("status") if legacy_projection is not None else None
                ),
                "clarification_projection": (
                    "legacy_event_conflict" if legacy_projection is not None else None
                ),
                "clarification_projection_note": (
                    "以已保存的 completed requestClarification 事件唯讀相容投影；"
                    "原 checkpoint 維持 needs_review，直到補答開始執行。"
                    if legacy_projection is not None
                    else None
                ),
                "display_status": display_status,
                "queued_work": (
                    {
                        "status": queued_answer.get("status"),
                        "work_type": active_work_type,
                        "answer": queued_answer.get("answer")
                        if active_work_type == "clarification"
                        else None,
                        "queue_id": queued_answer.get("queue_id"),
                    }
                    if isinstance(queued_answer, dict)
                    else None
                ),
                "queued_clarification": (
                    {
                        "status": queued_answer.get("status"),
                        "answer": queued_answer.get("answer"),
                        "queue_id": queued_answer.get("queue_id"),
                        "work_type": "clarification",
                    }
                    if isinstance(queued_answer, dict)
                    and active_work_type == "clarification"
                    else None
                ),
                "work_types": work_types,
                "work_type": active_work_type,
                "resume_required": (
                    item.get("status") in {"pending", "running"}
                    and queued_answer is None
                ),
                "failure_message": failure_message,
                "clarification_review_candidate": review_source is not None,
                "clarification_review_source": review_source,
                "clarification_review_reason": item.get("clarification_review_reason"),
                "classification_annotation": latest_annotations.get(
                    str(item.get("id"))
                ),
                "conversation_id": _safe_conversation_id(item.get("conversation_id")),
                "retry_count": item.get("retry_count"),
                "manual_retry_count": item.get("manual_retry_count", 0),
                "manual_retry_succeeded": manual_retry_succeeded(item),
                "can_retry": (
                    item.get("status") == "failed"
                    and item.get("pending_turn") is None
                    and not (
                        isinstance(queued_answer, dict) and active_work_type == "retry"
                    )
                ),
                "elapsed_ms": item.get("elapsed_ms"),
                "processing_elapsed_ms": _processing_elapsed_ms(item),
                "user_wait_ms": item.get("user_wait_ms"),
                "usage": item.get("usage_totals"),
                "usage_summary": usage_summary,
                "tool_call_count": tool_calls,
                "chart_count": charts,
                "assistant_text": (
                    clarification["question"][:MAX_UI_MESSAGE_CHARS]
                    if clarification is not None
                    and (failure_message is None or item.get("status") == "failed")
                    else _message_text(assistant.get("content"))[:MAX_UI_MESSAGE_CHARS]
                    if assistant
                    and (failure_message is None or item.get("status") == "failed")
                    else ""
                ),
                "choices": (
                    clarification["options"]
                    if clarification
                    else _message_choices(assistant)
                    if assistant
                    else []
                )
                if failure_message is None
                else [],
                "error_count": len(item.get("errors", [])),
            }
        )
    overview["questions"] = questions
    overview["status_counts"] = {
        status: sum(question["display_status"] == status for question in questions)
        for status in _UI_QUESTION_STATUSES
    }
    overview["queued_clarification_count"] = sum(
        1
        for item in active_queue
        if isinstance(item, dict)
        and EvaluationWorkbenchService._queue_work_type(item) == "clarification"
        and item.get("status") == "queued"
    )
    overview["queued_retry_count"] = sum(
        1
        for item in active_queue
        if isinstance(item, dict)
        and EvaluationWorkbenchService._queue_work_type(item) == "retry"
        and item.get("status") == "queued"
    )
    overview["queued_work_count"] = sum(
        1
        for item in active_queue
        if isinstance(item, dict) and item.get("status") == "queued"
    )
    overview["classification_annotations"] = annotations or []
    return overview


def _failure_message_for_ui(question: dict[str, Any]) -> tuple[str | None, Any]:
    """以固定文字提示失敗；保留 checkpoint status 與原始錯誤內容。"""

    status = question.get("status")
    result = _latest_result_for_ui(question)
    kind = _saved_result_failure_kind(result)

    if status == "completed" and kind is not None:
        details = {
            "rate_limited": "服務回應了限流訊號",
            "python": "Python 分析工具未成功完成",
            "chart": "圖表附加狀態未確認",
            "chart_failed": "圖表產生或附加失敗",
            "unfinished_tool": "工具呼叫狀態未完成",
            "blank": "沒有保存可驗證的回答",
            "unknown": "保存結果包含錯誤或異常終態",
        }
        return (
            "原 run 標記為完成，但保存紀錄顯示"
            f"{details.get(kind, details['unknown'])}；請開啟原對話人工複核。"
            "工作台不會修改原 run 狀態。",
            "needs_review",
        )

    if status == "failed":
        if kind is None:
            for error in reversed(question.get("errors", [])):
                kind = _safe_failure_kind(error)
                if kind is not None:
                    break
        return _failure_text(kind or "unknown", status), status

    if status == "needs_review" and kind is not None:
        return _failure_text(kind, status), status

    if status == "running":
        pending = question.get("pending_turn")
        attempts = pending.get("attempts") if isinstance(pending, dict) else None
        if isinstance(attempts, list) and attempts:
            latest = attempts[-1]
            errors = (
                [
                    latest.get("recovery_error"),
                    latest.get("error"),
                ]
                if isinstance(latest, dict)
                else []
            )
            kinds = [_safe_failure_kind(error) for error in errors]
            if "rate_limited" in kinds:
                return _failure_text("rate_limited", status), status
            if any(kind is not None for kind in kinds):
                return _failure_text("unknown", status), status

    return None, status


def _latest_result_for_ui(question: dict[str, Any]) -> dict[str, Any] | None:
    for turn in reversed(question.get("turns", [])):
        if not isinstance(turn, dict):
            continue
        result = turn.get("result")
        if isinstance(result, dict):
            return result
        attempts = turn.get("attempts")
        if isinstance(attempts, list):
            for attempt in reversed(attempts):
                result = attempt.get("result") if isinstance(attempt, dict) else None
                if isinstance(result, dict):
                    return result
    pending = question.get("pending_turn")
    attempts = pending.get("attempts") if isinstance(pending, dict) else None
    if isinstance(attempts, list):
        for attempt in reversed(attempts):
            result = attempt.get("result") if isinstance(attempt, dict) else None
            if isinstance(result, dict):
                return result
    return None


def _saved_result_failure_kind(result: dict[str, Any] | None) -> str | None:
    if not isinstance(result, dict):
        return None
    if result.get("error"):
        return _safe_failure_kind(result["error"]) or "unknown"

    messages = result.get("messages")
    assistant = (
        next(
            (
                message
                for message in reversed(messages)
                if isinstance(message, dict) and message.get("role") == "assistant"
            ),
            None,
        )
        if isinstance(messages, list)
        else None
    )
    if isinstance(assistant, dict) and assistant.get("error"):
        return _safe_failure_kind(assistant["error"]) or "unknown"

    tool_calls = result.get("tool_calls")
    tool_calls = tool_calls if isinstance(tool_calls, list) else []
    analysis_error = _analysis_completion_error(
        tool_calls,
        assistant.get("output") if isinstance(assistant, dict) else None,
        result.get("charts") if isinstance(result.get("charts"), list) else [],
    )
    if analysis_error is not None:
        return _safe_failure_kind(analysis_error) or "unknown"
    if any(
        isinstance(call, dict)
        and isinstance(call.get("status"), str)
        and call.get("status") not in {"completed", "failed", "error"}
        for call in tool_calls
    ):
        return "unfinished_tool"

    analyses = [
        call
        for call in tool_calls
        if isinstance(call, dict) and call.get("name") == "runPythonAnalysis"
    ]
    if analyses and all(call.get("status") in {"failed", "error"} for call in analyses):
        return "python"

    if assistant is None:
        return None
    if result.get("charts"):
        return None
    if _structured_clarification(assistant):
        return None
    output = assistant.get("output")
    has_text = bool(_message_text(assistant.get("content")).strip())
    if isinstance(output, list):
        has_text = has_text or any(
            isinstance(item, dict)
            and item.get("type") == "message"
            and bool(_message_text(item.get("content")).strip())
            for item in output
        )
    if not has_text:
        return "blank"
    return None


def _safe_failure_kind(error: Any) -> str | None:
    """只用錯誤的型別/有限訊號分類，不把遠端文字回傳給 UI。"""

    if isinstance(error, dict):
        if error.get("code") in {
            "analysis_message_limit",
            "analysis_retry_limit",
            "analysis_terminated",
        }:
            return "analysis_limit"
        if error.get("code") == "analysis_incomplete":
            return "analysis_incomplete"
        if error.get("code") == "render_failed":
            return "chart_failed"
        if _is_rate_limited(error):
            return "rate_limited"
        message = error.get("message") or error.get("content") or error.get("detail")
    else:
        message = error
    if isinstance(message, str):
        if re.search(r"(?i)\b429\b|rate[\s_-]*limit|too many requests", message):
            return "rate_limited"
        if message.startswith("Python 分析工具未成功完成"):
            return "python"
        if message.startswith("互動圖表"):
            return "chart"
        if "仍有未完成的工具呼叫" in message:
            return "unfinished_tool"
        if "沒有可見答案" in message:
            return "blank"
        if "沒有可驗證的回答" in message:
            return "blank"
        if message:
            return "unknown"
    return "unknown" if error else None


def _is_rate_limited(value: dict[str, Any]) -> bool:
    for key in ("status", "status_code", "http_status"):
        if value.get(key) == 429:
            return True
    code = value.get("code") or value.get("type")
    if isinstance(code, str) and code.casefold() in {
        "rate_limit",
        "rate_limit_error",
        "rate_limit_exceeded",
        "too_many_requests",
    }:
        return True
    return any(
        isinstance(value.get(key), str)
        and re.search(
            r"(?i)\b429\b|rate[\s_-]*limit|too many requests",
            value[key],
        )
        for key in ("message", "content", "detail")
    )


def _failure_text(kind: str, status: Any) -> str:
    if status == "running":
        if kind == "rate_limited":
            return (
                "服務暫時受限；本輪狀態尚未確認，原回合已保留且未自動重送。"
                "請先開啟原對話確認，再決定下一步。"
            )
        return (
            "系統尚未能確認本輪是否完成；原回合已保留且未自動重送。"
            "請先開啟原對話確認，再決定是否續跑。"
        )
    messages = {
        "analysis_limit": "分析執行次數已達上限或分析已停止；已保存結果與已發布圖表保留，但未完成部分不視為結論。",
        "analysis_incomplete": "本輪未能產生可驗證的完整分析答覆；已保存結果與已發布圖表保留，未完成部分不視為結論。",
        "rate_limited": "服務暫時受限；本輪已停止自動重送。請開啟原對話確認狀態，再人工決定下一步。",
        "python": "Python 分析工具未成功完成；自動重試已停止。請開啟原對話檢查工具結果，再決定下一步。",
        "chart": "圖表附加狀態未確認；系統未自動重送。請開啟原對話檢查圖表，再決定下一步。",
        "chart_failed": "圖表產生或附加失敗；請查看原對話中的既有結果。",
        "unfinished_tool": "本輪有未完成的工具呼叫；系統未自動重送。請開啟原對話確認工具狀態，再決定下一步。",
        "blank": "本輪沒有可驗證的回答；系統未自動重送。請開啟原對話確認回合狀態，再決定下一步。",
        "unknown": "本輪未能確認成功；自動重試已停止。請開啟原對話查看結果，再人工決定下一步。",
    }
    return messages.get(kind, messages["unknown"])


def _clarification_review_source(
    question: dict[str, Any], state: dict[str, Any]
) -> str | None:
    """產生人工複核提示；只回報線索，不改動 checkpoint 狀態。"""

    if (
        state.get("clarification_policy_version") == "requestClarification-event-v1"
        and question.get("status") == "awaiting_clarification"
    ):
        return None
    for turn in reversed(question.get("turns", [])):
        if not isinstance(turn, dict):
            continue
        results = [turn.get("result")]
        attempts = turn.get("attempts")
        if isinstance(attempts, list):
            results.extend(
                attempt.get("result")
                for attempt in reversed(attempts)
                if isinstance(attempt, dict)
            )
        for result in results:
            if not isinstance(result, dict):
                continue
            signal = result.get("clarification_signal")
            if signal in {"text_candidate", "event_conflict", "tool_failed"}:
                return signal
            if signal == "event" and question.get("status") == "awaiting_clarification":
                continue
            calls = result.get("tool_calls")
            if isinstance(calls, list) and any(
                isinstance(call, dict)
                and call.get("name") == "requestClarification"
                and (
                    call.get("status") != "completed"
                    or question.get("status") != "awaiting_clarification"
                )
                for call in calls
            ):
                return "requestClarification_event"
            messages = result.get("messages")
            if isinstance(messages, list):
                for message in reversed(messages):
                    if (
                        isinstance(message, dict)
                        and message.get("role") == "assistant"
                        and _structured_clarification(message)
                        and question.get("status") != "awaiting_clarification"
                    ):
                        return "requestClarification_event"
    if (
        state.get("clarification_policy_version") == "requestClarification-event-v1"
        and question.get("status") == "needs_review"
    ):
        return "missing_or_failed_event"
    if question.get("status") in {
        "completed",
        "failed",
        "awaiting_clarification",
    }:
        assistant = _latest_assistant(question)
        if assistant and clarification_text_candidate(assistant):
            return "legacy_text_hint"
    return None


def _legacy_event_conflict_clarification(
    question: dict[str, Any],
) -> dict[str, Any] | None:
    """只投影有完整舊事件證據且可綁定原回合的澄清，不讀取回答文字。"""

    if question.get("status") != "needs_review" or isinstance(
        question.get("pending_turn"), dict
    ):
        return None
    turns = question.get("turns")
    if not isinstance(turns, list) or not turns or not isinstance(turns[-1], dict):
        return None
    source_turn = turns[-1]
    if not isinstance(source_turn.get("operation_id"), str) or not source_turn.get(
        "operation_id"
    ):
        return None
    if _safe_conversation_id(question.get("conversation_id")) is None:
        return None
    result = source_turn.get("result")
    if (
        not isinstance(result, dict)
        or result.get("clarification_signal") != "event_conflict"
    ):
        return None
    tool_calls = result.get("tool_calls")
    clarification = (
        _structured_clarification({"tool_calls": tool_calls})
        if isinstance(tool_calls, list)
        else None
    )
    if clarification is None:
        messages = result.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                if isinstance(message, dict) and message.get("role") == "assistant":
                    clarification = _structured_clarification(message)
                    if clarification is not None:
                        break
    if clarification is None:
        return None
    return {
        "clarification": clarification,
        "source_operation_id": source_turn["operation_id"],
        "conversation_id": question["conversation_id"],
    }


def _safe_conversation_id(value: Any) -> str | None:
    """只將單一路徑安全字元的 Open WebUI chat ID 暴露給頁面連結。"""

    if isinstance(value, str) and CONVERSATION_ID_PATTERN.fullmatch(value):
        return value
    return None


def _processing_elapsed_ms(question: dict[str, Any]) -> int | None:
    """回傳不含人工等待的處理時間，不修改原始 checkpoint。"""

    measured = _nonnegative_int(question.get("processing_elapsed_ms"))
    if measured is not None:
        return measured
    elapsed = _nonnegative_int(question.get("elapsed_ms"))
    user_wait = _nonnegative_int(question.get("user_wait_ms"))
    if elapsed is None or user_wait is None or user_wait > elapsed:
        return None
    return elapsed - user_wait


def _latest_assistant(question: dict[str, Any]) -> dict[str, Any] | None:
    for turn in reversed(question.get("turns", [])):
        if not isinstance(turn, dict):
            continue
        candidates = [turn.get("result")]
        attempts = turn.get("attempts")
        if isinstance(attempts, list):
            candidates.extend(
                attempt.get("result")
                for attempt in reversed(attempts)
                if isinstance(attempt, dict)
            )
        for result in candidates:
            messages = result.get("messages") if isinstance(result, dict) else None
            if isinstance(messages, list):
                for message in reversed(messages):
                    if isinstance(message, dict) and message.get("role") == "assistant":
                        return message
    pending = question.get("pending_turn")
    attempts = pending.get("attempts") if isinstance(pending, dict) else None
    if isinstance(attempts, list):
        for attempt in reversed(attempts):
            result = attempt.get("result") if isinstance(attempt, dict) else None
            messages = result.get("messages") if isinstance(result, dict) else None
            if isinstance(messages, list):
                for message in reversed(messages):
                    if isinstance(message, dict) and message.get("role") == "assistant":
                        return message
    return None


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item["text"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


def _message_choices(message: dict[str, Any]) -> list[str]:
    candidates = [
        message.get("choices"),
        message.get("options"),
        message.get("suggestions"),
    ]
    metadata = message.get("metadata")
    if isinstance(metadata, dict):
        candidates.extend(
            (
                metadata.get("choices"),
                metadata.get("options"),
                metadata.get("suggestions"),
            )
        )
    for candidate in candidates:
        if isinstance(candidate, list):
            values = [
                item.strip()
                for item in candidate
                if isinstance(item, str) and item.strip() and len(item) <= 300
            ][:12]
            if values:
                return values
    return []


def _structured_clarification(message: dict[str, Any]) -> dict[str, Any] | None:
    """只讀取已完成的澄清工具呼叫，不解析任意回答文字。"""

    for key in ("tool_calls", "output"):
        calls = message.get(key)
        if not isinstance(calls, list):
            continue
        for call in reversed(calls):
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
            if not isinstance(arguments, dict):
                continue
            question = arguments.get("question")
            if not isinstance(question, str) or not question.strip():
                continue
            options = arguments.get("options")
            return {
                "question": question.strip(),
                "options": [
                    option.strip()
                    for option in options
                    if isinstance(option, str) and option.strip() and len(option) <= 300
                ][:3]
                if isinstance(options, list)
                else [],
            }
    return None


def _token_totals(questions: list[dict[str, Any]]) -> dict[str, int | None]:
    return summarize_questions(questions)["token_totals"]


def _render_summary(state: dict[str, Any]) -> str:
    overview = _state_overview(state)
    processing_values = [
        value
        for question in state["questions"]
        if isinstance(question, dict)
        for value in [_processing_elapsed_ms(question)]
        if value is not None
    ]
    wait_values = [
        value
        for question in state["questions"]
        if isinstance(question, dict)
        for value in [_nonnegative_int(question.get("user_wait_ms"))]
        if value is not None
    ]

    def duration_total(values: list[int]) -> str:
        if not values:
            return "未提供"
        return (
            f"{_format_duration(sum(values))}"
            f"（{len(values)}/{len(state['questions'])} 題有值）"
        )

    lines = [
        "BadmintonAI 評測結果",
        "=" * 32,
        f"Run ID：{state.get('run_id')}",
        f"建立時間：{state.get('created_at') or '未提供'}",
        f"最後更新：{state.get('updated_at') or '未提供'}",
        f"題目來源：{overview['question_source'].get('name', '未提供')}",
        f"題目數：{overview['question_count']}",
        f"模型快照：{json.dumps(overview['model_snapshot'], ensure_ascii=False, sort_keys=True)}",
        f"資料快照：{json.dumps(overview['data_snapshot'], ensure_ascii=False, sort_keys=True)}",
        "狀態統計：",
        f"重試後成功：{sum(manual_retry_succeeded(q) for q in state['questions'])} 題",
    ]
    lines.extend(
        f"  {status}：{count}"
        for status, count in sorted(overview["status_counts"].items())
    )
    lines.append("總處理耗時（不含人工等待補答）：" + duration_total(processing_values))
    lines.append("總人工等待澄清：" + duration_total(wait_values))
    lines.append("模型實際 Token 用量（Open WebUI 未回傳時不估算）：")
    lines.append("  " + overview["token_summary"])
    for question in state["questions"]:
        lines.extend(
            [
                "",
                f"Q{question.get('id')} [{question.get('status')}]",
                f"人工重試：{question.get('manual_retry_count', 0)} 次；重試後成功：{'是' if manual_retry_succeeded(question) else '否'}",
                f"題目：{question.get('prompt', '')}",
                f"端到端耗時：{_format_duration(question.get('elapsed_ms'))}",
                f"處理耗時（不含人工等待）：{_format_duration(_processing_elapsed_ms(question))}",
                f"使用者等待澄清：{_format_duration(question.get('user_wait_ms'))}",
                f"重試次數：{question.get('retry_count', 0)}",
                "Token：" + format_usage_summary(summarize_question(question)),
            ]
        )
        for index, turn in enumerate(question.get("turns", []), start=1):
            if not isinstance(turn, dict):
                continue
            turn_kind = turn.get("kind")
            if turn_kind == "clarification":
                request_label = "補答"
            elif turn_kind == "retry":
                request_label = "重試回合"
            elif turn_kind == "question" or index == 1:
                request_label = "原始提問"
            else:
                request_label = "補答"
            request_text = turn.get("request")
            if isinstance(request_text, str):
                lines.extend([f"第 {index} 輪{request_label}：", request_text])
            result = turn.get("result")
            if not isinstance(result, dict):
                continue
            for message in result.get("messages", []):
                if isinstance(message, dict) and message.get("role") == "assistant":
                    lines.append(f"第 {index} 輪回答：")
                    lines.append(
                        _message_text(message.get("content")) or "（無文字內容）"
                    )
            lines.append(
                f"工具呼叫：{len(result.get('tool_calls', []))}；圖表 metadata：{len(result.get('charts', []))}"
            )
        for error in question.get("errors", []):
            if isinstance(error, dict):
                lines.append(
                    f"錯誤：{error.get('stage', '未知階段')} / {error.get('type', '未知')} / {error.get('message', '')}"
                )
    annotations = state.get("classification_annotations")
    if isinstance(annotations, list) and annotations:
        lines.extend(["", "管理員分類註記（外掛標註；不改寫 checkpoint）："])
        labels = {
            "missed_clarification": "標註為漏判追問",
            "answered_with_followup": "標註為已作答並附建議問句",
            "needs_review": "標註為仍需人工複核",
        }
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            lines.extend(
                [
                    f"Q{annotation.get('question_id')}：{labels.get(annotation.get('classification'), '人工分類')}；"
                    f"原狀態={annotation.get('original_status', '未提供')}；"
                    f"管理員={annotation.get('actor', '未提供')}；"
                    f"時間={annotation.get('recorded_at', '未提供')}",
                    f"理由：{annotation.get('reason', '')}",
                ]
            )
    return "\n".join(lines) + "\n"


def _format_duration(value: Any) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return "未提供"
    seconds, milliseconds = divmod(value, 1000)
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _safe_basename(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = value.replace("\\", "/").rsplit("/", 1)[-1]
    return candidate[:160] or None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utc_now_milliseconds() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _chmod_private(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _redact_secret_values(value: Any, secret: str) -> Any:
    if isinstance(value, dict):
        return {key: _redact_secret_values(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secret_values(item, secret) for item in value]
    if isinstance(value, str):
        if secret:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(
            r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]+=*",
            r"\1[REDACTED]",
            value,
        )
        return re.sub(
            r"(?i)((?:api[_-]?key|authorization|access[_-]?token)\s*[:=]\s*)[^\s,;]+",
            r"\1[REDACTED]",
            value,
        )
    return value


__all__ = [
    "DEFAULT_SOURCE_PATH",
    "ROUTE_NAMES",
    "ROUTE_PATHS",
    "SOURCE_FILENAME",
    "SOURCE_ID",
    "EvaluationWorkbenchService",
    "WorkbenchError",
    "create_api_router",
]
