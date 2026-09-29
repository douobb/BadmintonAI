"""評測報告 HTML 的 offline、圖表、nullable usage 與注入安全測試。"""

from __future__ import annotations

import builtins
import re
import shutil
import subprocess
from typing import Any

import pytest

from badminton_ai.server.plotly_rich import (
    PLOTLY_SPEC_VERSION,
    render_plotly_charts_html,
    validate_plotly_charts,
)
from scripts.evaluation_report_html import (
    REPORT_CSP,
    EvaluationReportError,
    build_evaluation_report_html,
    fetch_plotly_javascript,
)

PLOTLY_JS = "/*! plotly.js v3.4.0 */ window.Plotly = {};"


def _chart_embed(title: str = "球種分布") -> str:
    validated = validate_plotly_charts(
        {
            "schema_version": PLOTLY_SPEC_VERSION,
            "charts": [
                {
                    "title": title,
                    "figure": {
                        "data": [{"type": "bar", "x": ["A", "B"], "y": [2, 1]}],
                        "layout": {},
                    },
                }
            ],
        },
        native_validation=False,
    )
    return render_plotly_charts_html(validated)


def _state(questions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": "a" * 32,
        "created_at": "2026-09-27T00:00:00Z",
        "updated_at": "2026-09-27T00:00:10Z",
        "manifest": {
            "question_source": {"name": "questions.txt", "sha256": "b" * 64},
            "model_snapshot": {
                "model_id": "badmintonai",
                "model_name": "BadmintonAI",
                "updated_at": "2026-09-26T12:00:00Z",
                "tool_ids": ["server:badminton-ai"],
            },
            "data_snapshot": {
                "tool_server_version": "1.2.3",
                "snapshot_id": "sha256:dataset-v1",
                "snapshot_version": "dataset-v1",
                "source": "dataset.json",
                "row_count": 120,
            },
        },
        "questions": questions,
    }


def _question(
    question_id: str,
    prompt: str,
    turns: list[dict[str, Any]],
    *,
    status: str = "completed",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": question_id,
        "prompt": prompt,
        "status": status,
        "elapsed_ms": 1234,
        "processing_elapsed_ms": 900,
        "user_wait_ms": 334,
        "usage_totals": usage,
        "turns": turns,
        "pending_turn": None,
    }


