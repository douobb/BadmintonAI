"""固定版 Open WebUI 供應商錯誤遮蔽的建置檢查。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PATCH_PATH = PROJECT_ROOT / "openwebui_patch" / "apply_safe_provider_errors.py"


def _load_patch() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "safe_provider_patch_test", PATCH_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_patch_hides_provider_body_and_covers_plain_text_response(
    tmp_path: Path,
) -> None:
    patch = _load_patch()
    misc = tmp_path / "utils" / "misc.py"
    misc.parent.mkdir()
    misc.write_text(patch.ORIGINAL_DETAIL, encoding="utf-8")
    main = tmp_path / "main.py"
    main.write_text(patch.ORIGINAL_MAIN_GUARD, encoding="utf-8")

    patch.apply_patch(tmp_path)

    namespace: dict[str, object] = {}
    exec(misc.read_text(encoding="utf-8"), namespace)
    detail = namespace["get_response_error_detail"]
    assert callable(detail)
    response = type("Response", (), {"status_code": 429, "body": b"secret-id"})()
    assert "secret-id" not in detail(response)
    assert "429" in detail(response)
    assert "稍後再試" in detail(response)
    assert "isinstance(response, Response)" in main.read_text(encoding="utf-8")


def test_patch_rejects_unexpected_upstream_source(tmp_path: Path) -> None:
    patch = _load_patch()
    misc = tmp_path / "utils" / "misc.py"
    misc.parent.mkdir()
    misc.write_text("def get_response_error_detail(response): return response.body")
    (tmp_path / "main.py").write_text(patch.ORIGINAL_MAIN_GUARD)

    with pytest.raises(RuntimeError, match="原始碼與已驗證"):
        patch.apply_patch(tmp_path)
