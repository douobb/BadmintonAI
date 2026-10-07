"""分析結果短期保存、範圍隔離與繪圖冪等狀態測試。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from badminton_ai.sandbox import SandboxArtifact
from badminton_ai.server.result_store import (
    AnalysisResultExpired,
    AnalysisResultNotFound,
    AnalysisResultOutputError,
    AnalysisResultScope,
    AnalysisResultStore,
)


def _artifact(relative_path: str = "summary.json", content: bytes = b'{"ok":true}'):
    extension = Path(relative_path).suffix
    kind, mime_type = {
        ".json": ("json", "application/json"),
        ".csv": ("table", "text/csv"),
        ".jsonl": ("json", "application/jsonl"),
    }.get(extension, ("other", "application/octet-stream"))
    return SandboxArtifact(
        relative_path=relative_path,
        kind=kind,
        extension=extension,
        mime_type=mime_type,
        size_bytes=len(content),
        content_base64=base64.b64encode(content).decode("ascii"),
    )


def _scope(user_id: str = "user-1", chat_id: str = "chat-1") -> AnalysisResultScope:
    return AnalysisResultScope(user_id, chat_id)


def test_result_survives_store_recreation_and_is_bound_to_user_and_chat(
    tmp_path: Path,
) -> None:
    root = tmp_path / "saved"
    store = AnalysisResultStore(root)
    saved = store.save(
        scope=_scope(), snapshot_id="sha256:snapshot", artifacts=[_artifact()]
    )

    reopened = AnalysisResultStore(root)
    loaded = reopened.get(saved.result_id, scope=_scope())
    assert loaded.files[0].content == b'{"ok":true}'
    assert loaded.snapshot_id == "sha256:snapshot"
    for unauthorized in (_scope("other-user"), _scope(chat_id="other-chat")):
        with pytest.raises(AnalysisResultNotFound):
            reopened.get(saved.result_id, scope=unauthorized)
    with pytest.raises(AnalysisResultNotFound):
        reopened.get("../manifest.json", scope=_scope())


def test_result_store_rejects_traversal_and_nontext_artifacts(tmp_path: Path) -> None:
    store = AnalysisResultStore(tmp_path / "saved")
    for artifact in (
        _artifact("../outside.csv", b"x\n1\n"),
        _artifact("C:/outside.csv", b"x\n1\n"),
    ):
        with pytest.raises(AnalysisResultOutputError):
            store.save(
                scope=_scope(), snapshot_id="sha256:snapshot", artifacts=[artifact]
            )
    with pytest.raises(AnalysisResultOutputError):
        store.save(
            scope=_scope(),
            snapshot_id="sha256:snapshot",
            artifacts=[_artifact("image.png", b"not data")],
        )


def test_result_store_enforces_expiry_and_capacity(tmp_path: Path) -> None:
    now = [100.0]
    root = tmp_path / "saved"
    expiring = AnalysisResultStore(root, ttl_seconds=10, clock=lambda: now[0])
    result = expiring.save(
        scope=_scope(), snapshot_id="sha256:snapshot", artifacts=[_artifact()]
    )
    now[0] += 11
    with pytest.raises(AnalysisResultExpired):
        expiring.get(result.result_id, scope=_scope())

    bounded = AnalysisResultStore(root, max_results=1, clock=lambda: now[0])
    first = bounded.save(
        scope=_scope(), snapshot_id="sha256:first", artifacts=[_artifact()]
    )
    second = bounded.save(
        scope=_scope(), snapshot_id="sha256:second", artifacts=[_artifact()]
    )
    with pytest.raises(AnalysisResultNotFound):
        bounded.get(first.result_id, scope=_scope())
    assert bounded.get(second.result_id, scope=_scope()).snapshot_id == "sha256:second"

    byte_bounded = AnalysisResultStore(
        tmp_path / "bytes",
        max_cache_bytes=3,
        max_result_bytes=3,
        max_file_bytes=3,
    )
    oldest = byte_bounded.save(
        scope=_scope(),
        snapshot_id="sha256:oldest",
        artifacts=[_artifact(content=b"123")],
    )
    newest = byte_bounded.save(
        scope=_scope(),
        snapshot_id="sha256:newest",
        artifacts=[_artifact(content=b"456")],
    )
    with pytest.raises(AnalysisResultNotFound):
        byte_bounded.get(oldest.result_id, scope=_scope())
    assert byte_bounded.get(newest.result_id, scope=_scope()).files[0].content == b"456"


def test_render_idempotency_persists_and_unknown_inflight_is_not_replayed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "saved"
    store = AnalysisResultStore(root)
    saved = store.save(
        scope=_scope(), snapshot_id="sha256:snapshot", artifacts=[_artifact()]
    )
    assert (
        store.begin_render(
            saved.result_id, scope=_scope(), message_id="message-1"
        ).status
        == "run"
    )
    reopened = AnalysisResultStore(root)
    assert (
        reopened.begin_render(
            saved.result_id, scope=_scope(), message_id="message-1"
        ).status
        == "unknown"
    )

    claim = reopened.begin_render(
        saved.result_id, scope=_scope(), message_id="message-2"
    )
    assert claim.status == "run"
    reopened.finish_render(
        scope=_scope(), message_id="message-2", status="completed", chart_count=2
    )
    another_restart = AnalysisResultStore(root)
    duplicate = another_restart.begin_render(
        saved.result_id, scope=_scope(), message_id="message-2"
    )
    assert duplicate.status == "completed"
    assert duplicate.chart_count == 2
    assert duplicate.result_id == saved.result_id
    assert (
        another_restart.begin_render(
            saved.result_id, scope=_scope(), message_id="message-3"
        ).status
        == "run"
    )


@pytest.mark.parametrize("stored_result_id", [None, "../manifest.json", "f" * 48])
def test_completed_render_without_verified_result_identity_is_unknown(
    tmp_path: Path, stored_result_id: str | None
) -> None:
    root = tmp_path / "saved"
    store = AnalysisResultStore(root)
    saved = store.save(
        scope=_scope(), snapshot_id="sha256:snapshot", artifacts=[_artifact()]
    )
    assert (
        store.begin_render(
            saved.result_id, scope=_scope(), message_id="message-1"
        ).status
        == "run"
    )
    store.finish_render(
        scope=_scope(), message_id="message-1", status="completed", chart_count=1
    )
    state_path = next(store.renders_root.glob("*.json"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if stored_result_id is None:
        state.pop("result_id")
    else:
        state["result_id"] = stored_result_id
    state_path.write_text(json.dumps(state), encoding="utf-8")

    reopened = AnalysisResultStore(root)
    claim = reopened.begin_render(
        saved.result_id, scope=_scope(), message_id="message-1"
    )

    assert claim.status == "unknown"
    assert claim.result_id is None
