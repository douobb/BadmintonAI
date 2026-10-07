"""TASK-030 評測核心的假 Open WebUI client 回歸測試。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.evaluation_runner import (
    EvaluationError,
    EvaluationRunner,
    TurnResult,
    manual_retry_succeeded,
    parse_numbered_questions,
)


def test_manual_retry_preserves_failed_request_and_usage(tmp_path: Path) -> None:
    failed = TurnResult(
        error={"message": "失敗", "retryable": False},
        usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
    )
    success = TurnResult(
        messages=[{"role": "assistant", "content": "完成"}],
        usage={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
    )
    client = FakeOpenWebUI(plans={"1": [failed, success]})
    runner = _runner(client, tmp_path, questions="1: 原題\n", max_attempts=1)
    runner.run_pending()
    before = runner.snapshot()["questions"][0]
    assert before["status"] == "failed"
    after = runner.retry_failed("1")["questions"][0]
    assert after["status"] == "completed"
    assert manual_retry_succeeded(after)
    assert after["turns"][0] == before["turns"][0]
    assert client.sends[0][1] == client.sends[1][1] == "原題"
    assert after["usage_totals"]["total_tokens"] == 9
    summary = runner.summary()
    assert summary["manual_retry_succeeded_count"] == 1
    assert summary["token_observed_totals"] == {
        "input_tokens": 6,
        "output_tokens": 3,
        "total_tokens": 9,
    }
    assert summary["token_coverage"]["total_tokens"]["complete"] is True
    assert summary["questions"][0]["usage_attempt_count"] == 2
    with pytest.raises(EvaluationError):
        runner.retry_failed("1")


def test_manual_retry_failure_never_automatically_resends(tmp_path: Path) -> None:
    failed = TurnResult(error={"message": "失敗", "retryable": False})
    again = TurnResult(error={"message": "仍失敗", "retryable": True})
    client = FakeOpenWebUI(plans={"1": [failed, again]})
    runner = _runner(client, tmp_path, questions="1: 原題\n", max_attempts=2)
    runner.run_pending()
    question = runner.retry_failed("1")["questions"][0]
    assert question["status"] == "failed"
    assert len(client.sends) == 2
    assert question["manual_retry_count"] == 1
    assert not manual_retry_succeeded(question)


def test_manual_retry_unknown_native_task_is_not_resent(tmp_path: Path) -> None:
    client = FakeOpenWebUI(plans={"1": [ValueError("失敗")]})
    runner = _runner(client, tmp_path, questions="1: 原題\n", max_attempts=1)
    runner.run_pending()
    client.recover_errors.append(ConnectionError("仍在執行或狀態未知"))
    with pytest.raises(EvaluationError, match="未知或仍執行"):
        runner.retry_failed("1")
    assert len(client.sends) == 1
    assert runner.snapshot()["questions"][0]["status"] == "failed"


def test_manual_retry_projection_ignores_clarification_and_legacy_retry() -> None:
    for turn in (
        {"kind": "clarification", "result": {}},
        {"kind": "clarification", "manual_retry": True, "result": {}},
        {"kind": "retry", "result": {}},
        {"manual_retry": True, "result": {"awaiting_clarification": True}},
    ):
        assert not manual_retry_succeeded({"status": "completed", "turns": [turn]})


class SimulatedProcessCrash(BaseException):
    """模擬 API 已完成、runner 尚未取得回應時程序中斷。"""


class FakeOpenWebUI:
    """支援 idempotency 與讀回回合的記憶體假 API。"""

    def __init__(
        self,
        plans: dict[str, list[Any]] | None = None,
        recover_errors: list[Exception] | None = None,
    ) -> None:
        self.plans = plans or {}
        self.recover_errors = recover_errors or []
        self.conversations: dict[tuple[str, str], str] = {}
        self.conversation_questions: dict[str, str] = {}
        self.responses: dict[tuple[str, str], TurnResult] = {}
        self.creates: list[tuple[str, str, str]] = []
        self.folder_creates: list[tuple[str, str, str, str, int]] = []
        self.folder_recovers: list[tuple[str, str, str, str, int]] = []
        self.sends: list[tuple[str, str, str]] = []

    def create_conversation(
        self,
        run_id: str,
        question_id: str,
        idempotency_key: str,
        model_snapshot: Any,
    ) -> str:
        del model_snapshot
        key = (run_id, idempotency_key)
        if key not in self.conversations:
            conversation_id = f"chat-{question_id}"
            self.conversations[key] = conversation_id
            self.conversation_questions[conversation_id] = question_id
            self.creates.append((run_id, question_id, idempotency_key))
        return self.conversations[key]

    def create_conversation_in_folder(
        self,
        run_id: str,
        question_id: str,
        idempotency_key: str,
        model_snapshot: Any,
        *,
        created_at: str,
        question_count: int,
    ) -> str:
        self.folder_creates.append(
            (run_id, question_id, idempotency_key, created_at, question_count)
        )
        return self.create_conversation(
            run_id, question_id, idempotency_key, model_snapshot
        )

    def recover_conversation(
        self, run_id: str, question_id: str, idempotency_key: str
    ) -> str | None:
        del question_id
        return self.conversations.get((run_id, idempotency_key))

    def recover_conversation_in_folder(
        self,
        run_id: str,
        question_id: str,
        idempotency_key: str,
        *,
        created_at: str,
        question_count: int,
    ) -> str | None:
        self.folder_recovers.append(
            (run_id, question_id, idempotency_key, created_at, question_count)
        )
        return self.recover_conversation(run_id, question_id, idempotency_key)

    def send_turn(
        self, conversation_id: str, content: str, operation_id: str
    ) -> TurnResult:
        key = (conversation_id, operation_id)
        self.sends.append((conversation_id, content, operation_id))
        if key in self.responses:
            return self.responses[key]
        question_id = self.conversation_questions[conversation_id]
        queue = self.plans.get(question_id, [])
        response = queue.pop(0) if queue else self._success(content)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response = response(content)
        if isinstance(response, SimulatedProcessCrash):
            self.responses[key] = self._success(content)
            raise response
        if response == "crash_after_commit":
            self.responses[key] = self._success(content)
            raise SimulatedProcessCrash()
        if response == "timeout_after_commit":
            self.responses[key] = self._success(content)
            raise TimeoutError("response timed out after remote completion")
        self.responses[key] = response
        return response

    def recover_turn(
        self, conversation_id: str, operation_id: str
    ) -> TurnResult | None:
        if self.recover_errors:
            raise self.recover_errors.pop(0)
        return self.responses.get((conversation_id, operation_id))

    @staticmethod
    def _success(content: str, usage: dict[str, int] | None = None) -> TurnResult:
        return TurnResult(
            messages=[
                {"role": "user", "content": content},
                {"role": "assistant", "content": "已完成分析"},
            ],
            tool_calls=[{"name": "runPythonAnalysis", "result": {"rows": 3}}],
            charts=[{"id": "chart-1", "mime_type": "application/vnd.plotly.v1+json"}],
            usage=usage,
        )


def _write_questions(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _runner(
    client: FakeOpenWebUI,
    tmp_path: Path,
    *,
    questions: str = "1: 原始題目？\n2: 第二題原文。\n",
    max_attempts: int = 2,
) -> EvaluationRunner:
    question_file = _write_questions(tmp_path / "questions.txt", questions)
    return EvaluationRunner.start(
        client,
        question_file=question_file,
        store_path=tmp_path / "run.json",
        model_snapshot={"model": "badmintonai", "version": "model-v1"},
        data_snapshot={"sha256": "dataset-v1"},
        max_attempts=max_attempts,
    )


def test_parser_preserves_original_question_text_and_rejects_invalid_lines() -> None:
    questions = parse_numbered_questions("1:  原文含雙空格？  \r\n2: 第二題!\r\n")

    assert [question.prompt for question in questions] == [
        " 原文含雙空格？  ",
        "第二題!",
    ]
    assert [question.source_line for question in questions] == [1, 2]
    with pytest.raises(EvaluationError, match="格式錯誤"):
        parse_numbered_questions("1. 題目格式錯誤")
    with pytest.raises(EvaluationError, match="題號重複"):
        parse_numbered_questions("1: 第一題\n1: 第二題")


def test_selected_questions_keep_full_source_manifest_and_original_order(
    tmp_path: Path,
) -> None:
    source = _write_questions(
        tmp_path / "questions.txt",
        "8: 第八題原文。\n\n1:  第一題原文？  \n5: 第五題原文!\n",
    )
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    client = FakeOpenWebUI()
    store_path = tmp_path / "selected-run.json"

    runner = EvaluationRunner.start(
        client,
        question_file=source,
        store_path=store_path,
        model_snapshot={"model": "badmintonai", "version": "model-v1"},
        data_snapshot={"sha256": "dataset-v1"},
        selected_question_ids=["5", "8"],
    )
    initial = runner.snapshot()
    state = runner.run_pending()

    assert initial["manifest"]["question_source"] == {
        "name": "questions.txt",
        "sha256": original_hash,
        "question_count": 3,
        "selected_question_ids": ["8", "5"],
    }
    assert [item["id"] for item in initial["questions"]] == ["8", "5"]
    assert [item["source_line"] for item in initial["questions"]] == [1, 4]
    assert [item["prompt"] for item in initial["questions"]] == [
        "第八題原文。",
        "第五題原文!",
    ]
    assert state["manifest"]["question_source"]["sha256"] == original_hash
    assert [item[1] for item in client.sends] == ["第八題原文。", "第五題原文!"]

    resumed = EvaluationRunner.resume(client, store_path=store_path)
    resumed.run_pending()
    assert len(client.sends) == 2


@pytest.mark.parametrize(
    ("selected_question_ids", "message"),
    [
        ([], "至少選取一題"),
        (["1", "1"], "不可重複"),
        (["99"], "不存在"),
        ([1], "格式無效"),
    ],
)
def test_runner_rejects_invalid_question_selection_before_checkpoint(
    tmp_path: Path,
    selected_question_ids: list[Any],
    message: str,
) -> None:
    source = _write_questions(
        tmp_path / "questions.txt", "1: 原始題目？\n2: 第二題原文。\n"
    )
    store_path = tmp_path / "run.json"
    client = FakeOpenWebUI()

    with pytest.raises(EvaluationError, match=message):
        EvaluationRunner.start(
            client,
            question_file=source,
            store_path=store_path,
            model_snapshot={"model": "badmintonai", "version": "model-v1"},
            data_snapshot={"sha256": "dataset-v1"},
            selected_question_ids=selected_question_ids,
        )

    assert not store_path.exists()
    assert client.creates == []


def test_each_question_gets_its_own_chat_and_persists_full_result(
    tmp_path: Path,
) -> None:
    client = FakeOpenWebUI()
    runner = _runner(client, tmp_path)

    state = runner.run_pending()

    questions = state["questions"]
    assert state["conversation_organization"] == "openwebui_native_folders_v1"
    assert [item[1] for item in client.folder_creates] == ["1", "2"]
    assert all(item[4] == 2 for item in client.folder_creates)
    assert [question["status"] for question in questions] == ["completed"] * 2
    assert [question["conversation_id"] for question in questions] == [
        "chat-1",
        "chat-2",
    ]
    assert [call[1] for call in client.sends] == ["原始題目？", "第二題原文。"]
    assert questions[0]["turns"][0]["request"] == "原始題目？"
    assert questions[0]["turns"][0]["attempts"][0]["result"]["tool_calls"] == [
        {"name": "runPythonAnalysis", "result": {"rows": 3}}
    ]
    assert questions[0]["turns"][0]["attempts"][0]["result"]["charts"][0]["id"] == (
        "chart-1"
    )
    assert questions[0]["usage_totals"] == {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
    }


def test_legacy_run_checkpoint_keeps_legacy_conversation_creation(
    tmp_path: Path,
) -> None:
    client = FakeOpenWebUI()
    _runner(client, tmp_path, questions="1: 舊 run 題目\n")
    checkpoint_path = tmp_path / "run.json"
    legacy_state = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    legacy_state.pop("conversation_organization")
    checkpoint_path.write_text(
        json.dumps(legacy_state, ensure_ascii=False), encoding="utf-8"
    )

    resumed = EvaluationRunner.resume(client, store_path=checkpoint_path)
    state = resumed.run_pending()

    assert state["questions"][0]["status"] == "completed"
    assert client.creates == [
        (state["run_id"], "1", f"{state['run_id']}:1:conversation")
    ]
    assert client.folder_creates == []
    assert client.folder_recovers == []


def test_transient_timeout_is_recorded_and_retried(tmp_path: Path) -> None:
    client = FakeOpenWebUI(
        {"1": [TimeoutError("upstream timeout"), FakeOpenWebUI._success("原始題目？")]}
    )
    runner = _runner(client, tmp_path, questions="1: 原始題目？\n")

    state = runner.run_pending()
    question = state["questions"][0]

    assert question["status"] == "completed"
    assert question["retry_count"] == 1
    assert question["errors"][0]["type"] == "TimeoutError"
    assert len(question["turns"][0]["attempts"]) == 2
    assert question["turns"][0]["attempts"][0]["error"]["retryable"] is True


def test_retryable_response_usage_is_summed_only_from_actual_fields(
    tmp_path: Path,
) -> None:
    first = TurnResult(
        usage={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
        error={"code": "temporary", "message": "暫時錯誤", "retryable": True},
    )
    second = FakeOpenWebUI._success(
        "原始題目？", {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}
    )
    runner = _runner(
        FakeOpenWebUI({"1": [first, second]}),
        tmp_path,
        questions="1: 原始題目？\n",
    )

    question = runner.run_pending()["questions"][0]

    assert question["retry_count"] == 1
    assert question["usage_totals"] == {
        "input_tokens": 9,
        "output_tokens": 5,
        "total_tokens": 14,
    }
    assert runner.summary()["token_totals"] == question["usage_totals"]


@pytest.mark.parametrize("output_field", ["messages", "tool_calls", "charts"])
def test_retryable_error_with_partial_output_is_not_automatically_retried(
    tmp_path: Path, output_field: str
) -> None:
    partial_output = {output_field: [{"partial": True}]}
    result = TurnResult(
        **partial_output,
        error={"code": "temporary", "message": "結果不完整", "retryable": True},
    )
    client = FakeOpenWebUI({"1": [result, FakeOpenWebUI._success("原始題目？")]})
    runner = _runner(client, tmp_path, questions="1: 原始題目？\n")

    question = runner.run_pending()["questions"][0]

    assert question["status"] == "failed"
    assert len(client.sends) == 1
    saved_result = question["turns"][0]["attempts"][0]["result"]
    assert saved_result[output_field] == [{"partial": True}]


def test_nonretryable_failure_is_saved_and_credentials_are_redacted(
    tmp_path: Path,
) -> None:
    client = FakeOpenWebUI(
        {"1": [RuntimeError("Authorization: Bearer abc.secret-token failed")]}
    )
    runner = _runner(client, tmp_path, questions="1: 原始題目？\n")

    state = runner.run_pending()
    record = (tmp_path / "run.json").read_text(encoding="utf-8")

    assert state["questions"][0]["status"] == "failed"
    assert "abc.secret-token" not in record
    assert "[REDACTED]" in record


def test_crash_after_remote_completion_recovers_without_resending(
    tmp_path: Path,
) -> None:
    client = FakeOpenWebUI(
        {"1": ["crash_after_commit"]},
        recover_errors=[ConnectionError("recovery endpoint unavailable")],
    )
    question_file = _write_questions(tmp_path / "questions.txt", "1: 原始題目？\n")
    store_path = tmp_path / "run.json"
    original = EvaluationRunner.start(
        client,
        question_file=question_file,
        store_path=store_path,
        model_snapshot={"version": "model-v1"},
        data_snapshot={"version": "data-v1"},
    )

    with pytest.raises(SimulatedProcessCrash):
        original.run_pending()

    resumed = EvaluationRunner.resume(client, store_path=store_path)
    state = resumed.run_pending()
    question = state["questions"][0]

    assert question["status"] == "running"
    assert question["pending_turn"] is not None
    assert question["errors"][-1]["stage"] == "recover_turn"
    assert len(client.sends) == 1

    state = resumed.run_pending()

    assert state["questions"][0]["status"] == "completed"
    assert len(client.sends) == 1
    assert state["questions"][0]["turns"][0]["attempts"][0]["recovered"] is True
    assert state["questions"][0]["turns"][0]["attempts"][0]["duration_ms"] is None


def test_timeout_followed_by_recovery_query_error_never_resends_turn(
    tmp_path: Path,
) -> None:
    client = FakeOpenWebUI(
        {"1": ["timeout_after_commit"]},
        recover_errors=[ConnectionError("recovery endpoint unavailable")],
    )
    runner = _runner(client, tmp_path, questions="1: 原始題目？\n")

    state = runner.run_pending()
    question = state["questions"][0]

    assert question["status"] == "running"
    assert question["pending_turn"] is not None
    assert len(client.sends) == 1
    assert question["errors"][-1]["stage"] == "recover_turn"

    state = runner.run_pending()

    assert state["questions"][0]["status"] == "completed"
    assert len(client.sends) == 1


def test_resume_skips_completed_questions(tmp_path: Path) -> None:
    client = FakeOpenWebUI()
    runner = _runner(client, tmp_path)
    runner.run_pending()
    count_before_resume = len(client.sends)

    resumed = EvaluationRunner.resume(client, store_path=tmp_path / "run.json")
    state = resumed.run_pending()

    assert len(client.sends) == count_before_resume
    assert [question["status"] for question in state["questions"]] == [
        "completed",
        "completed",
    ]


def test_stale_runner_instance_refreshes_state_before_execution(
    tmp_path: Path,
) -> None:
    client = FakeOpenWebUI()
    first = _runner(client, tmp_path)
    second = EvaluationRunner.resume(client, store_path=tmp_path / "run.json")

    first.run_pending()
    second.run_pending()

    assert len(client.sends) == 2
    assert [question["status"] for question in second.snapshot()["questions"]] == [
        "completed",
        "completed",
    ]


def test_stop_request_leaves_remaining_questions_pending(tmp_path: Path) -> None:
    client = FakeOpenWebUI()
    runner = _runner(client, tmp_path)
    state = runner.run_pending(stop_requested=lambda: len(client.sends) >= 1)

    assert [question["status"] for question in state["questions"]] == [
        "completed",
        "pending",
    ]
    assert len(client.sends) == 1


def test_clarification_pauses_only_one_question_and_reuses_its_chat(
    tmp_path: Path,
) -> None:
    clarification_event = {
        "type": "function_call",
        "name": "requestClarification",
        "status": "completed",
        "arguments": json.dumps(
            {"question": "請問您指的是哪一場比賽？", "options": ["第一場", "第二場"]},
            ensure_ascii=False,
        ),
    }
    clarification = TurnResult(
        messages=[
            {
                "role": "assistant",
                "content": "請問您指的是哪一場比賽？",
                "output": [clarification_event],
            }
        ],
        tool_calls=[clarification_event],
        awaiting_clarification=True,
        clarification_signal="event",
        usage=None,
    )
    client = FakeOpenWebUI({"1": [clarification]})
    runner = _runner(client, tmp_path)

    state = runner.run_pending()
    question_one, question_two = state["questions"]
    original_chat_id = question_one["conversation_id"]
    assert question_one["status"] == "awaiting_clarification"
    assert question_two["status"] == "completed"
    assert (
        question_one["turns"][0]["attempts"][0]["result"]["messages"][0]["content"]
        == "請問您指的是哪一場比賽？"
    )

    state = runner.submit_clarification("1", "第一場比賽")
    question_one = state["questions"][0]

    assert question_one["status"] == "completed"
    assert question_one["conversation_id"] == original_chat_id
    assert question_one["turns"][1]["kind"] == "clarification"
    assert question_one["turns"][1]["request"] == "第一場比賽"
    assert client.sends[-1][0] == original_chat_id
    assert len(client.creates) == 2


def test_valid_clarification_waits_without_hiding_same_turn_analysis_error(
    tmp_path: Path,
) -> None:
    clarification_event = {
        "type": "function_call",
        "name": "requestClarification",
        "status": "completed",
        "arguments": json.dumps({"question": "採用哪個比賽範圍？"}),
    }
    error = {
        "message": "Python 分析工具未成功完成；回答需要人工複核",
        "retryable": False,
    }
    result = TurnResult(
        messages=[
            {
                "role": "assistant",
                "content": "請先確認比賽範圍。",
                "output": [clarification_event],
            }
        ],
        tool_calls=[
            {"name": "runPythonAnalysis", "status": "failed"},
            clarification_event,
        ],
        awaiting_clarification=True,
        clarification_signal="event",
        error=error,
    )
    client = FakeOpenWebUI({"1": [result]})
    runner = _runner(client, tmp_path)

    state = runner.run_pending()
    question = state["questions"][0]

    assert question["status"] == "awaiting_clarification"
    assert question["turns"][0]["result"]["error"] == error


def test_start_refuses_to_mix_different_question_or_snapshot_versions(
    tmp_path: Path,
) -> None:
    client = FakeOpenWebUI()
    runner = _runner(client, tmp_path)
    runner.run_pending()
    changed = _write_questions(
        tmp_path / "questions.txt", "1: 被修改的題目\n2: 第二題原文。\n"
    )

    with pytest.raises(EvaluationError, match="不同"):
        EvaluationRunner.start(
            client,
            question_file=changed,
            store_path=tmp_path / "run.json",
            model_snapshot={"model": "badmintonai", "version": "model-v1"},
            data_snapshot={"sha256": "dataset-v1"},
        )

    with pytest.raises(EvaluationError, match="憑證欄位"):
        EvaluationRunner.start(
            client,
            question_file=changed,
            store_path=tmp_path / "another-run.json",
            model_snapshot={"version": "model-v1", "api_key": "must-not-save"},
            data_snapshot={"sha256": "dataset-v1"},
        )


def test_new_run_requires_saved_event_and_preserves_ambiguous_turn_for_review(
    tmp_path: Path,
) -> None:
    text_only = TurnResult(
        messages=[{"role": "assistant", "content": "您指的是哪一場比賽？"}],
        awaiting_clarification=True,
        clarification_signal="text_candidate",
        clarification_review_reason="缺少保存的追問事件",
    )
    failed_event = TurnResult(
        messages=[{"role": "assistant", "content": "工具執行失敗"}],
        clarification_signal="tool_failed",
        clarification_review_reason="追問事件未完成",
    )
    answer_with_suggestion = TurnResult(
        messages=[{"role": "assistant", "content": "結果為 31.8%。需要其他場次嗎？"}],
        tool_calls=[{"name": "runPythonAnalysis", "status": "completed"}],
    )
    client = FakeOpenWebUI(
        {"1": [text_only], "2": [failed_event], "3": [answer_with_suggestion]}
    )
    runner = _runner(
        client,
        tmp_path,
        questions="1: 純文字追問\n2: 追問工具失敗\n3: 已回答附建議問句\n",
    )

    first_state = runner.run_pending()

    assert [item["status"] for item in first_state["questions"]] == [
        "needs_review",
        "needs_review",
        "completed",
    ]
    saved_before_restart = (tmp_path / "run.json").read_bytes()
    resumed = EvaluationRunner.resume(client, store_path=tmp_path / "run.json")
    second_state = resumed.run_pending()

    assert [item["status"] for item in second_state["questions"]] == [
        "needs_review",
        "needs_review",
        "completed",
    ]
    assert (tmp_path / "run.json").read_bytes() == saved_before_restart
    assert len(client.sends) == 3


def test_legacy_checkpoint_without_event_policy_is_not_migrated_on_resume(
    tmp_path: Path,
) -> None:
    client = FakeOpenWebUI()
    runner = _runner(client, tmp_path, questions="1: 舊題目\n")
    runner.run_pending()
    path = tmp_path / "run.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state.pop("clarification_policy_version")
    state["questions"][0]["turns"][0]["result"]["messages"][-1]["content"] = (
        "結果完成。還要看另一個場次嗎？"
    )
    state["questions"][0]["status"] = "completed"
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    original = path.read_bytes()

    recovered = EvaluationRunner.resume(client, store_path=path)

    assert recovered.snapshot()["questions"][0]["status"] == "completed"
    assert path.read_bytes() == original
