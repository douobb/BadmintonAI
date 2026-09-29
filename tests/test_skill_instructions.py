"""驗證提示詞與唯一分析 Skill 的必要決策及簡潔分工。"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_PROMPT_PATH = PROJECT_ROOT / "prompts" / "badmintonai-system.md"
SKILL_PATH = PROJECT_ROOT / "skills" / "badminton-analysis.md"


def test_one_compact_prompt_and_one_analysis_skill() -> None:
    prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    skill = SKILL_PATH.read_text(encoding="utf-8")
    assert sorted(path.name for path in SYSTEM_PROMPT_PATH.parent.glob("*.md")) == [
        "badmintonai-system.md"
    ]
    assert sorted(path.name for path in SKILL_PATH.parent.glob("*.md")) == [
        "badminton-analysis.md"
    ]
    assert len(prompt) < 800
    assert len(skill) < 2350
    for text in (prompt, skill):
        assert "TASK-" not in text
        assert "chart_spec.json" not in text


def test_prompt_keeps_clarification_and_source_routing() -> None:
    prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    assert "必要時查分析 Skill、核准 Knowledge 或資料／欄位摘要" in prompt
    assert "欄位探查不用 Python" in prompt
    assert "確認前不要執行 `runPythonAnalysis`" in prompt
    assert "使用者定義優先" in prompt
    assert "可操作核准定義直接用" in prompt
    assert "題內示例優先" in prompt
    assert "術語門檻只在會改變結論且無核准定義時才澄清" in prompt
    assert "合理假設並明示" in prompt
    assert "未指定場次時預設目前資料集的全部可用場次" in prompt
    assert "才呼叫一次 `requestClarification`" in prompt
    assert "未要求資料驗證的策略建議直接給一般非個人化建議" in prompt
    assert "不反問是否改做分析" in prompt
    assert "明確要求資料依據時按前述規則核對、分析，資料不足則說明限制" in prompt
    assert "本機統計使用資料工具" in prompt
    assert "最新賽事、排名、賽程" in prompt
    assert "搜尋不能替代資料欄位或計算口徑的確認" in prompt
    assert "搜尋不可用或無可靠來源時明說無法核實" in prompt


def test_prompt_and_skill_derive_metrics_before_clarifying() -> None:
    prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    skill = SKILL_PATH.read_text(encoding="utf-8")
    for text in (prompt, skill):
        assert "澄清前先查核准欄位能否推導常規指標" in text
        assert "揭露公式與單位" in text
        assert "未校準座標不稱公尺" in text
        assert text.index("能否推導常規指標") < text.index(
            "才呼叫一次 `requestClarification`"
        )


def test_prompt_and_skill_batch_one_analysis_goal_without_call_cap() -> None:
    prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    skill = SKILL_PATH.read_text(encoding="utf-8")
    for text in (prompt, skill):
        assert "同一分析目標盡量一次完成計算與必要 artifacts" in text
        assert "需依前次結果調整才再呼叫" in text
    assert "零樣本不估計或判高低；不放寬確認條件湊非零或重算" in prompt
    assert "零樣本不估計或判高低，不放寬條件湊非零" in skill
    assert "最多 3 次修正" in skill


def test_requested_metric_is_not_substituted_with_all_losses() -> None:
    prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    skill = SKILL_PATH.read_text(encoding="utf-8")
    assert "不以相近指標替代" in prompt
    assert "不得以近似指標替代題意" in skill
    assert "失誤率按核准口徑計自身終局失誤" in prompt
    assert "失誤次數／率依核准口徑計自身終局失誤" in skill
    assert "失誤次數同理，未標記原因的失分不當失誤" in prompt
    assert "未標記原因仍屬失分分母，但不算已確認失誤" in skill
    assert "所有失分回合" in prompt and "所有失分回合代替" in skill
    assert "比例明示分子分母" in prompt
    assert "明示分母" in skill


def test_skill_clarifies_ambiguous_terms_without_overasking() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    assert skill.index("先由題目與前文辨識球員") < skill.index("使用者定義優先")
    assert "確認前不執行 `runPythonAnalysis`" in skill
    assert "後場」採區碼 1–4" in skill
    assert "檢索片段未顯示區碼時查看來源全文" in skill
    assert "四角拉吊" in skill
    assert "會改變結論的口徑且無核准定義時澄清" in skill
    assert "題內示例優先（如「最後 3 拍」），不自創門檻" in skill
    assert "proxy 不證實真實戰術意圖" in skill
    assert "合理假設並明示" in skill
    assert "未指定範圍採目前快照全部可用場次" in skill
    assert "呼叫一次 `requestClarification`" in skill
    assert skill.index("細節採合理假設並明示") < skill.index(
        "口徑缺漏會改結論且無合理預設才呼叫一次 `requestClarification`"
    )
    assert skill.index("使用者定義優先") < skill.index("策略建議給一般非個人化答案")
    assert "要求資料時按前述規則核對並分析" in skill
    assert "狹窄欄位查詢未命中不能斷言資料沒有該欄位" in skill
    assert "搜尋不能自動決定分析欄位或門檻" in skill
    assert "使用者要求不搜尋時遵守" in skill


def test_skill_preserves_data_semantics() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    for rule in (
        "一列代表一筆擊球事件",
        "不把逐拍列數當回合數",
        "核對 `df.columns`、schema 與欄位定義",
        '`getpoint_player == ""` 是非終局事件的空字串標記',
        "`isna()`／`dropna()` 不會移除它",
        "終局欄可命名 `getpoint_player_result`",
        "每回合末列非空",
        "`getpoint_player_result == P` 判勝",
        "不可用原事件 `getpoint_player` 判勝",
        "`player == P` 且 `type == T`",
        "含對手失誤送分",
        "P 失分例：先取 P 參與回合鍵",
        "不可先篩終局 `player == P`",
        "未標記原因仍屬失分分母",
        "不可先篩資料再 `shift`",
        "有效樣本",
        "player_location_area",
        "landing_area",
        "未定義落點 33",
        "場內熱區只畫 1–24 的 6×4 網格，出界另列統計",
        "先驗證代碼型別",
        "先用 `resolve_player()` 取得資料中的正式名稱",
        "回合長度以完整回合的事件列數計算",
        "數值排序與比較先 `pd.to_numeric`",
        "題目已指明空間對象時不反問另一種位置",
        "33 不從總樣本消失",
    ):
        assert rule in skill


def test_skill_joins_participating_rallies_before_counting_losses() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    loss_steps = (
        "先取 P 參與回合鍵",
        "join 完整終局表",
        "終局欄非空且 `getpoint_player_result != P`",
    )
    positions = [skill.index(step) for step in loss_steps]
    assert positions == sorted(positions)
    assert "不可先篩終局 `player == P`" in skill
    assert "繪圖前核對失分回合數與原因類別回合數總和相等" in skill


def test_skill_preserves_one_pass_chart_and_failure_rules() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    assert "它不是 REPL，每次呼叫都須產生至少一個 artifact" in skill
    assert "print 不算產物" in skill
    assert "不要先用 print 探查再呼叫分析" in skill
    assert "純資料概況優先用摘要／欄位工具" in skill
    assert "已知欄位與口徑時不為了例行確認而列出全部欄位或場次" in skill
    assert "在單次分析中輸出簡短摘要及所需互動 Plotly 圖" in skill
    assert "多類別比較、趨勢或空間分布預設一張圖，無助理解才省略" in skill
    assert "不為湊圖另呼叫工具" in skill
    assert "需逐項精確數值時可輔以精簡表格" in skill
    assert "不把不同單位硬塞同一座標軸" in skill
    assert "聊天圖表不用 Matplotlib/PNG" in skill
    assert "以最後一次成功回應為準" in skill
    assert "`rich_ui_status=embedded` 或 `duplicate_suppressed` 表示圖已呈現" in skill
    assert "狀態不明時不要盲目重畫" in skill
    assert "不要另寫 `plotly_charts.json` 的 Markdown 圖片連結" in skill
    assert "最多 3 次修正" in skill
    assert "502／503／504 屬基礎設施錯誤，不重試" in skill
    assert (
        "`analysis_retry_limit`、`analysis_message_limit` 或 `analysis_terminated`"
        in skill
    )
    assert "sandbox 未安裝 SciPy" in skill
    assert "預載 pandas/numpy 或標準函式庫" in skill
    assert "不要臆測顯著性" in skill
    assert "全範圍勝負分析可用球員摘要交叉核對，但摘要不代替實際分析" in skill


def test_prompt_prefers_useful_charts_and_result_first_answer() -> None:
    prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    skill = SKILL_PATH.read_text(encoding="utf-8")
    assert (
        "多類別比較、趨勢或空間分布預設一張互動圖，無助理解才省略；單值不強制" in prompt
    )
    assert "先說主要發現與意義，再呈圖表；口徑及限制簡述於後" in prompt
    assert "先給可驗證結果及主要意義" in skill
    assert "方法細節按需展開" in skill
