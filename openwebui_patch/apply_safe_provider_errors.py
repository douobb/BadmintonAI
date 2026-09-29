"""在固定 Open WebUI 版本中遮蔽供應商 HTTP 錯誤的原始內容。"""

from __future__ import annotations

from pathlib import Path

BACKEND_ROOT = Path("/app/backend/open_webui")

ORIGINAL_DETAIL = '''def get_response_error_detail(response: object) -> str:
    status_code = getattr(response, 'status_code', None)
    fallback = f'Provider returned HTTP {status_code}' if status_code else 'Provider returned an error'

    try:
        body = response.body
        if not isinstance(body, str):
            body = body.decode('utf-8', 'replace')
        detail = JSONCodec.loads(body)
    except Exception:
        return fallback

    while isinstance(detail, dict):
        next_detail = None
        for key in ('error', 'message', 'detail'):
            if key in detail:
                next_detail = detail[key]
                break
        if next_detail is None:
            return str(detail)
        detail = next_detail

    return detail if isinstance(detail, str) else str(detail)
'''

SAFE_DETAIL = '''def get_response_error_detail(response: object) -> str:
    """僅依 HTTP 狀態回報安全訊息，不將供應商本文送入聊天紀錄。"""
    status_code = getattr(response, 'status_code', None)
    if status_code == 429:
        return '模型服務回傳 HTTP 429：目前請求過多或額度受限，請稍後再試；本輪不會自動重送。'
    if status_code in (401, 403):
        return '模型服務驗證或權限失敗，請管理員檢查連線設定。'
    if isinstance(status_code, int) and status_code >= 500:
        return '模型服務暫時無法使用，請稍後再試；本輪不會自動重送。'
    if isinstance(status_code, int) and status_code >= 400:
        return '模型服務拒絕本次請求，請檢查模型設定或稍後再試。'
    return '模型服務發生錯誤，請稍後再試。'
'''

ORIGINAL_MAIN_GUARD = (
    "if isinstance(response, JSONResponse) and response.status_code >= 400:"
)
SAFE_MAIN_GUARD = "if isinstance(response, Response) and response.status_code >= 400:"


def _replace_once(path: Path, original: str, replacement: str) -> None:
    source = path.read_text(encoding="utf-8")
    if source.count(original) != 1:
        raise RuntimeError(f"Open WebUI 原始碼與已驗證的 v0.11.3 不符：{path.name}")
    path.write_text(source.replace(original, replacement), encoding="utf-8")


def apply_patch(backend_root: Path = BACKEND_ROOT) -> None:
    _replace_once(
        backend_root / "utils" / "misc.py", ORIGINAL_DETAIL, SAFE_DETAIL
    )
    _replace_once(
        backend_root / "main.py", ORIGINAL_MAIN_GUARD, SAFE_MAIN_GUARD
    )


if __name__ == "__main__":
    apply_patch()
