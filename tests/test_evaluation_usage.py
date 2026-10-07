"""評測用量彙總的完整性與重複計算回歸測試。"""

from __future__ import annotations

from scripts.evaluation_usage import (
    format_usage_summary,
    summarize_attempts,
    summarize_question,
    summarize_questions,
)


def _attempt(usage: dict[str, int] | None) -> dict[str, object]:
    return {"result": {"usage": usage} if usage is not None else None}


def test_attempt_coverage_is_independent_per_field_and_never_estimated() -> None:
    summary = summarize_attempts(
        [
            _attempt({"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}),
            _attempt({"input_tokens": 5, "total_tokens": 99}),
            _attempt(None),
        ]
    )

    assert summary["attempt_count"] == 3
    assert summary["observed_totals"] == {
        "input_tokens": 12,
        "output_tokens": 3,
        "total_tokens": 109,
    }
    assert summary["token_totals"] == {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
    }
    assert summary["fields"]["input_tokens"]["missing_attempt_count"] == 1
    assert summary["fields"]["output_tokens"]["missing_attempt_count"] == 2
    assert summary["fields"]["total_tokens"]["missing_attempt_count"] == 1
    assert (
        "input_tokens: 已取得 12（缺少 1 次用量；實際可能更高）"
        in format_usage_summary(summary)
    )
    assert (
        "output_tokens: 已取得 3（缺少 2 次用量；實際可能更高）"
        in format_usage_summary(summary)
    )


def test_questions_sum_question_clarification_and_retry_without_result_double_count() -> (
    None
):
    question = {
        "turns": [
            {
                "kind": "question",
                "attempts": [
                    _attempt(
                        {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24}
                    )
                ],
                # turn.result 只是 attempt.result 的投影，不能再加一次。
                "result": {
                    "usage": {
                        "input_tokens": 200,
                        "output_tokens": 40,
                        "total_tokens": 240,
                    }
                },
            },
            {
                "kind": "clarification",
                "attempts": [_attempt({"input_tokens": 5, "output_tokens": 2})],
                "result": {"usage": {"input_tokens": 500}},
            },
            {
                "kind": "retry",
                "manual_retry": True,
                "attempts": [
                    _attempt({"input_tokens": 3, "output_tokens": 1, "total_tokens": 4})
                ],
                "result": {"usage": {"input_tokens": 300}},
            },
        ],
        # 尚未送出的 pending turn 不算一筆缺失用量。
        "pending_turn": {"attempts": []},
    }

    summary = summarize_question(question)
    assert summary["attempt_count"] == 3
    assert summary["observed_totals"] == {
        "input_tokens": 28,
        "output_tokens": 7,
        "total_tokens": 28,
    }
    assert summary["fields"]["total_tokens"]["missing_attempt_count"] == 1
    assert summary["token_totals"]["input_tokens"] == 28
    assert summary["token_totals"]["total_tokens"] is None
    assert (
        summarize_questions([question])["observed_totals"] == summary["observed_totals"]
    )


def test_empty_and_all_missing_attempts_are_not_reported_as_zero_usage() -> None:
    no_requests = summarize_attempts([])
    missing_requests = summarize_attempts([_attempt(None), _attempt({})])

    assert "尚無已送出的模型請求" in format_usage_summary(no_requests)
    assert missing_requests["observed_totals"]["input_tokens"] is None
    assert missing_requests["token_totals"]["input_tokens"] is None
    assert format_usage_summary(missing_requests) == (
        "尚未取得模型實際用量（2 次請求用量缺失）"
    )


def test_totals_are_not_inferred_from_input_and_output_fields() -> None:
    summary = summarize_attempts([_attempt({"input_tokens": 20, "output_tokens": 5})])

    assert summary["observed_totals"]["input_tokens"] == 20
    assert summary["observed_totals"]["output_tokens"] == 5
    assert summary["observed_totals"]["total_tokens"] is None
    assert summary["fields"]["total_tokens"]["missing_attempt_count"] == 1