def _turn(
    request: str,
    answer: str,
    *,
    kind: str = "question",
    embeds: list[str] | None = None,
    usage: dict[str, int] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    charts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    assistant: dict[str, Any] = {"role": "assistant", "content": answer}
    if embeds is not None:
        assistant["embeds"] = embeds
    result = {
        "messages": [{"role": "user", "content": request}, assistant],
        "usage": usage,
        "tool_calls": tool_calls or [],
        "charts": charts or [],
    }
    return {
        "kind": kind,
        "request": request,
        "duration_ms": 900,
        "attempts": [{"duration_ms": 900, "result": result}],
        "result": result,
    }


def test_q1_without_chart_says_none_was_generated_and_keeps_null_usage() -> None:
    state = _state(
        [
            _question(
                "1",
                "原始 Q1 問題？",
                [_turn("原始 Q1 問題？", "這是 Q1 回答。", usage=None)],
                usage={
                    "input_tokens": None,
                    "output_tokens": None,
                    "total_tokens": None,
                },
            )
        ]
    )
    document, chart_count = build_evaluation_report_html(
        state, plotly_js_provider=lambda: pytest.fail("沒有圖表時不應下載 JS")
    )

    assert chart_count == 0
    assert "此題未產生圖表。" in document
    assert "未提供（null）" in document
    assert "input_tokens=未提供（null）" in document
    assert "此嘗試耗時" in document
    assert "Q1 回答" in document
    assert "data_snapshot" in document
    assert "sha256:dataset-v1" in document


def test_q3_embed_is_safely_rebuilt_offline_and_printable() -> None:
    raw_embed = _chart_embed()
    state = _state(
        [
            _question(
                "3",
                "畫出球種分布。",
                [
                    _turn(
                        "畫出球種分布。",
                        "**已產生球種分布。**\n\n![圖](attachment:plotly_charts.json)\n\n[資料說明](https://example.invalid/data)",
                        embeds=[raw_embed],
                        usage={
                            "input_tokens": 12,
                            "output_tokens": 34,
                            "total_tokens": 46,
                        },
                        tool_calls=[{"name": "runPythonAnalysis"}],
                        charts=[{"fingerprint": "saved-chart"}],
                    )
                ],
                usage={"input_tokens": 12, "output_tokens": 34, "total_tokens": 46},
            )
        ]
    )
    document, chart_count = build_evaluation_report_html(
        state, plotly_js_provider=lambda: PLOTLY_JS
    )

    assert chart_count == 1
    assert 'id="chart-0"' in document
    assert document.count('class="chart" id="chart-') == 1
    assert "<strong>已產生球種分布。</strong>" in document
    assert "![圖]" not in document
    assert "attachment:plotly_charts.json" not in document
    assert "<img" not in document
    assert '<a href="https://example.invalid/data"' not in document
    assert '<span class="markdown-link">資料說明</span>' in document
    assert "球種分布" in document
    assert "runPythonAnalysis" not in document
    assert "12" in document and "34" in document
    assert "connect-src &#x27;none&#x27;" not in document
    assert "connect-src 'none'" in document
    assert "frame-src 'none'" in document
    assert "window.Plotly = {};" in document
    assert '<script src="/assets/plotly-6.6.0.min.js"' not in document
    assert "@media print" in document
    assert ".toolbar { display:none !important; }" in document
    assert ".chart .modebar { display:none !important; }" in document
    assert ".snapshot-full { display:none !important; }" in document
    assert (
        '<details class="snapshot-full"><summary>展開完整快照 JSON（螢幕檢視）'
        in document
    )
    assert "SHA-256" in document and "badmintonai" in document
    assert "sha256:dataset-v1" in document and "1.2.3" in document
    assert ".turn-stats { display:table;" in document
    assert "break-inside:avoid; page-break-inside:avoid;" in document
    print_styles = document.partition("@media print {")[2].partition("</style>")[0]
    assert (
        ".snapshot-compact { grid-template-columns:minmax(0,1fr); gap:1mm; }"
        in print_styles
    )
    assert (
        ".snapshot-card dl > div { grid-template-columns:minmax(35mm,42mm) minmax(0,1fr);"
        in print_styles
    )
    assert (
        ".snapshot-card dd { overflow-wrap:anywhere; word-break:normal; hyphens:auto; }"
        in print_styles
    )
    assert (
        ".chart-card { break-inside:avoid; page-break-inside:avoid; }" in print_styles
    )
    assert ".chart { min-height:320px; }" in print_styles
    assert ".chart { display:none" not in print_styles
    assert 'setTheme("light")' in document
    assert 'window.addEventListener("beforeprint"' in document
    assert 'window.addEventListener("afterprint"' in document
    assert "window.print()" in document
    assert "本系統不提供直接 PDF API" in document
    assert "plotly.js v3.4.0" in document


def test_saved_embed_validation_does_not_import_python_plotly(monkeypatch) -> None:
    embed = _chart_embed()
    state = _state(
        [
            _question(
                "3",
                "畫圖？",
                [_turn("畫圖？", "已有保存圖表。", embeds=[embed])],
            )
        ]
    )
    original_import = builtins.__import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "plotly" or name.startswith("plotly."):
            raise AssertionError("報告匯出不可依賴 Python Plotly")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    document, chart_count = build_evaluation_report_html(
        state, plotly_js_provider=lambda: PLOTLY_JS
    )

    assert chart_count == 1
    assert 'id="chart-0"' in document


def test_usage_aliases_are_reported_without_filling_missing_fields() -> None:
    state = _state(
        [
            _question(
                "1",
                "部分 usage？",
                [_turn("部分 usage？", "回答。", usage={"prompt_tokens": 7})],
                usage=None,
            )
        ]
    )
    document, _ = build_evaluation_report_html(state)

    assert "input_tokens=7" in document
    assert "output_tokens=未提供（null）" in document
    assert "total_tokens=未提供（null）" in document


def test_q85_clarification_includes_both_rounds_and_user_answer() -> None:
    state = _state(
        [
            _question(
                "85",
                "從防守轉攻擊得分比例？",
                [
                    _turn(
                        "從防守轉攻擊得分比例？",
                        "請選擇口徑：1. 球種序列 2. 場區序列。",
                    ),
                    _turn(
                        "按球種序列計算，採選項 1。",
                        "採用球種序列，統計結果為 25%。",
                        kind="clarification",
                        usage={
                            "input_tokens": 20,
                            "output_tokens": 10,
                            "total_tokens": 30,
                        },
                    ),
                ],
                usage={"input_tokens": 80, "output_tokens": 40, "total_tokens": 120},
            )
        ]
    )
    document, _ = build_evaluation_report_html(state)

    assert "從防守轉攻擊得分比例？" in document
    assert "請選擇口徑：1. 球種序列 2. 場區序列。" in document
    assert "按球種序列計算，採選項 1。" in document
    assert "採用球種序列，統計結果為 25%。" in document
    assert "第 2 輪補答" in document
    assert "input_tokens=80" in document


def test_report_escapes_untrusted_content_and_never_copies_embed_html() -> None:
    injected_embed = '<script>alert("embed-xss")</script>' + _chart_embed()
    state = _state(
        [
            _question(
                "1",
                '<img src=x onerror="alert(1)">',
                [
                    _turn(
                        "補答 <svg onload=alert(1)>",
                        '</pre><script>alert("answer-xss")</script>',
                        embeds=[injected_embed],
                    )
                ],
            )
        ]
    )
    document, chart_count = build_evaluation_report_html(
        state, plotly_js_provider=lambda: PLOTLY_JS
    )

    assert chart_count == 1
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in document
    assert "&lt;script&gt;alert(&quot;answer-xss&quot;)&lt;/script&gt;" in document
    assert "embed-xss" not in document
    assert "onerror=alert" not in document
    assert "<img src=x" not in document
    assert "<svg onload" not in document


def test_markdown_formatting_is_safe_and_attachment_images_are_not_rendered() -> None:
    state = _state(
        [
            _question(
                "1",
                "格式安全測試。",
                [
                    _turn(
                        "格式安全測試。",
                        "**粗體**\n\n![圖](attachment:plotly_charts.json)",
                    )
                ],
            )
        ]
    )

    document, chart_count = build_evaluation_report_html(state)

    assert chart_count == 0
    assert "<strong>粗體</strong>" in document
    assert "attachment:plotly_charts.json" not in document
    assert "![圖]" not in document
    assert "<img" not in document
    assert "此題未產生圖表。" in document


def test_invalid_embed_is_not_executed_or_counted_as_a_chart() -> None:
    state = _state(
        [
            _question(
                "1",
                "不安全圖表？",
                [
                    _turn(
                        "不安全圖表？",
                        "無圖。",
                        embeds=['<script>alert("no")</script>'],
                    )
                ],
            )
        ]
    )
    document, chart_count = build_evaluation_report_html(
        state, plotly_js_provider=lambda: pytest.fail("拒絕的 embed 不應載入 JS")
    )

    assert chart_count == 0
    assert 'alert("no")' not in document
    assert "未通過安全檢查" in document
    assert "圖表 embed" in document


def test_report_rejects_invalid_run_id_and_bad_plotly_bundle() -> None:
    with pytest.raises(EvaluationReportError, match="run ID"):
        build_evaluation_report_html({"run_id": "../bad", "questions": []})
    with pytest.raises(EvaluationReportError, match="資產驗證失敗"):
        build_evaluation_report_html(
            _state(
                [
                    _question(
                        "3",
                        "圖表？",
                        [_turn("圖表？", "圖表", embeds=[_chart_embed()])],
                    )
                ]
            ),
            plotly_js_provider=lambda: "<script>alert(1)</script>",
        )


def test_plotly_asset_fetch_is_fixed_internal_get_and_checks_version(
    monkeypatch,
) -> None:
    class _Headers:
        @staticmethod
        def get_content_type() -> str:
            return "application/javascript"

    class _Response:
        headers = _Headers()

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        @staticmethod
        def read(_limit: int) -> bytes:
            return PLOTLY_JS.encode("utf-8")

    class _Opener:
        @staticmethod
        def open(request: Any, *, timeout: int) -> _Response:
            assert (
                request.full_url == "http://tool-server:8000/assets/plotly-6.6.0.min.js"
            )
            assert request.get_method() == "GET"
            assert timeout >= 30
            return _Response()

    monkeypatch.setenv(
        "BADMINTON_AI_EVALUATION_TOOL_SERVER_URL", "http://tool-server:8000"
    )
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: _Opener())
    assert fetch_plotly_javascript() == PLOTLY_JS

    monkeypatch.setenv(
        "BADMINTON_AI_EVALUATION_TOOL_SERVER_URL", "http://attacker.example"
    )
    with pytest.raises(EvaluationReportError, match="固定內部 Tool Server"):
        fetch_plotly_javascript()


