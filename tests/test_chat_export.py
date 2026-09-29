"""驗證單一 Open WebUI 對話的安全離線 HTML 匯出。"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from badminton_ai.server.plotly_rich import (
    PLOTLY_SPEC_VERSION,
    render_plotly_charts_html,
    validate_plotly_charts,
)
from scripts.export_chat_html import (
    ChatExportError,
    build_chat_export_html,
    main,
)


def _chart(title: str) -> dict:
    return {
        "title": title,
        "figure": {
            "data": [{"type": "bar", "x": ["A", "B"], "y": [2, 1]}],
            "layout": {},
        },
    }


def _embed(*charts: dict) -> str:
    validated = validate_plotly_charts(
        {"schema_version": PLOTLY_SPEC_VERSION, "charts": list(charts)}
    )
    return render_plotly_charts_html(validated)


def _stored_embed(*charts: dict) -> str:
    payload = json.dumps(
        {"charts": list(charts)}, ensure_ascii=False, separators=(",", ":")
    )
    return f'<script id="plotly-figure-data" type="application/json">{payload}</script>'


def _chat(*, embed: str | None = None) -> dict:
    answer = {
        "id": "a1",
        "parentId": "u1",
        "role": "assistant",
        "content": "資料結果：2 筆。",
    }
    if embed is not None:
        answer["embeds"] = [embed]
    return {
        "chat": {
            "history": {
                "currentId": "a1",
                "messages": {
                    "u1": {
                        "id": "u1",
                        "parentId": None,
                        "role": "user",
                        "content": "請作圖。",
                    },
                    "a1": answer,
                },
            }
        }
    }


def test_export_keeps_active_branch_and_multiple_charts(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.export_chat_html._plotly_javascript", lambda: "/* bundled */"
    )
    payload = _chat(embed=_embed(_chart("球種"), _chart("落點")))
    payload["chat"]["history"]["messages"]["a2"] = {
        "id": "a2",
        "parentId": "u1",
        "role": "assistant",
        "content": "另一個不在目前分支的答案",
    }

    document, messages, charts, warnings = build_chat_export_html(payload)

    assert (messages, charts, warnings) == (2, 2, 0)
    assert document.index("請作圖") < document.index("資料結果")
    assert document.index("球種") < document.index("落點")
    assert "另一個不在目前分支的答案" not in document
    assert document.count('class="chart-card"') == 2
    assert "127.0.0.1:8000" not in document
    assert "connect-src 'none'" in document


def test_q3_shape_markdown_and_plotly_chart_are_both_preserved(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.export_chat_html._plotly_javascript", lambda: "/* bundled */"
    )
    payload = _chat(embed=_embed(_chart("Q3 測試長條圖")))
    payload["chat"]["history"]["messages"]["a1"]["content"] = (
        "資料摘要 **加粗**，並保留 Markdown。"
    )

    document, messages, charts, warnings = build_chat_export_html(payload, theme="auto")

    assert (messages, charts, warnings) == (2, 1, 0)
    assert "資料摘要 <strong>加粗</strong>" in document
    assert "Q3 測試長條圖" in document
    assert "Plotly.newPlot" in document
    assert '<html lang="zh-Hant" data-theme="auto">' in document


def test_non_native_figure_requires_explicit_structural_validation_opt_in(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "scripts.export_chat_html._plotly_javascript", lambda: "/* bundled */"
    )
    figure = {
        "data": [
            {
                "type": "bar",
                "x": ["A"],
                "y": [2],
                "future_trace_option": True,
            }
        ],
        "layout": {},
    }
    payload = _chat(embed=_stored_embed({"title": "已保存圖表", "figure": figure}))

    strict_document, _, strict_charts, strict_warnings = build_chat_export_html(payload)
    preview_document, _, preview_charts, preview_warnings = build_chat_export_html(
        payload, native_validation=False
    )

    assert (strict_charts, strict_warnings) == (0, 1)
    assert "future_trace_option" not in strict_document
    assert (preview_charts, preview_warnings) == (1, 0)
    assert 'class="chart-card"' in preview_document
    assert "future_trace_option" in preview_document
    assert "Plotly.newPlot" in preview_document


def test_structural_validation_still_rejects_remote_or_malicious_embed() -> None:
    figure = {
        "data": [{"type": "bar", "x": ["A"], "y": [2]}],
        "layout": {"images": [{"source": "https://attacker.example/image.png"}]},
    }
    payload = _chat(embed=_stored_embed({"title": "不安全圖表", "figure": figure}))

    document, _, charts, warnings = build_chat_export_html(
        payload, native_validation=False
    )

    assert (charts, warnings) == (0, 1)
    assert "https://attacker.example" not in document
    assert 'id="chat-chart-data"' not in document
    assert "不接受外部 URL" in document


def test_explicit_plotly_provider_works_without_local_plotly(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.export_chat_html._plotly_javascript",
        lambda: pytest.fail("provider 不應回退到本機 Plotly"),
    )
    payload = _chat(embed=_embed(_chart("固定資產圖表")))
    bundle = "/*! plotly.js v3.4.0 */ window.Plotly = {};"

    document, _, charts, warnings = build_chat_export_html(
        payload,
        native_validation=False,
        plotly_javascript_provider=lambda: bundle,
    )

    assert (charts, warnings) == (1, 0)
    assert bundle in document
    assert 'id="chat-chart-data"' in document


def test_explicit_plotly_provider_rejects_script_termination() -> None:
    payload = _chat(embed=_embed(_chart("不應輸出")))

    with pytest.raises(ChatExportError, match="script 終止標記"):
        build_chat_export_html(
            payload,
            native_validation=False,
            plotly_javascript_provider=lambda: "</script><script>alert(1)</script>",
        )


def test_export_escapes_chat_text_and_ignores_untrusted_embed_script(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "scripts.export_chat_html._plotly_javascript", lambda: "/* bundled */"
    )
    payload = _chat(embed=_embed(_chart("合法圖")) + "<script>alert('evil')</script>")
    payload["chat"]["history"]["messages"]["u1"]["content"] = "<script>evil()</script>"

    document, _, charts, warnings = build_chat_export_html(payload)

    assert (charts, warnings) == (1, 0)
    assert "&lt;script&gt;evil()&lt;/script&gt;" in document
    assert "alert('evil')" not in document


def test_export_renders_markdown_without_running_raw_html() -> None:
    payload = _chat()
    payload["chat"]["history"]["messages"]["a1"]["content"] = (
        "## 統計結果\n\n**5,191 筆**，欄位為 `type`。\n\n"
        "- 網前球\n- 殺球\n\n"
        "| 球種 | 次數 |\n|---|---:|\n| 網前球 | 1,033 |\n\n"
        "```python\nprint('<script>')\n```\n\n"
        "<script>alert('bad')</script>\n\n"
        "[不安全連結](javascript:alert(1))"
    )

    document, messages, charts, warnings = build_chat_export_html(payload)

    assert (messages, charts, warnings) == (2, 0, 0)
    assert "<h2>統計結果</h2>" in document
    assert "<strong>5,191 筆</strong>" in document
    assert "<code>type</code>" in document
    assert "<li>網前球</li>" in document
    assert "<table>" in document
    assert '<td style="text-align:right">1,033</td>' in document
    assert '<pre><code class="language-python">' in document
    assert "&lt;script&gt;alert" in document
    assert "<script>alert('bad')</script>" not in document
    assert 'href="javascript:' not in document


def test_export_renders_safe_markdown_link_and_line_break() -> None:
    payload = _chat()
    payload["chat"]["history"]["messages"]["a1"]["content"] = (
        "第一行\n第二行\n\n[資料來源](https://example.org/summary)"
    )

    document, _, _, _ = build_chat_export_html(payload)

    assert "第一行<br" in document
    assert '<a href="https://example.org/summary">資料來源</a>' in document


def test_bad_embed_is_visible_as_warning_not_executed() -> None:
    payload = _chat(embed="<script>alert('evil')</script>")

    document, _, charts, warnings = build_chat_export_html(payload)

    assert (charts, warnings) == (0, 1)
    assert "無法匯出" in document
    assert "alert('evil')" not in document


def test_export_rejects_cyclic_active_branch() -> None:
    payload = _chat()
    payload["chat"]["history"]["messages"]["u1"]["parentId"] = "a1"

    with pytest.raises(ChatExportError, match="循環"):
        build_chat_export_html(payload)


def test_export_accepts_native_single_chat_array(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.export_chat_html._plotly_javascript", lambda: "/* bundled */"
    )
    document, messages, charts, warnings = build_chat_export_html(
        [_chat(embed=_embed(_chart("原生 JSON")))]
    )

    assert (messages, charts, warnings) == (2, 1, 0)
    assert "原生 JSON" in document


def test_export_rejects_multiple_native_chats() -> None:
    with pytest.raises(ChatExportError, match="恰好一筆"):
        build_chat_export_html([_chat(), _chat()])


def test_cli_refuses_to_overwrite_existing_file(tmp_path, capsys) -> None:
    source = tmp_path / "chat.json"
    target = tmp_path / "chat.html"
    source.write_text(json.dumps(_chat(), ensure_ascii=False), encoding="utf-8")
    target.write_text("KEEP", encoding="utf-8")

    assert main([str(source), str(target)]) == 1
    assert target.read_text(encoding="utf-8") == "KEEP"
    assert "匯出失敗" in capsys.readouterr().err


def test_cli_exports_synthetic_chat_fixture(tmp_path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "chat_export_example.json"
    target = tmp_path / "conversation.html"

    assert main([str(fixture), str(target)]) == 0
    document = target.read_text(encoding="utf-8")
    assert "請畫出測試資料的長條圖" in document
    assert "測試長條圖" in document
    assert "Plotly.newPlot" in document
    assert "127.0.0.1:8000" not in document
    assert '<html lang="zh-Hant" data-theme="light">' in document


def test_cli_accepts_fixed_dark_theme(tmp_path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "chat_export_example.json"
    target = tmp_path / "dark.html"

    assert main([str(fixture), str(target), "--theme", "dark"]) == 0
    assert '<html lang="zh-Hant" data-theme="dark">' in target.read_text(
        encoding="utf-8"
    )


def test_actual_plotly_bundle_is_inline_and_version_pinned() -> None:
    document, _, charts, warnings = build_chat_export_html(
        _chat(embed=_embed(_chart("離線圖")))
    )

    assert (charts, warnings) == (1, 0)
    assert "Plotly.newPlot" in document
    assert "<script src=" not in document
    assert "127.0.0.1:8000" not in document


def test_exported_charts_follow_light_and_dark_theme(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.export_chat_html._plotly_javascript", lambda: "/* bundled */"
    )
    document, _, charts, warnings = build_chat_export_html(
        _chat(embed=_embed(_chart("主題圖"))), theme="auto"
    )

    assert (charts, warnings) == (1, 0)
    assert '<html lang="zh-Hant" data-theme="auto">' in document
    assert 'matchMedia("(prefers-color-scheme: light)")' in document
    assert 'colorScheme.addEventListener("change"' in document
    assert "Plotly.relayout(target, themeUpdate(axes, theme))" in document
    assert 'layout.paper_bgcolor = "rgba(0,0,0,0)"' in document


@pytest.mark.parametrize("theme", ["light", "dark", "auto"])
def test_export_theme_option_applies_to_page_and_charts(monkeypatch, theme) -> None:
    monkeypatch.setattr(
        "scripts.export_chat_html._plotly_javascript", lambda: "/* bundled */"
    )
    document, _, _, _ = build_chat_export_html(
        _chat(embed=_embed(_chart("一致配色"))), theme=theme
    )

    assert f'data-theme="{theme}"' in document
    assert "const themeMode = document.documentElement.dataset.theme" in document
    assert ':root[data-theme="dark"]' in document


def test_export_defaults_to_fixed_light_theme(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.export_chat_html._plotly_javascript", lambda: "/* bundled */"
    )
    document, _, _, _ = build_chat_export_html(_chat(embed=_embed(_chart("淺色"))))

    assert '<html lang="zh-Hant" data-theme="light">' in document


def test_export_renderer_javascript_has_valid_syntax(monkeypatch) -> None:
    if shutil.which("node") is None:
        pytest.skip("此環境沒有 Node.js")
    monkeypatch.setattr(
        "scripts.export_chat_html._plotly_javascript", lambda: "/* bundled */"
    )
    document, _, _, _ = build_chat_export_html(_chat(embed=_embed(_chart("語法圖"))))
    renderer = re.findall(r"<script>(.*?)</script>", document, flags=re.DOTALL)[-1]

    result = subprocess.run(
        ["node", "--check"], input=renderer, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
