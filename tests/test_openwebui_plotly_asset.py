"""驗證 Plotly 同站代理及鎖定版 Open WebUI patch。"""

from __future__ import annotations

import shutil
import subprocess
from email.message import Message
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openwebui_patch import apply_plotly_asset as patch
from openwebui_patch import plotly_asset_route as route


class _UpstreamResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        content_type: str = "application/javascript; charset=utf-8",
        version: str = "6.6.0",
        content: bytes = b"/* plotly.js v3.4.0 */\nwindow.Plotly = {};",
    ) -> None:
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["X-Plotly-Python-Version"] = version
        self.headers["Content-Length"] = str(len(content))
        self._content = content

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int) -> bytes:
        return self._content[:size]


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(route.router, prefix="/badmintonai")
    return TestClient(app)


def test_public_asset_route_proxies_only_fixed_internal_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, int]] = []

    def open_upstream(request, timeout):
        calls.append((request.full_url, request.get_method(), timeout))
        return _UpstreamResponse()

    monkeypatch.setattr(route, "_open_upstream", open_upstream)
    response = _client().get("/badmintonai/assets/plotly-6.6.0.min.js")

    assert response.status_code == 200
    assert response.content == b"/* plotly.js v3.4.0 */\nwindow.Plotly = {};"
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-plotly-python-version"] == "6.6.0"
    assert calls == [
        (route.PLOTLY_ASSET_URL, "GET", route.PLOTLY_ASSET_TIMEOUT_SECONDS)
    ]


@pytest.mark.parametrize(
    ("status", "content_type", "version", "content"),
    [
        (503, "application/javascript", "6.6.0", b""),
        (200, "text/html", "6.6.0", b"/* plotly.js v3.4.0 */"),
        (200, "application/javascript", "6.5.0", b"/* plotly.js v3.4.0 */"),
        (200, "application/javascript", "6.6.0", b"/* plotly.js v3.3.0 */"),
    ],
)
def test_route_maps_bad_upstream_responses_to_gateway_error(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    content_type: str,
    version: str,
    content: bytes,
) -> None:
    monkeypatch.setattr(
        route,
        "_open_upstream",
        lambda *_args, **_kwargs: _UpstreamResponse(
            status=status,
            content_type=content_type,
            version=version,
            content=content,
        ),
    )

    response = _client().get("/badmintonai/assets/plotly-6.6.0.min.js")

    assert response.status_code == 502
    assert response.json() == {"detail": "固定 Plotly 資產目前不可用"}


def test_route_rejects_oversized_or_unreachable_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    large_body = b"/* plotly.js v3.4.0 */" + b"x" * route.MAX_PLOTLY_ASSET_BYTES
    monkeypatch.setattr(
        route,
        "_open_upstream",
        lambda *_args, **_kwargs: _UpstreamResponse(content=large_body),
    )
    client = _client()
    oversized = client.get("/badmintonai/assets/plotly-6.6.0.min.js")
    assert oversized.status_code == 502

    monkeypatch.setattr(
        route,
        "_open_upstream",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    unavailable = client.get("/badmintonai/assets/plotly-6.6.0.min.js")
    assert unavailable.status_code == 502

    truncated = _UpstreamResponse()
    truncated.headers.replace_header("Content-Length", "100")
    monkeypatch.setattr(route, "_open_upstream", lambda *_args, **_kwargs: truncated)
    incomplete = client.get("/badmintonai/assets/plotly-6.6.0.min.js")
    assert incomplete.status_code == 502


def test_route_has_no_arbitrary_asset_path() -> None:
    response = _client().get("/badmintonai/assets/plotly-other.js")

    assert response.status_code == 404


def test_upstream_redirects_are_not_followed() -> None:
    request = route.urllib.request.Request(route.PLOTLY_ASSET_URL)

    assert (
        route._NoRedirectHandler().redirect_request(
            request, None, 302, "Found", Message(), "https://example.invalid/asset.js"
        )
        is None
    )


def test_frontend_patch_only_adds_exact_legacy_normalizer_before_srcdoc() -> None:
    source = "\n".join(
        (
            "<script lang='ts'>",
            "\timport { injectCsp } from '$lib/utils/csp';",
            "\tiframeDoc = await processHtmlForDeps(src as string);",
            "\t\t\t\tsrcdoc={injectCsp(iframeDoc, $config?.ui?.iframe_csp ?? '')}",
            "\t\t\t\t{sandbox}",
            "</script>",
        )
    )

    patched = patch.patch_frontend_source(source)

    assert (
        "import { normalizeLegacyPlotlyAsset } from '$lib/utils/badmintonaiPlotlyAsset.js';"
        in patched
    )
    assert patch.FRONTEND_CALL_PATCH in patched
    assert "srcdoc={injectCsp(iframeDoc, $config?.ui?.iframe_csp ?? '')}" in patched
    assert "{sandbox}" in patched
    assert patch.patch_frontend_source(patched) == patched


def test_backend_patch_mounts_route_on_general_openwebui_app() -> None:
    source = "\n".join(
        (
            "from open_webui.routers import (",
            "    utils,",
            ")",
            "app.include_router(calendar.router, prefix='/api/v1/calendars', tags=['calendars'])",
        )
    )

    patched = patch.patch_backend_source(source)

    assert "    badmintonai_plotly_asset," in patched
    assert (
        "app.include_router(badmintonai_plotly_asset.router, prefix='/badmintonai')"
        in patched
    )
    assert patch.patch_backend_source(patched) == patched


def test_patcher_rejects_upstream_drift_and_installs_route_files(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="route 結構不符"):
        patch.patch_backend_source("from open_webui.routers import (\n)\n")
    with pytest.raises(RuntimeError, match="FullHeightIframe srcdoc"):
        patch.patch_frontend_source(patch.FRONTEND_IMPORT)

    frontend = tmp_path / "frontend"
    frontend_file = frontend / patch.FRONTEND_PATH
    frontend_file.parent.mkdir(parents=True)
    frontend_file.write_text(
        "\n".join(
            (
                "<script lang='ts'>",
                patch.FRONTEND_IMPORT,
                patch.FRONTEND_CALL,
                "</script>",
            )
        ),
        encoding="utf-8",
    )
    patch.apply_frontend_patch(frontend)
    assert (frontend / "src/lib/utils/badmintonaiPlotlyAsset.js").is_file()
    assert patch.FRONTEND_CALL_PATCH in frontend_file.read_text(encoding="utf-8")

    backend = tmp_path / "backend"
    main_file = backend / "backend/open_webui/main.py"
    main_file.parent.mkdir(parents=True)
    main_file.write_text(
        "\n".join(
            (
                "from open_webui.routers import (",
                "    utils,",
                ")",
                patch.ROUTER_MOUNT,
            )
        ),
        encoding="utf-8",
    )
    patch.apply_backend_patch(backend)
    assert (
        backend / "backend/open_webui/routers/badmintonai_plotly_asset.py"
    ).is_file()
    assert "app.include_router(badmintonai_plotly_asset.router" in main_file.read_text(
        encoding="utf-8"
    )


def test_exact_historical_embed_normalization_in_node() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js 不可用")
    root = Path(__file__).resolve().parents[1]
    harness = Path(__file__).with_name("plotly_asset_normalize_harness.cjs")
    result = subprocess.run(
        [
            node,
            str(harness),
            str(root / "openwebui_patch/plotly_asset_normalize.js"),
        ],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
