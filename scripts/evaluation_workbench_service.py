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
from datetime import datetime, timezone
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
    parse_numbered_questions,
    select_question_ids,
)

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
}
ROUTE_NAMES = frozenset(ROUTES)
ROUTE_PATHS = frozenset(ROUTES.values())
CONVERSATION_IFRAME_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; connect-src 'none'; worker-src blob:; "
    "base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'self'"
)

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
        self._lease_path = self.storage_dir / "worker.lock"
        self._meta_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._lease: _ExecutionLease | None = None
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
        return OpenWebUIEvaluationClient(
            base_url=DEFAULT_OPEN_WEBUI_URL,
            api_key=key,
            model_id=os.environ.get(
                "BADMINTON_AI_EVALUATION_MODEL_ID", DEFAULT_MODEL_ID
            ),
            timeout_seconds=20,
            poll_interval_seconds=0.5,
            max_wait_seconds=120,
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

    def _write_pointer(self, run_id: str, *, paused: bool) -> None:
        payload = (
            json.dumps({"run_id": run_id, "paused": paused}, ensure_ascii=False) + "\n"
        )
        self._atomic_write(self._pointer_path, payload.encode("utf-8"))

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
        projected = _state_for_ui(state, annotations=annotations["annotations"])
        projected = _redact_secret_values(projected, self._api_key_provider())
        return {
            "run": projected,
            "run_id": run_id,
            "run_status": _run_status(state, worker_running=False, paused=False),
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
        lease = self._try_lease()
        if lease is None:
            raise WorkbenchError("另一個評測工作正在執行", status_code=409)
        staging_path: Path | None = None
        try:
            current = self._current_state()
            if current and not _is_terminal(current[1]):
                raise WorkbenchError(
                    "目前 run 尚未完成；請續跑或處理待澄清題目", status_code=409
                )
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
            )
            state = runner.snapshot()
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
            self._write_pointer(run_id, paused=False)
            self._runner = runner
            self._current_run_id = run_id
            self._last_worker_error = None
            self._stop_event.clear()
            self._launch_worker(lease, runner, action="run")
            lease = None
            return self.status()
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

    def stop(self) -> dict[str, Any]:
        current = self._current_state()
        if current is None:
            raise WorkbenchError("目前沒有可停止的 run", status_code=404)
        run_id, state, _paused = current
        if _is_terminal(state):
            raise WorkbenchError("目前 run 已完成", status_code=409)
        self._stop_event.set()
        self._write_pointer(run_id, paused=True)
        return self.status()

    def resume(self) -> dict[str, Any]:
        current = self._current_state()
        if current is None:
            raise WorkbenchError("目前沒有可續跑的 run", status_code=404)
        run_id, state, _paused = current
        if _is_terminal(state):
            raise WorkbenchError("目前 run 已完成", status_code=409)
        waiting = any(
            item.get("status") == "awaiting_clarification"
            for item in state["questions"]
        )
        runnable = any(
            item.get("status") in {"pending", "running"} for item in state["questions"]
        )
        if not runnable:
            message = "請先補答待澄清題目" if waiting else "目前沒有可續跑題目"
            raise WorkbenchError(message, status_code=409)
        lease = self._try_lease()
        if lease is None:
            raise WorkbenchError("另一個評測工作正在執行", status_code=409)
        try:
            runner = self._load_runner(run_id)
            self._write_pointer(run_id, paused=False)
            self._last_worker_error = None
            self._stop_event.clear()
            self._launch_worker(lease, runner, action="run")
            lease = None
        finally:
            self._release_lease(lease)
        return self.status()

    def clarify(self, question_id: str, answer: str) -> dict[str, Any]:
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
        current = self._current_state()
        if current is None:
            raise WorkbenchError("目前沒有可補答的 run", status_code=404)
        run_id, state, _paused = current
        question = next(
            (item for item in state["questions"] if item.get("id") == question_id),
            None,
        )
        if not isinstance(question, dict) or question.get("status") != (
            "awaiting_clarification"
        ):
            raise WorkbenchError("該題目前不等待澄清", status_code=409)
        lease = self._try_lease()
        if lease is None:
            raise WorkbenchError("另一個評測工作正在執行", status_code=409)
        try:
            runner = self._load_runner(run_id)
            self._write_pointer(run_id, paused=False)
            self._last_worker_error = None
            self._stop_event.clear()
            self._launch_worker(
                lease,
                runner,
                action="clarify",
                question_id=question_id,
                answer=answer,
            )
            lease = None
        finally:
            self._release_lease(lease)
        return self.status()

    def recover_after_restart(self) -> bool:
        """重啟後只復原曾由使用者啟動且未明確停止的 run。"""

        try:
            current = self._current_state()
        except WorkbenchError:
            return False
        if current is None:
            return False
        run_id, state, paused = current
        if paused or _is_terminal(state):
            return False
        if not any(
            item.get("status") in {"pending", "running"} for item in state["questions"]
        ):
            return False
        lease = self._try_lease()
        if lease is None:
            return False
        try:
            runner = self._load_runner(run_id)
            self._stop_event.clear()
            self._launch_worker(lease, runner, action="run")
            lease = None
            return True
        except WorkbenchError:
            return False
        finally:
            self._release_lease(lease)

    def shutdown(self) -> None:
        """停用或程序關閉時保留 checkpoint，阻止開始下一題。"""

        self._stop_event.set()

    def _stop_requested(self) -> bool:
        if self._stop_event.is_set():
            return True
        try:
            pointer = self._read_pointer()
        except WorkbenchError:
            return True
        return pointer is None or pointer["paused"]

    def status(self) -> dict[str, Any]:
        current = self._current_state()
        local_worker_running = self._worker_is_running()
        busy_elsewhere = False if local_worker_running else self._has_other_worker()
        worker_running = local_worker_running or busy_elsewhere
        if current is None:
            return {
                "run": None,
                "run_status": "idle",
                "worker_running": worker_running,
                "busy_elsewhere": busy_elsewhere,
                "can_start": not busy_elsewhere,
                "can_resume": False,
                "last_error": self._last_worker_error,
            }
        run_id, state, paused = current
        run_status = _run_status(state, worker_running=worker_running, paused=paused)
        annotations = self._read_annotations(run_id)
        return {
            "run": _redact_secret_values(
                _state_for_ui(state, annotations=annotations["annotations"]),
                self._api_key_provider(),
            ),
            "run_status": run_status,
            "worker_running": worker_running,
            "busy_elsewhere": busy_elsewhere,
            "can_start": _is_terminal(state) and not busy_elsewhere,
            "can_resume": (
                not worker_running
                and not busy_elsewhere
                and not _is_terminal(state)
                and any(
                    item.get("status") in {"pending", "running"}
                    for item in state["questions"]
                )
            ),
            "run_id": run_id,
            "last_error": self._last_worker_error,
        }

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        self._ensure_storage()
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
            overview = _state_overview(state)
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

    def _launch_worker(
        self,
        lease: _ExecutionLease,
        runner: EvaluationRunner,
        *,
        action: str,
        question_id: str | None = None,
        answer: str | None = None,
    ) -> None:
        with self._meta_lock:
            if self._worker is not None and self._worker.is_alive():
                raise WorkbenchError("另一個評測工作正在執行", status_code=409)
            self._lease = lease
            worker = threading.Thread(
                target=self._worker_main,
                args=(runner, action, question_id, answer, lease),
                daemon=True,
                name="badmintonai-evaluation-runner",
            )
            self._worker = worker
            worker.start()

    def _worker_main(
        self,
        runner: EvaluationRunner,
        action: str,
        question_id: str | None,
        answer: str | None,
        lease: _ExecutionLease,
    ) -> None:
        try:
            if action == "clarify":
                if question_id is None or answer is None:
                    raise WorkbenchError("補答狀態無效")
                runner.submit_clarification(question_id, answer)
            runner.run_pending(stop_requested=self._stop_requested)
            self._last_worker_error = None
        except Exception as exc:
            # 只保留例外類別，不記錄可能包含第三方回應或設定的文字。
            self._last_worker_error = type(exc).__name__
        finally:
            with self._meta_lock:
                if self._lease is lease:
                    self._lease = None
                self._worker = None
            self._release_lease(lease)


def create_api_router(
    workbench: EvaluationWorkbenchService,
    admin_dependency: Callable[..., Any],
) -> APIRouter:
    """建立所有 API route；每一條 read/write/download 都明確驗證 admin。"""

    router = APIRouter()

    @router.get(
        ROUTES["badmintonai_evaluation_status"], name="badmintonai_evaluation_status"
    )
    async def status(user: Any = Depends(admin_dependency)) -> dict[str, Any]:
        del user
        try:
            return await run_in_threadpool(workbench.status)
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
            return await run_in_threadpool(workbench.stop)
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
            return await run_in_threadpool(workbench.resume)
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
        if not isinstance(question_id, str) or not isinstance(answer, str):
            raise HTTPException(status_code=400, detail="補答內容格式無效")
        try:
            return await run_in_threadpool(workbench.clarify, question_id, answer)
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
        and item.get("status") in {"completed", "failed", "needs_review"}
        for item in questions
    )


