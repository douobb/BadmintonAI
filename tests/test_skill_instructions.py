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
    assert len(skill) < 3300
    for text in (prompt, skill):
        assert "TASK-" not in text
        assert "chart_spec.json" not in text


def test_prompt_keeps_clarification_and_source_routing() -> None:
    prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    assert "必要時查分析 Skill、核准 Knowledge 或資料摘要" in prompt
    assert "可先用 Python 探查" in prompt
    assert "未確認前不猜數值" in prompt
    assert "使用者定義優先" in prompt
    assert "採核准口徑" in prompt
    assert "題內示例" in prompt
    assert "必要條件缺漏會改變結論且無合理預設時" in prompt
    assert "合理假設須明示" in prompt
    assert "未指定場次預設目前快照全部可用場次" in prompt
    assert "才呼叫一次 `requestClarification`" in prompt
    assert "策略建議直接給一般非個人化建議" in prompt
    assert "不反問是否改做分析" in prompt
    assert "明確要求資料依據則按前述規則核對分析" in prompt
    assert "`runPythonAnalysis`" in prompt
    assert "時效性公開資訊" in prompt
    assert "欄位口徑仍以核准資料為準" in prompt
    assert "無可靠來源明說無法核實" in prompt


def test_prompt_and_skill_derive_metrics_before_clarifying() -> None:
    prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    skill = SKILL_PATH.read_text(encoding="utf-8")
    for text in (prompt, skill):
        assert "能否推導常規指標" in text
        assert "公式" in text and "單位" in text
        assert "未校準座標不稱公尺" in text
        assert text.index("能否推導常規指標") < text.index(
            "才呼叫一次 `requestClarification`"
        )


def test_prompt_and_skill_batch_one_analysis_goal_without_call_cap() -> None:
    prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    skill = SKILL_PATH.read_text(encoding="utf-8")
    for text in (prompt, skill):
        assert "`runPythonAnalysis`" in text
        assert "renderAnalysisChart" in text
    assert "零樣本" in prompt and "勝率不可計" in prompt
    assert "需依結果改統計才重跑" in skill
    assert all(
        term in skill for term in ("零樣本", "符合回合 0", "勝率不可計", "不改定義")
    )
    assert "最多 3 次修正" in skill
    assert "以已保存的 `result_id` 呼叫 `renderAnalysisChart`" in prompt


def test_requested_metric_is_not_substituted_with_all_losses() -> None:
    prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    skill = SKILL_PATH.read_text(encoding="utf-8")
    assert "不以近似指標替代" in prompt
    assert "不得以近似指標替代" in skill
    assert "失誤依自身終局失誤口徑" in prompt
    assert "失誤次數／率依核准口徑計自身終局失誤" in skill
    assert "未標記原因不推定為失誤" in prompt
    assert "未標記原因仍屬失分分母，但不算已確認失誤" in skill
    assert "所有失分回合代替" in skill
    assert "公式、單位與分子分母" in prompt
    assert "明示分母" in skill


def test_skill_clarifies_ambiguous_terms_without_overasking() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    assert skill.index("先由題目與前文辨識球員") < skill.index("使用者定義優先")
    assert "澄清前可用 Python 探查" in skill
    assert "後場" in skill and "區碼 1–4" in skill
    assert "檢索片段缺定義時查全文" in skill
    assert "四角拉吊" in skill
    assert "會改結論且無核准定義時才澄清" in skill
    assert "題內示例優先，不自創門檻" in skill
    assert "proxy 不證實戰術意圖" in skill
    assert "合理假設並明示" in skill
    assert "未指定範圍採目前快照全部可用場次" in skill
    assert "呼叫一次 `requestClarification`" in skill
    assert skill.index("細節採合理假設並明示") < skill.index(
        "口徑缺漏會改結論且無合理預設才呼叫一次 `requestClarification`"
    )
    assert skill.index("使用者定義優先") < skill.index("策略建議給一般非個人化答案")
    assert "要求資料時按前述規則核對並分析" in skill
    assert "窄查未命中不證明欄位不存在" in skill
    assert "搜尋釋義" in skill and "不決定欄位或門檻" in skill
    assert "使用者要求不搜尋時遵守" in skill


