"""彙總評測中已實際回報的 Token 用量與欄位完整度。"""

from __future__ import annotations

from typing import Any

USAGE_FIELDS: dict[str, tuple[str, ...]] = {
    "input_tokens": ("input_tokens", "prompt_tokens"),
    "output_tokens": ("output_tokens", "completion_tokens"),
    "total_tokens": ("total_tokens",),
}


def canonical_usage(usage: Any, field: str) -> int | None:
    """讀取欄位別名，不用其他欄位推算未知數值。"""

    if not isinstance(usage, dict):
        return None
    for source_key in USAGE_FIELDS[field]:
        value = usage.get(source_key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def attempts_for_question(question: Any) -> list[dict[str, Any]]:
    """只取保存於 attempts 的實際請求，避免再把 turn.result 加總一次。"""

    if not isinstance(question, dict):
        return []
    attempts: list[dict[str, Any]] = []
    turns = question.get("turns")
    if isinstance(turns, list):
        for turn in turns:
            if isinstance(turn, dict) and isinstance(turn.get("attempts"), list):
                attempts.extend(
                    attempt for attempt in turn["attempts"] if isinstance(attempt, dict)
                )
    pending = question.get("pending_turn")
    if isinstance(pending, dict) and isinstance(pending.get("attempts"), list):
        attempts.extend(
            attempt for attempt in pending["attempts"] if isinstance(attempt, dict)
        )
    return attempts


def summarize_attempts(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """保留舊 strict totals，另回報已知合計及每欄缺少次數。"""

    count = len(attempts)
    fields: dict[str, dict[str, Any]] = {}
    strict_totals: dict[str, int | None] = {}
    observed_totals: dict[str, int | None] = {}
    any_known = False
    for field in USAGE_FIELDS:
        values = [
            canonical_usage(item.get("result", {}).get("usage"), field)
            if isinstance(item.get("result"), dict)
            else None
            for item in attempts
        ]
        known = [value for value in values if value is not None]
        missing = count - len(known)
        known_total = sum(known) if known else None
        complete = count > 0 and missing == 0
        any_known = any_known or bool(known)
        strict_totals[field] = known_total if complete else None
        observed_totals[field] = known_total
        fields[field] = {
            "known_total": known_total,
            "known_attempt_count": len(known),
            "attempt_count": count,
            "missing_attempt_count": missing,
            "complete": complete,
        }
    return {
        "attempt_count": count,
        "has_usage": any_known,
        "token_totals": strict_totals,
        "observed_totals": observed_totals,
        "fields": fields,
    }


def summarize_question(question: Any) -> dict[str, Any]:
    return summarize_attempts(attempts_for_question(question))


def summarize_questions(questions: Any) -> dict[str, Any]:
    """以所有逐題實際 attempts 計算整輪摘要。"""

    attempts: list[dict[str, Any]] = []
    if isinstance(questions, list):
        for question in questions:
            attempts.extend(attempts_for_question(question))
    return summarize_attempts(attempts)


def format_usage_summary(summary: Any) -> str:
    """將完整、部分與全缺用量標示成不會誤讀為完整總計的文字。"""

    if not isinstance(summary, dict):
        return "尚未取得模型實際用量"
    attempt_count = summary.get("attempt_count")
    if not isinstance(attempt_count, int) or isinstance(attempt_count, bool):
        return "尚未取得模型實際用量"
    if attempt_count <= 0:
        return "尚未取得模型實際用量（尚無已送出的模型請求）"
    fields = summary.get("fields")
    if not isinstance(fields, dict):
        return "尚未取得模型實際用量"
    labels = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "total_tokens": "total_tokens",
    }
    parts: list[str] = []
    for key, label in labels.items():
        field = fields.get(key)
        if not isinstance(field, dict):
            continue
        known = field.get("known_total")
        missing = field.get("missing_attempt_count")
        complete = field.get("complete") is True
        if complete and isinstance(known, int) and not isinstance(known, bool):
            parts.append(f"{label}: {known}")
        elif isinstance(known, int) and not isinstance(known, bool):
            parts.append(
                f"{label}: 已取得 {known}（缺少 {missing} 次用量；實際可能更高）"
            )
        else:
            parts.append(f"{label}: 未取得（缺少 {missing} 次用量；實際可能更高）")
    if summary.get("has_usage") is not True:
        return f"尚未取得模型實際用量（{attempt_count} 次請求用量缺失）"
    return " · ".join(parts) if parts else "尚未取得模型實際用量"