def _run_status(state: dict[str, Any], *, worker_running: bool, paused: bool) -> str:
    if paused and worker_running:
        return "stopping"
    if worker_running:
        return "running"
    if _is_terminal(state):
        if any(item.get("status") == "needs_review" for item in state["questions"]):
            return "needs_review"
        return (
            "completed"
            if all(item.get("status") == "completed" for item in state["questions"])
            else "finished_with_errors"
        )
    if paused:
        return "stopped"
    if any(
        item.get("status") == "awaiting_clarification" for item in state["questions"]
    ):
        return "awaiting_clarification"
    if any(item.get("status") == "needs_review" for item in state["questions"]):
        return "needs_review"
    return "paused"


def _state_overview(state: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for item in state["questions"]:
        status = item.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    manifest = state.get("manifest", {})
    question_source = manifest.get("question_source", {})
    model = manifest.get("model_snapshot", {})
    data = manifest.get("data_snapshot", {})
    return {
        "run_id": state.get("run_id"),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "question_source": question_source,
        "model_snapshot": model,
        "data_snapshot": data,
        "question_count": len(state["questions"]),
        "status_counts": counts,
        "token_totals": _token_totals(state["questions"]),
    }


def _state_for_ui(
    state: dict[str, Any], *, annotations: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    overview = _state_overview(state)
    latest_annotations = {
        str(item.get("question_id")): item
        for item in annotations or []
        if isinstance(item, dict) and isinstance(item.get("question_id"), str)
    }
    questions = []
    for item in state["questions"]:
        assistant = _latest_assistant(item)
        review_source = _clarification_review_source(item, state)
        failure_message, display_status = _failure_message_for_ui(item)
        clarification = (
            _structured_clarification(assistant)
            if assistant and item.get("status") == "awaiting_clarification"
            else None
        )
        turns = item.get("turns", [])
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
                "status": item.get("status"),
                "display_status": display_status,
                "failure_message": failure_message,
                "clarification_review_candidate": review_source is not None,
                "clarification_review_source": review_source,
                "clarification_review_reason": item.get("clarification_review_reason"),
                "classification_annotation": latest_annotations.get(
                    str(item.get("id"))
                ),
                "conversation_id": _safe_conversation_id(item.get("conversation_id")),
                "retry_count": item.get("retry_count"),
                "elapsed_ms": item.get("elapsed_ms"),
                "processing_elapsed_ms": _processing_elapsed_ms(item),
                "user_wait_ms": item.get("user_wait_ms"),
                "usage": item.get("usage_totals"),
                "tool_call_count": tool_calls,
                "chart_count": charts,
                "assistant_text": (
                    (
                        clarification["question"]
                        if clarification
                        else _message_text(assistant.get("content"))
                    )[:MAX_UI_MESSAGE_CHARS]
                    if assistant and failure_message is None
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
        "rate_limited": "服務暫時受限；本輪已停止自動重送。請開啟原對話確認狀態，再人工決定下一步。",
        "python": "Python 分析工具未成功完成；自動重試已停止。請開啟原對話檢查工具結果，再決定下一步。",
        "chart": "圖表附加狀態未確認；系統未自動重送。請開啟原對話檢查圖表，再決定下一步。",
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
    keys = ("input_tokens", "output_tokens", "total_tokens")
    totals: dict[str, int | None] = {}
    for key in keys:
        values = []
        for item in questions:
            if not isinstance(item, dict):
                continue
            usage = item.get("usage_totals")
            values.append(usage.get(key) if isinstance(usage, dict) else None)
        totals[key] = (
            sum(values)
            if values
            and all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in values
            )
            else None
        )
    return totals


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
    ]
    lines.extend(
        f"  {status}：{count}"
        for status, count in sorted(overview["status_counts"].items())
    )
    lines.append("總處理耗時（不含人工等待補答）：" + duration_total(processing_values))
    lines.append("總人工等待澄清：" + duration_total(wait_values))
    lines.append("模型實際 Token 用量（Open WebUI 未回傳時不估算）：")
    lines.extend(
        f"  {key}：{value if value is not None else '未取得模型實際用量'}"
        for key, value in overview["token_totals"].items()
    )
    for question in state["questions"]:
        lines.extend(
            [
                "",
                f"Q{question.get('id')} [{question.get('status')}]",
                f"題目：{question.get('prompt', '')}",
                f"端到端耗時：{_format_duration(question.get('elapsed_ms'))}",
                f"處理耗時（不含人工等待）：{_format_duration(_processing_elapsed_ms(question))}",
                f"使用者等待澄清：{_format_duration(question.get('user_wait_ms'))}",
                f"重試次數：{question.get('retry_count', 0)}",
                "Token："
                + (
                    ", ".join(
                        f"{key}={value if value is not None else '未取得模型實際用量'}"
                        for key, value in (question.get("usage_totals") or {}).items()
                    )
                    or "未取得模型實際用量"
                ),
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