def test_skill_preserves_data_semantics() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    for rule in (
        "一列代表一筆擊球事件",
        "不把逐拍列數當回合數",
        "下一拍直接終局與回合最終得分不可互代",
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
        "hit_area",
        "landing_area",
        "對手位置",
        "前場 17–24、中場 5–16、後場 1–4",
        "聚合或合併後依出錯物件核對欄位",
        "未定義落點 33",
        "場內熱區只畫 1–24 的 6×4 網格，出界另列統計",
        "先驗證代碼型別",
        "先用 `resolve_player()` 取得資料中的正式名稱",
        "回合長度以完整回合的事件列數計算",
        "數值排序或轉整數先 `pd.to_numeric(errors='coerce')`",
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
    assert "print-only stdout 可探查" in skill
    assert "無 `result_id`、不可繪圖" in skill
    assert "正式分析須保存 JSON／CSV／JSONL" in skill
    assert "合計最多 4 KiB 預覽（標記截斷）" in skill
    assert "純概況優先摘要／欄位工具" in skill
    assert "renderAnalysisChart(result_id, code)" in skill
    assert "多類別、趨勢、空間分布通常一圖，單值不強制" in skill
    assert "只輸出 `plotly_charts.json`" in skill
    assert "render 失敗只修圖不重算" in skill
    assert "同訊息不重複成功請求，狀態不明不盲重畫" in skill
    assert "最多 3 次修正" in skill
    assert "502／503／504 屬基礎設施錯誤，不重試" in skill
    assert (
        "`analysis_retry_limit`、`analysis_message_limit` 或 `analysis_terminated`"
        in skill
    )
    assert "SciPy 未裝" in skill
    assert "預載 `pd`、`np`" in skill
    assert "勿臆測顯著性" in skill
    assert "全範圍勝負分析可用球員摘要交叉核對，但摘要不代替實際分析" in skill


def test_prompt_prefers_useful_charts_and_result_first_answer() -> None:
    prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    assert "多類別、趨勢、空間分布通常配一圖，單值不強制" in prompt
    assert "先說主要發現，再簡述口徑限制" in prompt


def test_proxy_question_and_options_share_a_complete_plain_language_definition() -> (
    None
):
    skill = SKILL_PATH.read_text(encoding="utf-8")
    prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    proxy_rule = next(line for line in skill.splitlines() if line.startswith("- 澄清"))
    for concepts in (
        ("question", "共用", "範圍", "球種", "分母"),
        ("options", "白話", "必要定義", "差異", "直接分析"),
        ("1–2", "實質不同", "自行定義", "合併"),
        ("最貼題", "資料支持", "額外假設少", "先列"),
        ("僅首項", "建議", "理由", "不預選"),
        ("共用標示", "替代定義", "非核准", "意圖", "因果"),
        ("確認前", "探索", "無合理 proxy", "限制", "可回答方向"),
        ("選完", "不再問球種"),
    ):
        assert all(concept in proxy_rule for concept in concepts)
    assert all(
        concept in prompt
        for concept in ("共用口徑", "白話短選項", "確認後", "無合理 proxy")
    )


def test_clarification_example_keeps_player_direction_and_distinct_success_choices() -> (
    None
):
    skill = SKILL_PATH.read_text(encoding="utf-8")
    example = next(line for line in skill.splitlines() if line.startswith("- 例："))
    question, options = example.split("options", 1)
    assert all(
        concept in question
        for concept in ("周天成反手回擊後", "對手", "以殺球代表攻擊", "對手下一拍殺球")
    )
    assert "直接得分" in options and "最後贏下回合" in options
    assert "自行指定" in options and "成功定義" in options
    assert options.count("建議") == 1
    assert all(
        field not in example
        for field in ("hit_area", "getpoint_player", "type ==", "÷")
    )


def test_explicit_definition_and_zero_samples_do_not_trigger_redefinition() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    zero_rule = next(line for line in skill.splitlines() if "符合回合 0" in line)
    assert all(
        concept in zero_rule
        for concept in ("明確定義", "零樣本", "勝率不可計", "不改定義", "追問湊樣本")
    )
    assert all(
        concept in zero_rule for concept in ("替代分析", "可選後續", "不阻擋原題")
    )
    assert skill.index("使用者定義優先") < skill.index("- 澄清")
    assert "核准口徑直接用" in skill
    assert all(
        concept in skill
        for concept in ("欄位語義不明", "metadata", "Knowledge", "不請使用者猜 schema")
    )
    assert all(
        concept in prompt
        for concept in ("零樣本", "0", "勝率不可計", "不改口徑追問湊樣本")
    )


def test_skill_distinguishes_own_hit_sequence_and_segmented_reads() -> None:
    import pandas as pd

    skill = SKILL_PATH.read_text(encoding="utf-8")
    sequence_rule = next(
        line for line in skill.splitlines() if line.startswith("- 時序")
    )
    assert all(
        term in sequence_rule
        for term in (
            "完整事件",
            "同回合",
            "該球員序列",
            "hit_area",
            "允許對手拍",
            "不要求全體球序差 1",
            "不可互代",
            "回合鍵",
        )
    )
    assert all(
        term in skill
        for term in (
            "next_offset_bytes",
            "offset_bytes",
            "has_more",
            "片段不是完整 JSON",
            "read 成功不代表先前錯誤已解決",
            "不能憑零值宣稱驗證成功",
        )
    )
    # 依 Skill 的自身序列口徑，中間有對手事件仍是相鄰兩次自身擊球。
    events = pd.DataFrame(
        [
            {"rally": 1, "ball_round": "3.0", "player": "P", "hit_area": "21.0"},
            {"rally": 1, "ball_round": "2.0", "player": "O", "hit_area": "14.0"},
            {"rally": 1, "ball_round": "1.0", "player": "P", "hit_area": "1.0"},
            {"rally": 2, "ball_round": "1.0", "player": "P", "hit_area": "4.0"},
        ]
    )
    ordered = events.assign(
        ball_round=pd.to_numeric(events.ball_round),
        hit_area=pd.to_numeric(events.hit_area),
    ).sort_values(["rally", "ball_round"])
    own = ordered.loc[ordered.player == "P"]
    previous = own.groupby("rally").hit_area.shift()
    qualifying = (
        own.hit_area.isin([1, 4, 21, 24])
        & previous.isin([1, 4, 21, 24])
        & (own.hit_area != previous)
    )
    assert qualifying.sum() == 1
