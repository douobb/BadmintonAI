"""精確同步既有 badminton-analysis；預設只讀，套用需比對審核時的內容雜湊。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

SKILL_ID = "badminton-analysis"
ROOT = Path(__file__).resolve().parents[1]


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def update_payload(live: dict, content: str, expected_hash: str) -> dict:
    if live.get("id") != SKILL_ID or not isinstance(live.get("content"), str):
        raise ValueError("讀回 Skill ID 或內容格式不符")
    if content_hash(live["content"]) != expected_hash:
        raise ValueError("線上 Skill 已變更，需重新審核，未更新")
    # 不提交 access_grants，原生 SkillForm 的 None 預設會保留現有權限。
    return {
        **{
            key: live[key] for key in ("id", "name", "description", "meta", "is_active")
        },
        "content": content,
    }


def _config() -> dict[str, str]:
    config = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip().strip("\"'")
    return {**config, **os.environ}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-live-sha256")
    args = parser.parse_args()
    if args.apply and not args.expected_live_sha256:
        parser.error("--apply 必須提供審核時的 --expected-live-sha256")
    config = _config()
    token = config.get("BADMINTON_AI_OPEN_WEBUI_API_KEY", "")
    port = config.get("OPEN_WEBUI_HOST_PORT", "3000")
    if not token or not port.isdigit():
        raise ValueError("本機連線設定不完整")
    url = f"http://127.0.0.1:{port}/api/v1/skills/id/{SKILL_ID}"

    def request(method: str, body: dict | None = None) -> dict:
        req = Request(
            url + ("/update" if method == "POST" else ""),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urlopen(req, timeout=30) as response:
                result = json.load(response)
        except (URLError, TimeoutError):
            raise ValueError("Skill 請求失敗；未輸出憑證或第三方回應") from None
        if not isinstance(result, dict) or result.get("id") != SKILL_ID:
            raise ValueError("Skill 讀回 ID 不符")
        return result

    local = (ROOT / "skills" / "badminton-analysis.md").read_text(encoding="utf-8")
    live = request("GET")
    live_hash = content_hash(live["content"])
    if args.apply:
        request("POST", update_payload(live, local, args.expected_live_sha256))
        verified = request("GET")
        if verified.get("content") != local:
            raise ValueError("同步後內容讀回不一致")
        live_hash = content_hash(verified["content"])
    print(
        json.dumps(
            {
                "id": SKILL_ID,
                "applied": args.apply,
                "live_sha256": live_hash,
                "local_sha256": content_hash(local),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
