"""將固定 Plotly route 與歷史 embed 正規化器套用至鎖定版 Open WebUI。"""

from __future__ import annotations

import argparse
from pathlib import Path

ROUTER_IMPORT = "from open_webui.routers import ("
ROUTER_IMPORT_ENTRY = "    badmintonai_plotly_asset,\n"
ROUTER_IMPORT_END = "    utils,\n)"
ROUTER_MOUNT = "app.include_router(calendar.router, prefix='/api/v1/calendars', tags=['calendars'])"
ROUTER_MOUNT_PATCH = (
    ROUTER_MOUNT
    + "\napp.include_router(badmintonai_plotly_asset.router, prefix='/badmintonai')"
)
FRONTEND_PATH = Path("src/lib/components/common/FullHeightIframe.svelte")
FRONTEND_IMPORT = "\timport { injectCsp } from '$lib/utils/csp';"
FRONTEND_IMPORT_PATCH = (
    FRONTEND_IMPORT
    + "\n\timport { normalizeLegacyPlotlyAsset } from '$lib/utils/badmintonaiPlotlyAsset.js';"
)
FRONTEND_CALL = "iframeDoc = await processHtmlForDeps(src as string);"
FRONTEND_CALL_PATCH = (
    "iframeDoc = await processHtmlForDeps(normalizeLegacyPlotlyAsset(src as string));"
)


def _replace_once(source: str, original: str, replacement: str, label: str) -> str:
    if source.count(original) != 1:
        raise RuntimeError(f"Open WebUI {label} 結構不符，拒絕套用 Plotly 補丁")
    return source.replace(original, replacement, 1)


def patch_frontend_source(source: str) -> str:
    """在原生 srcdoc renderer 前只轉換已知歷史 URL。"""

    has_import = "normalizeLegacyPlotlyAsset" in source
    has_call = FRONTEND_CALL_PATCH in source
    if has_import or has_call:
        if has_import and has_call:
            return source
        raise RuntimeError("Open WebUI Plotly renderer 補丁不完整")
    source = _replace_once(
        source, FRONTEND_IMPORT, FRONTEND_IMPORT_PATCH, "FullHeightIframe CSP import"
    )
    return _replace_once(
        source, FRONTEND_CALL, FRONTEND_CALL_PATCH, "FullHeightIframe srcdoc"
    )


def patch_backend_source(source: str) -> str:
    """將固定 route 掛在 Open WebUI 一般聊天使用的後端 app。"""

    has_import = ROUTER_IMPORT_ENTRY in source
    has_mount = "app.include_router(badmintonai_plotly_asset.router" in source
    if has_import or has_mount:
        if has_import and has_mount:
            return source
        raise RuntimeError("Open WebUI Plotly route 補丁不完整")
    if (
        source.count(ROUTER_IMPORT) != 1
        or source.count(ROUTER_IMPORT_END) != 1
        or source.count(ROUTER_MOUNT) != 1
    ):
        raise RuntimeError("Open WebUI main.py route 結構不符，拒絕套用 Plotly 補丁")
    source = source.replace(
        ROUTER_IMPORT_END,
        "    utils,\n" + ROUTER_IMPORT_ENTRY + ")",
        1,
    )
    return _replace_once(source, ROUTER_MOUNT, ROUTER_MOUNT_PATCH, "calendar router")


def apply_frontend_patch(root: Path) -> None:
    source_path = root / FRONTEND_PATH
    helper_path = root / "src/lib/utils/badmintonaiPlotlyAsset.js"
    source = patch_frontend_source(source_path.read_text(encoding="utf-8"))
    helper_path.parent.mkdir(parents=True, exist_ok=True)
    helper_path.write_text(
        Path(__file__)
        .with_name("plotly_asset_normalize.js")
        .read_text(encoding="utf-8"),
        encoding="utf-8",
        newline="\n",
    )
    source_path.write_text(source, encoding="utf-8", newline="\n")


def apply_backend_patch(root: Path) -> None:
    source_path = root / "backend/open_webui/main.py"
    route_path = root / "backend/open_webui/routers/badmintonai_plotly_asset.py"
    source = patch_backend_source(source_path.read_text(encoding="utf-8"))
    route_path.parent.mkdir(parents=True, exist_ok=True)
    route_path.write_text(
        Path(__file__).with_name("plotly_asset_route.py").read_text(encoding="utf-8"),
        encoding="utf-8",
        newline="\n",
    )
    source_path.write_text(source, encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--frontend", action="store_true")
    mode.add_argument("--backend", action="store_true")
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    if args.frontend:
        apply_frontend_patch(args.root)
    else:
        apply_backend_patch(args.root)


if __name__ == "__main__":
    main()