def test_report_csp_is_offline_and_print_styles_are_explicit() -> None:
    document, _ = build_evaluation_report_html(_state([]))

    assert REPORT_CSP.startswith("default-src 'none'")
    assert "connect-src 'none'" in REPORT_CSP
    assert "frame-src 'none'" in REPORT_CSP
    assert "object-src 'none'" in REPORT_CSP
    assert 'http-equiv="Content-Security-Policy"' in document
    assert "@media print" in document
    assert ':root[data-theme="dark"]' in document
    assert "color-scheme:light !important" in document
    assert "列印／另存 PDF" in document


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js 未安裝")
def test_generated_chart_renderer_script_passes_node_syntax_check() -> None:
    state = _state(
        [
            _question(
                "3",
                "語法檢查圖表？",
                [_turn("語法檢查圖表？", "已完成。", embeds=[_chart_embed()])],
            )
        ]
    )
    document, _ = build_evaluation_report_html(
        state, plotly_js_provider=lambda: PLOTLY_JS
    )
    scripts = re.findall(r"<script>(.*?)</script>", document, flags=re.DOTALL)
    assert scripts
    checked = subprocess.run(
        ["node", "-e", "new Function(process.argv[1])", scripts[-1]],
        check=False,
        capture_output=True,
        text=True,
    )

    assert checked.returncode == 0, checked.stderr
