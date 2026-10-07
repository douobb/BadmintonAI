"""以本機受控檔案保存可重用的分析產物與繪圖冪等狀態。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Any, Callable, Sequence

from ..data.errors import DataError

RESULT_TTL_SECONDS = 24 * 60 * 60
MAX_RESULT_CACHE_BYTES = 512 * 1024 * 1024
MAX_RESULT_CACHE_ENTRIES = 256
MAX_RESULT_MANIFEST_BYTES = 64 * 1024
MAX_RENDER_STATE_ENTRIES = 4096
_RESULT_ID_PATTERN = re.compile(r"^[a-f0-9]{48}$")
_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_FILE_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_STORABLE_TYPES = {
    ".json": ("json", "application/json"),
    ".csv": ("table", "text/csv"),
    ".jsonl": ("json", "application/jsonl"),
}
_RESERVED_OUTPUTS = {"chart_spec.json", "plotly_charts.json"}


class AnalysisResultStoreError(DataError):
    """分析產物保存或讀取失敗。"""


class AnalysisResultOutputError(AnalysisResultStoreError):
    """分析沒有產生可供後續繪圖重用的文字資料檔。"""


class AnalysisResultNotFound(AnalysisResultStoreError):
    """結果不存在或不屬於目前 Open WebUI 使用者／聊天室。"""


class AnalysisResultExpired(AnalysisResultNotFound):
    """已保存的分析結果超過保存期限。"""


class AnalysisResultStoreUnavailable(AnalysisResultStoreError):
    """結果暫存空間無法安全讀寫。"""


@dataclass(frozen=True, slots=True)
class AnalysisResultScope:
    """由 Open WebUI request headers 驗證的 user/chat 範圍。"""

    user_id: str
    chat_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.user_id, str)
            or _IDENTITY_PATTERN.fullmatch(self.user_id) is None
            or not isinstance(self.chat_id, str)
            or _IDENTITY_PATTERN.fullmatch(self.chat_id) is None
        ):
            raise ValueError("Open WebUI user/chat 識別資訊無效")


@dataclass(frozen=True, slots=True)
class StoredAnalysisFile:
    """已驗證、可交給 render sandbox 的單一保存檔案。"""

    relative_path: str
    kind: str
    extension: str
    mime_type: str
    size_bytes: int
    content: bytes


@dataclass(frozen=True, slots=True)
class StoredAnalysisResult:
    """綁定 caller 與資料 snapshot 的分析產物集合。"""

    result_id: str
    snapshot_id: str
    created_at: float
    files: tuple[StoredAnalysisFile, ...]


@dataclass(frozen=True, slots=True)
class RenderClaim:
    """同一 assistant message 繪圖請求的持久冪等狀態。"""

    status: str
    attempts: int
    chart_count: int | None = None
    result_id: str | None = None


class AnalysisResultStore:
    """保存 JSON/CSV 類產物；使用 opaque IDs、TTL、總容量上限及原子寫入。"""

    def __init__(
        self,
        root: str | Path,
        *,
        ttl_seconds: int = RESULT_TTL_SECONDS,
        max_cache_bytes: int = MAX_RESULT_CACHE_BYTES,
        max_results: int = MAX_RESULT_CACHE_ENTRIES,
        max_file_bytes: int = 10 * 1024 * 1024,
        max_result_bytes: int = 25 * 1024 * 1024,
        max_files: int = 16,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or ttl_seconds < 1
            or isinstance(max_cache_bytes, bool)
            or not isinstance(max_cache_bytes, int)
            or max_cache_bytes < 1
            or isinstance(max_results, bool)
            or not isinstance(max_results, int)
            or max_results < 1
            or isinstance(max_file_bytes, bool)
            or not isinstance(max_file_bytes, int)
            or max_file_bytes < 1
            or isinstance(max_result_bytes, bool)
            or not isinstance(max_result_bytes, int)
            or max_result_bytes < 1
            or isinstance(max_files, bool)
            or not isinstance(max_files, int)
            or max_files < 1
        ):
            raise ValueError("分析結果保存限制必須是正整數")
        self.ttl_seconds = ttl_seconds
        self.max_cache_bytes = max_cache_bytes
        self.max_results = max_results
        self.max_file_bytes = max_file_bytes
        self.max_result_bytes = max_result_bytes
        self.max_files = max_files
        self._clock = clock
        self._lock = Lock()
        self._instance_id = uuid.uuid4().hex
        try:
            requested_root = Path(root).expanduser()
            requested_root.mkdir(parents=True, exist_ok=True)
            if requested_root.is_symlink() or not requested_root.is_dir():
                raise AnalysisResultStoreUnavailable("分析結果保存目錄不是一般目錄")
            self.root = requested_root.resolve(strict=True)
            self.results_root = self.root / "results"
            self.renders_root = self.root / "renders"
            self.results_root.mkdir(exist_ok=True)
            self.renders_root.mkdir(exist_ok=True)
            if self.results_root.is_symlink() or self.renders_root.is_symlink():
                raise AnalysisResultStoreUnavailable("分析結果保存目錄不允許 symlink")
        except AnalysisResultStoreError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise AnalysisResultStoreUnavailable("分析結果保存目錄無法建立") from exc

    def save(
        self,
        *,
        scope: AnalysisResultScope,
        snapshot_id: str,
        artifacts: Sequence[Any],
    ) -> StoredAnalysisResult:
        """只保存符合大小限制的安全 JSON/CSV/JSONL 產物。"""

        if (
            not isinstance(snapshot_id, str)
            or not snapshot_id
            or len(snapshot_id) > 128
            or any(ord(character) < 32 for character in snapshot_id)
        ):
            raise AnalysisResultOutputError("資料 snapshot 識別資訊無效")
        files = self._decode_artifacts(artifacts)
        total_bytes = sum(item.size_bytes for item in files)
        if total_bytes > self.max_result_bytes:
            raise AnalysisResultOutputError("可保存分析產物總大小超過上限")

        result_id = secrets.token_hex(24)
        now = self._clock()
        manifest = {
            "version": 1,
            "result_id": result_id,
            "user_hash": _identity_hash(scope.user_id),
            "chat_hash": _identity_hash(scope.chat_id),
            "snapshot_id": snapshot_id,
            "created_at": now,
            "total_bytes": total_bytes,
            "files": [
                {
                    "relative_path": item.relative_path,
                    "kind": item.kind,
                    "extension": item.extension,
                    "mime_type": item.mime_type,
                    "size_bytes": item.size_bytes,
                    "sha256": hashlib.sha256(item.content).hexdigest(),
                }
                for item in files
            ],
        }
        temp_path = self.results_root / f".tmp-{result_id}"
        final_path = self.results_root / result_id
        with self._lock:
            try:
                self._prune_locked(now)
                self._evict_for_locked(total_bytes)
                temp_path.mkdir(mode=0o700)
                data_root = temp_path / "files"
                data_root.mkdir(mode=0o700)
                for item in files:
                    target = data_root.joinpath(
                        *PurePosixPath(item.relative_path).parts
                    )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(item.content)
                    try:
                        target.chmod(0o600)
                    except OSError:
                        pass
                _write_json_atomic(temp_path / "manifest.json", manifest)
                os.replace(temp_path, final_path)
            except AnalysisResultStoreError:
                _safe_remove_directory(temp_path, self.results_root)
                raise
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                _safe_remove_directory(temp_path, self.results_root)
                raise AnalysisResultStoreUnavailable("分析結果無法安全保存") from exc
            self._prune_locked(now, preserve=result_id)
        return StoredAnalysisResult(result_id, snapshot_id, now, tuple(files))

    def get(
        self,
        result_id: str,
        *,
        scope: AnalysisResultScope,
    ) -> StoredAnalysisResult:
        """只在 user/chat 綁定相符時讀取並驗證所有保存檔案。"""

        if (
            not isinstance(result_id, str)
            or _RESULT_ID_PATTERN.fullmatch(result_id) is None
        ):
            raise AnalysisResultNotFound("找不到分析結果")
        with self._lock:
            path = self.results_root / result_id
            manifest = self._read_manifest(path)
            if not self._scope_matches(manifest, scope):
                raise AnalysisResultNotFound("找不到分析結果")
            now = self._clock()
            created_at = _manifest_time(manifest)
            if now - created_at > self.ttl_seconds:
                _safe_remove_directory(path, self.results_root)
                raise AnalysisResultExpired("分析結果已逾期，請重新執行分析")
            self._prune_locked(now, preserve=result_id)
            if not path.is_dir() or path.is_symlink():
                raise AnalysisResultNotFound("找不到分析結果")
            return self._read_result_files(path, manifest)

    def begin_render(
        self,
        result_id: str,
        *,
        scope: AnalysisResultScope,
        message_id: str,
    ) -> RenderClaim:
        """原子保留同一 assistant message 的一次 render，防止重複執行 sandbox。"""

        self._validate_message_id(message_id)
        with self._lock:
            if (
                not isinstance(result_id, str)
                or _RESULT_ID_PATTERN.fullmatch(result_id) is None
            ):
                raise AnalysisResultNotFound("找不到分析結果")
            result_path = self.results_root / result_id
            manifest = self._read_manifest(result_path)
            if not self._scope_matches(manifest, scope):
                raise AnalysisResultNotFound("找不到分析結果")
            if self._clock() - _manifest_time(manifest) > self.ttl_seconds:
                _safe_remove_directory(result_path, self.results_root)
                raise AnalysisResultExpired("分析結果已逾期，請重新執行分析")
            key = _render_key(scope, message_id)
            path = self.renders_root / f"{key}.json"
            now = self._clock()
            self._prune_locked(now, preserve=result_id)
            state = self._read_render_state(path, key)
            if state is not None and now - _manifest_time(state) > self.ttl_seconds:
                _safe_remove_file(path, self.renders_root)
                state = None
            if state is None:
                active_render_count = sum(
                    1
                    for child in self.renders_root.iterdir()
                    if not child.is_symlink()
                    and child.is_file()
                    and re.fullmatch(r"[a-f0-9]{64}\.json", child.name)
                )
                if active_render_count >= MAX_RENDER_STATE_ENTRIES:
                    raise AnalysisResultStoreUnavailable("短期繪圖冪等狀態已達容量上限")
            if state is not None:
                status = state.get("status")
                attempts = _safe_nonnegative_int(state.get("attempts"))
                if status == "completed":
                    published_result_id = state.get("result_id")
                    if (
                        not isinstance(published_result_id, str)
                        or _RESULT_ID_PATTERN.fullmatch(published_result_id) is None
                    ):
                        state["status"] = "unknown"
                        state["updated_at"] = now
                        _write_json_atomic(path, state)
                        return RenderClaim("unknown", attempts)
                    try:
                        published_manifest = self._read_manifest(
                            self.results_root / published_result_id
                        )
                    except AnalysisResultStoreError:
                        published_manifest = None
                    if (
                        published_manifest is None
                        or not self._scope_matches(published_manifest, scope)
                        or now - _manifest_time(published_manifest) > self.ttl_seconds
                    ):
                        state["status"] = "unknown"
                        state["updated_at"] = now
                        _write_json_atomic(path, state)
                        return RenderClaim("unknown", attempts)
                    chart_count = _safe_nonnegative_int(state.get("chart_count", 1))
                    return RenderClaim(
                        "completed", attempts, chart_count, published_result_id
                    )
                if status == "unknown":
                    return RenderClaim("unknown", attempts)
                if status == "terminal":
                    return RenderClaim("terminal", attempts)
                if status == "in_progress":
                    if state.get("instance_id") != self._instance_id:
                        state["status"] = "unknown"
                        state["updated_at"] = now
                        _write_json_atomic(path, state)
                        return RenderClaim("unknown", attempts)
                    return RenderClaim("busy", attempts)
                if attempts >= 4:
                    return RenderClaim("limit", attempts)
            else:
                attempts = 0
                state = {
                    "version": 1,
                    "key": key,
                    "created_at": now,
                }
            state.update(
                {
                    "result_id": result_id,
                    "status": "in_progress",
                    "attempts": attempts + 1,
                    "instance_id": self._instance_id,
                    "updated_at": now,
                }
            )
            _write_json_atomic(path, state)
            return RenderClaim("run", attempts + 1)

    def finish_render(
        self,
        *,
        scope: AnalysisResultScope,
        message_id: str,
        status: str,
        chart_count: int | None = None,
    ) -> None:
        """保存 render 的 completed/failed/terminal/unknown 終態。"""

        self._validate_message_id(message_id)
        if status not in {"completed", "failed", "terminal", "unknown"}:
            raise ValueError("render 終態無效")
        if status == "completed" and (
            isinstance(chart_count, bool)
            or not isinstance(chart_count, int)
            or not 1 <= chart_count <= 4
        ):
            raise ValueError("已完成繪圖的圖數必須介於 1 至 4")
        key = _render_key(scope, message_id)
        path = self.renders_root / f"{key}.json"
        with self._lock:
            state = self._read_render_state(path, key)
            if (
                state is None
                or state.get("instance_id") != self._instance_id
                or state.get("status") != "in_progress"
            ):
                raise AnalysisResultStoreUnavailable("render 狀態無法安全確認")
            state["status"] = status
            if status == "completed":
                state["chart_count"] = chart_count
            state["updated_at"] = self._clock()
            _write_json_atomic(path, state)

    @staticmethod
    def _validate_message_id(message_id: str) -> None:
        if (
            not isinstance(message_id, str)
            or _IDENTITY_PATTERN.fullmatch(message_id) is None
        ):
            raise ValueError("Open WebUI message 識別資訊無效")

    @staticmethod
    def _scope_matches(manifest: dict[str, Any], scope: AnalysisResultScope) -> bool:
        return hmac.compare_digest(
            str(manifest.get("user_hash", "")), _identity_hash(scope.user_id)
        ) and hmac.compare_digest(
            str(manifest.get("chat_hash", "")), _identity_hash(scope.chat_id)
        )

    def _read_manifest(self, result_dir: Path) -> dict[str, Any]:
        try:
            if result_dir.is_symlink() or not result_dir.is_dir():
                raise AnalysisResultNotFound("找不到分析結果")
            root = result_dir.resolve(strict=True)
            root.relative_to(self.results_root.resolve(strict=True))
            manifest_path = result_dir / "manifest.json"
            if manifest_path.is_symlink() or not manifest_path.is_file():
                raise AnalysisResultNotFound("找不到分析結果")
            if manifest_path.stat().st_size > MAX_RESULT_MANIFEST_BYTES:
                raise AnalysisResultStoreUnavailable("分析結果索引超過大小上限")
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("version") != 1
                or payload.get("result_id") != result_dir.name
                or not isinstance(payload.get("files"), list)
                or len(payload["files"]) > self.max_files
                or not isinstance(payload.get("snapshot_id"), str)
            ):
                raise AnalysisResultStoreUnavailable("分析結果索引格式無效")
            return payload
        except AnalysisResultNotFound:
            raise
        except AnalysisResultStoreError:
            raise
        except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AnalysisResultStoreUnavailable("分析結果索引無法讀取") from exc

    def _read_result_files(
        self,
        result_dir: Path,
        manifest: dict[str, Any],
    ) -> StoredAnalysisResult:
        data_root = result_dir / "files"
        try:
            if data_root.is_symlink() or not data_root.is_dir():
                raise AnalysisResultStoreUnavailable("分析結果檔案目錄無效")
            resolved_data_root = data_root.resolve(strict=True)
            resolved_data_root.relative_to(result_dir.resolve(strict=True))
            files: list[StoredAnalysisFile] = []
            total_bytes = 0
            seen: set[str] = set()
            for metadata in manifest["files"]:
                if not isinstance(metadata, dict):
                    raise AnalysisResultStoreUnavailable("分析結果檔案索引無效")
                relative_path = _validate_relative_path(metadata.get("relative_path"))
                if relative_path in seen:
                    raise AnalysisResultStoreUnavailable("分析結果檔案索引重複")
                seen.add(relative_path)
                extension = Path(relative_path).suffix.casefold()
                expected = _STORABLE_TYPES.get(extension)
                if expected is None:
                    raise AnalysisResultStoreUnavailable("分析結果檔案格式不受支援")
                kind, mime_type = expected
                target = data_root.joinpath(*PurePosixPath(relative_path).parts)
                current = data_root
                for component in PurePosixPath(relative_path).parts:
                    current = current / component
                    if current.is_symlink():
                        raise AnalysisResultStoreUnavailable(
                            "分析結果檔案不允許 symlink"
                        )
                resolved_target = target.resolve(strict=True)
                resolved_target.relative_to(resolved_data_root)
                if not target.is_file():
                    raise AnalysisResultStoreUnavailable("分析結果檔案不是一般檔案")
                size_bytes = _safe_nonnegative_int(metadata.get("size_bytes"))
                if (
                    size_bytes > self.max_file_bytes
                    or target.stat().st_size != size_bytes
                ):
                    raise AnalysisResultStoreUnavailable("分析結果檔案大小不一致")
                content = target.read_bytes()
                digest = metadata.get("sha256")
                if not isinstance(digest, str) or not hmac.compare_digest(
                    digest,
                    hashlib.sha256(content).hexdigest(),
                ):
                    raise AnalysisResultStoreUnavailable("分析結果檔案完整性檢查失敗")
                total_bytes += len(content)
                if total_bytes > self.max_result_bytes:
                    raise AnalysisResultStoreUnavailable("分析結果總大小超過上限")
                files.append(
                    StoredAnalysisFile(
                        relative_path=relative_path,
                        kind=kind,
                        extension=extension,
                        mime_type=mime_type,
                        size_bytes=size_bytes,
                        content=content,
                    )
                )
            if not files:
                raise AnalysisResultStoreUnavailable("分析結果沒有可讀檔案")
            return StoredAnalysisResult(
                result_id=result_dir.name,
                snapshot_id=manifest["snapshot_id"],
                created_at=_manifest_time(manifest),
                files=tuple(files),
            )
        except AnalysisResultStoreError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise AnalysisResultStoreUnavailable("分析結果檔案無法安全讀取") from exc

    def _decode_artifacts(self, artifacts: Sequence[Any]) -> list[StoredAnalysisFile]:
        files: list[StoredAnalysisFile] = []
        seen: set[str] = set()
        total_bytes = 0
        for artifact in artifacts:
            relative_path = _validate_relative_path(
                getattr(artifact, "relative_path", None)
            )
            extension = Path(relative_path).suffix.casefold()
            if (
                extension not in _STORABLE_TYPES
                or Path(relative_path).name.casefold() in _RESERVED_OUTPUTS
            ):
                continue
            if relative_path in seen:
                raise AnalysisResultOutputError("分析產物檔名重複")
            seen.add(relative_path)
            expected_kind, expected_mime = _STORABLE_TYPES[extension]
            if (
                getattr(artifact, "kind", None) != expected_kind
                or getattr(artifact, "mime_type", None) != expected_mime
            ):
                raise AnalysisResultOutputError("分析產物格式中繼資料不一致")
            size_bytes = getattr(artifact, "size_bytes", None)
            content_base64 = getattr(artifact, "content_base64", None)
            if (
                isinstance(size_bytes, bool)
                or not isinstance(size_bytes, int)
                or size_bytes < 0
                or size_bytes > self.max_file_bytes
                or not isinstance(content_base64, str)
            ):
                raise AnalysisResultOutputError("分析產物超過保存限制")
            try:
                content = base64.b64decode(content_base64, validate=True)
            except (ValueError, TypeError) as exc:
                raise AnalysisResultOutputError("分析產物編碼無效") from exc
            if len(content) != size_bytes:
                raise AnalysisResultOutputError("分析產物大小不一致")
            total_bytes += size_bytes
            if total_bytes > self.max_result_bytes:
                raise AnalysisResultOutputError("分析產物總大小超過保存上限")
            files.append(
                StoredAnalysisFile(
                    relative_path=relative_path,
                    kind=expected_kind,
                    extension=extension,
                    mime_type=expected_mime,
                    size_bytes=size_bytes,
                    content=content,
                )
            )
            if len(files) > self.max_files:
                raise AnalysisResultOutputError("分析產物數量超過保存上限")
        if not files:
            raise AnalysisResultOutputError(
                "分析須輸出至少一個可供後續繪圖使用的 JSON、CSV 或 JSONL 檔案"
            )
        return files

    def _read_render_state(self, path: Path, key: str) -> dict[str, Any] | None:
        try:
            if not path.exists():
                return None
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
                raise AnalysisResultStoreUnavailable("繪圖狀態檔案無效")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("version") != 1
                or payload.get("key") != key
            ):
                raise AnalysisResultStoreUnavailable("繪圖狀態格式無效")
            return payload
        except AnalysisResultStoreError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AnalysisResultStoreUnavailable("繪圖狀態無法讀取") from exc

    def _prune_locked(self, now: float, *, preserve: str | None = None) -> None:
        entries: list[tuple[Path, float, int]] = []
        try:
            children = list(self.results_root.iterdir())
        except OSError as exc:
            raise AnalysisResultStoreUnavailable("分析結果目錄無法讀取") from exc
        for child in children:
            if (
                re.fullmatch(r"\.tmp-[a-f0-9]{48}", child.name)
                and not child.is_symlink()
                and child.is_dir()
            ):
                _safe_remove_directory(child, self.results_root)
                continue
            if (
                child.name == preserve
                or _RESULT_ID_PATTERN.fullmatch(child.name) is None
                or child.is_symlink()
                or not child.is_dir()
            ):
                continue
            try:
                manifest = self._read_manifest(child)
                created_at = _manifest_time(manifest)
                total_bytes = _safe_nonnegative_int(manifest.get("total_bytes"))
            except AnalysisResultStoreError:
                _safe_remove_directory(child, self.results_root)
                continue
            if now - created_at > self.ttl_seconds:
                _safe_remove_directory(child, self.results_root)
                continue
            entries.append((child, created_at, total_bytes))
        entries.sort(key=lambda item: item[1])
        while (
            len(entries) > self.max_results
            or sum(entry[2] for entry in entries) > self.max_cache_bytes
        ):
            path, _, _ = entries.pop(0)
            _safe_remove_directory(path, self.results_root)

        try:
            render_states = list(self.renders_root.iterdir())
        except OSError as exc:
            raise AnalysisResultStoreUnavailable("繪圖狀態目錄無法讀取") from exc
        for state_path in render_states:
            if state_path.is_symlink() or not state_path.is_file():
                continue
            if re.fullmatch(r"[a-f0-9]{64}\.json", state_path.name) is None:
                continue
            state = self._read_render_state(state_path, state_path.stem)
            if state is not None and now - _manifest_time(state) > self.ttl_seconds:
                _safe_remove_file(state_path, self.renders_root)

    def _evict_for_locked(self, incoming_bytes: int) -> None:
        if incoming_bytes > self.max_cache_bytes:
            raise AnalysisResultStoreUnavailable("分析結果超過暫存容量上限")
        entries: list[tuple[Path, float, int]] = []
        for child in self.results_root.iterdir():
            if (
                child.is_symlink()
                or not child.is_dir()
                or _RESULT_ID_PATTERN.fullmatch(child.name) is None
            ):
                continue
            try:
                manifest = self._read_manifest(child)
                entries.append(
                    (
                        child,
                        _manifest_time(manifest),
                        _safe_nonnegative_int(manifest.get("total_bytes")),
                    )
                )
            except AnalysisResultStoreError:
                _safe_remove_directory(child, self.results_root)
        entries.sort(key=lambda item: item[1])
        total_bytes = sum(item[2] for item in entries)
        while entries and (
            len(entries) >= self.max_results
            or total_bytes + incoming_bytes > self.max_cache_bytes
        ):
            path, _, size_bytes = entries.pop(0)
            _safe_remove_directory(path, self.results_root)
            total_bytes -= size_bytes


def _identity_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _render_key(scope: AnalysisResultScope, message_id: str) -> str:
    payload = f"{scope.user_id}\0{scope.chat_id}\0{message_id}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_relative_path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or "\\" in value
        or ":" in value
        or "\x00" in value
    ):
        raise AnalysisResultOutputError("分析產物路徑不符合契約")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or not 1 <= len(path.parts) <= 4
        or any(
            component in {".", ".."}
            or _FILE_COMPONENT_PATTERN.fullmatch(component) is None
            for component in path.parts
        )
    ):
        raise AnalysisResultOutputError("分析產物路徑不符合契約")
    return value


def _manifest_time(payload: dict[str, Any]) -> float:
    value = payload.get("created_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise AnalysisResultStoreUnavailable("分析結果時間欄位無效")
    return float(value)


def _safe_nonnegative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AnalysisResultStoreUnavailable("分析結果大小欄位無效")
    return value


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(data) > MAX_RESULT_MANIFEST_BYTES:
            raise AnalysisResultStoreUnavailable("分析結果索引超過大小上限")
        with temp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temp.chmod(0o600)
        except OSError:
            pass
        os.replace(temp, path)
    except AnalysisResultStoreError:
        _safe_remove_file(temp, path.parent)
        raise
    except OSError as exc:
        _safe_remove_file(temp, path.parent)
        raise AnalysisResultStoreUnavailable("分析結果索引無法保存") from exc


def _safe_remove_directory(path: Path, parent: Path) -> None:
    try:
        if path.is_symlink():
            return
        resolved_parent = parent.resolve(strict=True)
        resolved = path.resolve(strict=False)
        if resolved.parent != resolved_parent:
            return
        if resolved.exists():
            shutil.rmtree(resolved)
    except (OSError, RuntimeError, ValueError):
        return


def _safe_remove_file(path: Path, parent: Path) -> None:
    try:
        if path.is_symlink():
            path.unlink()
            return
        if path.resolve(strict=False).parent == parent.resolve(strict=True):
            path.unlink(missing_ok=True)
    except (OSError, RuntimeError, ValueError):
        return


__all__ = [
    "AnalysisResultExpired",
    "AnalysisResultNotFound",
    "AnalysisResultOutputError",
    "AnalysisResultScope",
    "AnalysisResultStore",
    "AnalysisResultStoreError",
    "AnalysisResultStoreUnavailable",
    "RenderClaim",
    "StoredAnalysisFile",
    "StoredAnalysisResult",
]
