"use strict";

(() => {
  const apiBase = "/badmintonai/evaluation/api";
  const form = document.querySelector("#source-form");
  const previewBox = document.querySelector("#preview-box");
  const previewList = document.querySelector("#preview-list");
  const selectedCount = document.querySelector("#selected-count");
  const selectionMessage = document.querySelector("#selection-message");
  const selectAllButton = document.querySelector("#select-all-button");
  const clearSelectionButton = document.querySelector("#clear-selection-button");
  const rangeInput = document.querySelector("#range-input");
  const applyRangeButton = document.querySelector("#apply-range-button");
  const questionMatrix = document.querySelector("#question-matrix");
  const clarificationBox = document.querySelector("#clarification-list");
  const selectedRunNotice = document.querySelector("#selected-run-notice");
  const runSummary = document.querySelector("#run-summary");
  const statusText = document.querySelector("#run-status");
  const notice = document.querySelector("#notice");
  const sourceFile = document.querySelector("#question-file");
  const previewButton = document.querySelector("#preview-button");
  const startButton = document.querySelector("#start-button");
  const confirmRun = document.querySelector("#confirm-run");
  const stopButton = document.querySelector("#stop-button");
  const resumeButton = document.querySelector("#resume-button");
  const downloadBox = document.querySelector("#downloads");
  const runsList = document.querySelector("#recent-runs");
  const detailDialog = document.querySelector("#question-detail-dialog");
  const detailTitle = document.querySelector("#question-detail-title");
  const detailContent = document.querySelector("#question-detail-content");
  const closeDetailButton = document.querySelector("#close-question-dialog");
  const pageTitle = document.querySelector("#page-title");
  const pageLede = document.querySelector("#page-lede");
  let preview = null;
  let refreshInProgress = false;
  const retryRequests = new Set();
  let canStart = false;
  let startInProgress = false;
  let detailReturnFocus = null;
  let activeDetail = null;
  let detailConversationFrame = null;
  let renderedRunId = null;
  const requestedRunId = /(?:^|[?&])run_id=([a-f0-9]{32})(?:&|$)/.exec(
    window.location.search || "",
  )?.[1];
  const selectedRunId = requestedRunId || null;

  const activePage = window.location.pathname === "/badmintonai/evaluation/new"
    ? "new"
    : window.location.pathname === "/badmintonai/evaluation/history"
      ? "history"
      : "progress";
  const pageCopy = {
    progress: ["評測工作台", "查看目前 run 的逐題進度與狀態。"],
    new: ["預覽與選題", "預覽原題，選取本輪要執行的題目。"],
    history: ["近期紀錄", "回看近期 run 與逐題複核註記。"],
  }[activePage];
  pageTitle.textContent = pageCopy[0];
  pageLede.textContent = pageCopy[1];
  document.title = `${pageCopy[0]} | BadmintonAI`;
  for (const view of document.querySelectorAll("[data-page-view]")) {
    view.hidden = view.dataset.pageView !== activePage;
  }
  for (const link of document.querySelectorAll("[data-page-link]")) {
    if (link.dataset.pageLink === activePage) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  }

  const statusNames = {
    idle: "尚無評測紀錄",
    running: "執行中",
    stopping: "停止中",
    stopped: "已停止，可續跑",
    paused: "待續跑",
    queued: "排隊中",
    awaiting_clarification: "待補答",
    needs_review: "需複核",
    completed: "已完成",
    failed: "失敗",
    // 下列狀態描述 run，不納入題目六分類。
    pending: "待執行",
    finished_with_errors: "已結束，含失敗題",
  };
  const questionStatuses = [
    "queued",
    "running",
    "awaiting_clarification",
    "completed",
    "failed",
    "needs_review",
  ];
  const shortStatusNames = {
    queued: "排隊中",
    running: "執行中",
    awaiting_clarification: "待補答",
    needs_review: "複核",
    completed: "完成",
    failed: "失敗",
    unknown: "未知",
  };

  function setNotice(message, isError = false) {
    notice.textContent = message;
    notice.classList.toggle("error", isError);
  }

  async function api(path, options = {}) {
    const response = await fetch(`${apiBase}${path}`, {
      credentials: "same-origin",
      cache: "no-store",
      ...options,
    });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json")
      ? await response.json()
      : null;
    if (!response.ok) {
      const message = payload && typeof payload.detail === "string"
        ? payload.detail
        : `請求失敗（HTTP ${response.status}）`;
      throw new Error(message);
    }
    return payload;
  }

  function createText(tag, text, className) {
    const element = document.createElement(tag);
    element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  function selectedSource() {
    const selected = form.querySelector('input[name="source"]:checked');
    return selected ? selected.value : "";
  }

  function selectedQuestionIds() {
    return Array.from(
      previewList.querySelectorAll('input[name="question-selection"]:checked'),
    ).map((checkbox) => checkbox.value);
  }

  function updateSelectionState(message = "") {
    const selectedIds = selectedQuestionIds();
    const hasPreview = Boolean(preview);
    selectedCount.textContent = hasPreview
      ? `已選 ${selectedIds.length} / ${preview.question_count} 題`
      : "尚未預覽題目";
    for (const control of [selectAllButton, clearSelectionButton, rangeInput, applyRangeButton]) {
      control.disabled = !hasPreview;
    }
    selectionMessage.textContent = message || (
      hasPreview && selectedIds.length === 0 ? "至少選取一題才能開始評測。" : ""
    );
    startButton.disabled = !(
      hasPreview && selectedIds.length > 0 && confirmRun.checked && canStart
        && !startInProgress
    );
  }

  function applyQuestionRange() {
    const match = /^(\d+)\s*-\s*(\d+)$/.exec(rangeInput.value.trim());
    if (!match) {
      selectionMessage.textContent = "請輸入有效範圍，例如 1-10。";
      return;
    }
    const first = Number(match[1]);
    const last = Number(match[2]);
    if (!Number.isSafeInteger(first) || !Number.isSafeInteger(last) || first < 1 || first > last) {
      selectionMessage.textContent = "範圍需為正整數，且起始題號不可大於結束題號。";
      return;
    }
    const checkboxes = Array.from(
      previewList.querySelectorAll('input[name="question-selection"]'),
    );
    const inRange = checkboxes.filter((checkbox) => {
      const questionId = Number(checkbox.value);
      return Number.isSafeInteger(questionId) && questionId >= first && questionId <= last;
    });
    if (!inRange.length) {
      selectionMessage.textContent = "預覽題目中沒有符合此範圍的題號。";
      return;
    }
    for (const checkbox of checkboxes) {
      checkbox.checked = inRange.includes(checkbox);
    }
    updateSelectionState(`已選取題號 ${first}-${last} 範圍內的預覽題目。`);
  }

  function formatMs(value) {
    if (!Number.isInteger(value) || value < 0) return "未提供";
    const seconds = Math.floor(value / 1000);
    const minutes = Math.floor(seconds / 60);
    return `${String(minutes).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}.${String(value % 1000).padStart(3, "0")}`;
  }

  function processingElapsedMs(question) {
    if (Number.isInteger(question.processing_elapsed_ms) && question.processing_elapsed_ms >= 0) {
      return question.processing_elapsed_ms;
    }
    if (
      Number.isInteger(question.elapsed_ms)
      && question.elapsed_ms >= 0
      && Number.isInteger(question.user_wait_ms)
      && question.user_wait_ms >= 0
      && question.user_wait_ms <= question.elapsed_ms
    ) {
      return question.elapsed_ms - question.user_wait_ms;
    }
    return null;
  }

  function displayRunStatus(runStatus, questions) {
    if (runStatus !== "completed") return runStatus;
    const hasCompletedFailure = Array.isArray(questions) && questions.some(
      (question) => question && typeof question === "object"
        && question.status === "completed"
        && question.display_status === "needs_review",
    );
    return hasCompletedFailure ? "needs_review" : runStatus;
  }

  function usageText(usage) {
    if (usage && typeof usage === "object" && usage.fields) {
      if (!Number.isInteger(usage.attempt_count) || usage.attempt_count <= 0) {
        return "尚未取得模型實際用量（尚無已送出的模型請求）";
      }
      if (usage.has_usage !== true) {
        return `尚未取得模型實際用量（${usage.attempt_count} 次請求用量缺失）`;
      }
      return ["input_tokens", "output_tokens", "total_tokens"]
        .map((key) => {
          const field = usage.fields[key] || {};
          if (field.complete === true && Number.isInteger(field.known_total)) {
            return `${key}: ${field.known_total}`;
          }
          if (Number.isInteger(field.known_total)) {
            return `${key}: 已取得 ${field.known_total}（缺少 ${field.missing_attempt_count} 次用量；實際可能更高）`;
          }
          return `${key}: 未取得（缺少 ${field.missing_attempt_count} 次用量；實際可能更高）`;
        })
        .join(" · ");
    }
    const keys = ["input_tokens", "output_tokens", "total_tokens"];
    if (!usage || typeof usage !== "object") return "未取得模型實際用量";
    if (!keys.some((key) => Number.isInteger(usage[key]) && usage[key] >= 0)) {
      return "未取得模型實際用量";
    }
    return keys
      .map((key) => `${key}: ${Number.isInteger(usage[key]) ? usage[key] : "未提供"}`)
      .join(" · ");
  }

  function clearConversationPreview() {
    if (detailConversationFrame) detailConversationFrame.removeAttribute("src");
    detailConversationFrame = null;
  }

  function showConversationFallback(frame, help, conversation, answer) {
    if (detailConversationFrame !== frame) return;
    frame.removeAttribute("src");
    frame.remove();
    detailConversationFrame = null;
    help.textContent = answer
      ? "原對話目前無法讀取；原題保留於上方，以下提供安全回答備援。"
      : "原對話目前無法讀取；題目與補答資訊仍保留於上方。";
    if (answer) conversation.append(answer);
  }

  function statusPath(useSelectedRun = true) {
    return useSelectedRun && selectedRunId
      ? `/status?run_id=${encodeURIComponent(selectedRunId)}`
      : "/status";
  }

  async function loadConversationPreview(frame, path, source, help, conversation, answer) {
    let navigationRequested = false;
    frame.addEventListener("load", () => {
      if (!navigationRequested || detailConversationFrame !== frame) return;
      frame.hidden = false;
      help.textContent = "原對話已載入；完整 Markdown 與互動圖表顯示於下方。";
    });
    frame.addEventListener("error", () => {
      showConversationFallback(frame, help, conversation, answer);
    }, { once: true });
    try {
      const response = await fetch(path, {
        credentials: "same-origin",
        cache: "no-store",
      });
      try {
        if (response.body && typeof response.body.cancel === "function") {
          await response.body.cancel();
        }
      } catch {
        // 預覽驗證只需要狀態與標頭；取消本文失敗不影響結果。
      }
      const contentType = response.headers?.get("content-type") || "";
      if (!response.ok || !contentType.toLowerCase().includes("text/html")) {
        throw new Error("原對話預覽無法讀取");
      }
      if (detailConversationFrame !== frame) return;
      navigationRequested = true;
      frame.src = source;
    } catch {
      showConversationFallback(frame, help, conversation, answer);
    }
  }

  function renderPreview(payload) {
    preview = payload;
    confirmRun.checked = false;
    previewList.replaceChildren();
    for (const question of payload.questions || []) {
      const item = document.createElement("li");
      item.className = "preview-choice";
      const label = document.createElement("label");
      label.className = "preview-choice-label";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.name = "question-selection";
      checkbox.value = String(question.id);
      checkbox.checked = true;
      checkbox.setAttribute("aria-label", `選取第 ${question.id} 題`);
      checkbox.addEventListener("change", () => updateSelectionState());
      label.append(
        checkbox,
        createText("strong", `Q${question.id}`),
        createText("span", question.prompt, "preview-prompt"),
      );
      item.append(label);
      previewList.append(item);
    }
    previewBox.hidden = false;
    updateSelectionState("預覽題目預設全選，可使用範圍或逐題勾選調整。");
    setNotice(`已預覽 ${payload.question_count} 題；原題文字未改寫。`);
  }

  function questionState(question) {
    const state = question.display_status || question.status;
    return questionStatuses.includes(state) ? state : "unknown";
  }

  function questionStateName(question) {
    if (question.manual_retry_succeeded === true) return "重試後成功";
    const state = questionState(question);
    return statusNames[state] || "狀態未知";
  }

  function safeRunId(runId) {
    return typeof runId === "string" && /^[a-f0-9]{32}$/.test(runId);
  }

  function appendDetailValue(list, label, value) {
    const term = createText("dt", label);
    const description = createText("dd", value);
    list.append(term, description);
  }

  function createRetryButton(question, runId) {
    if (question.status !== "failed" || !safeRunId(runId)) return null;
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "question-retry secondary";
    retry.textContent = "retry";
    retry.setAttribute("aria-label", `重試 Q${question.id} 最後失敗回合`);
    const key = `${runId}:${question.id}`;
    retry.disabled = retryRequests.has(key) || question.can_retry !== true;
    retry.title = retry.disabled ? "此題已有工作排隊或舊回合尚待復原" : "保留上下文並排入最後失敗回合的人工重試";
    retry.addEventListener("click", async () => {
      if (retryRequests.has(key)) return;
      retryRequests.add(key);
      retry.disabled = true;
      try {
        const result = await api(`/runs/${encodeURIComponent(runId)}/questions/${encodeURIComponent(String(question.id))}/retry`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({expected_retry_count: question.manual_retry_count || 0}),
        });
        setNotice(result?.already_queued
          ? `Q${question.id} 的人工 retry 已在佇列中。`
          : `Q${question.id} 的人工 retry 已排入佇列。`);
      } catch (error) { setNotice(error.message, true); }
      finally { retryRequests.delete(key); await refresh(); }
    });
    return retry;
  }

  function renderQuestion(question, runId, showAnnotationTools = false) {
    const item = document.createElement("article");
    item.className = "question-detail";
    const head = document.createElement("div");
    head.className = "question-head";
    head.append(createText("h3", `Q${question.id}`));
    const prompt = document.createElement("section");
    prompt.className = "detail-block";
    prompt.append(createText("h4", "原始題目"), createText("p", question.prompt || "未提供"));

    const statePill = createText(
      "span",
      questionStateName(question),
      `status-pill status-${questionState(question)}`,
    );
    head.append(statePill);
    const hasClarification = Boolean(question.queued_clarification)
      || question.status === "awaiting_clarification";
    const answer = hasClarification ? null : document.createElement("section");
    if (answer) {
      answer.className = "detail-block";
      answer.append(createText("h4", "回答"));
      answer.append(createText(
        "p",
        question.assistant_text || "沒有安全可顯示的回答文字；請檢視原對話或安全失敗提示。",
        "answer",
      ));
    }

    const metrics = document.createElement("dl");
    metrics.className = "detail-metrics";
    appendDetailValue(metrics, "處理耗時（不含人工等待）", formatMs(processingElapsedMs(question)));
    appendDetailValue(metrics, "Token", usageText(question.usage_summary));
    appendDetailValue(
      metrics,
      "工作類型",
      Array.isArray(question.work_types) && question.work_types.length
        ? question.work_types.join("、")
        : question.resume_required === true
          ? "原題（明確續跑後才排入）"
          : "尚無已執行工作",
    );
    appendDetailValue(metrics, "重試", Number.isInteger(question.retry_count) ? String(question.retry_count) : "未提供");
    appendDetailValue(metrics, "人工 Retry", String(question.manual_retry_count || 0));
    appendDetailValue(metrics, "工具呼叫", Number.isInteger(question.tool_call_count) ? String(question.tool_call_count) : "未提供");
    appendDetailValue(metrics, "錯誤紀錄", Number.isInteger(question.error_count) ? String(question.error_count) : "未提供");

    const conversation = document.createElement("section");
    conversation.className = "detail-block conversation-preview-section";
    conversation.append(createText("h4", "原始對話（唯讀）"));
    const hasConversation = safeRunId(runId)
      && typeof question.conversation_id === "string"
      && /^[A-Za-z0-9_-]{1,128}$/.test(question.conversation_id);
    if (hasConversation) {
      const help = createText(
        "p",
        "正在載入唯讀原對話；完整 Markdown 與互動圖表會顯示於下方。",
        "help",
      );
      const frame = document.createElement("iframe");
      frame.className = "conversation-frame";
      frame.title = `Q${question.id} 原始 Open WebUI 對話（唯讀）`;
      frame.loading = "eager";
      frame.hidden = true;
      frame.setAttribute("title", frame.title);
      frame.setAttribute("loading", "eager");
      frame.setAttribute("sandbox", "allow-scripts");
      frame.setAttribute("referrerpolicy", "no-referrer");
      const conversationPath = `${apiBase}/runs/${encodeURIComponent(runId)}/questions/${encodeURIComponent(String(question.id))}/conversation.html`;
      const conversationSource = Number.isInteger(question.chart_count) && question.chart_count > 0
        ? `${conversationPath}#chart-0`
        : conversationPath;
      detailConversationFrame = frame;
      conversation.append(help, frame);
      const conversationLink = document.createElement("a");
      conversationLink.href = `/c/${encodeURIComponent(question.conversation_id)}`;
      conversationLink.textContent = "開啟原對話";
      conversationLink.target = "_blank";
      conversationLink.rel = "noopener noreferrer";
      conversationLink.className = "conversation-link";
      conversation.append(conversationLink);
      void loadConversationPreview(
        frame,
        conversationPath,
        conversationSource,
        help,
        conversation,
        answer,
      );
    } else {
      conversation.append(createText("p", "此題沒有可供檢視的原始對話。", "help"));
    }
    item.append(head, prompt);
    if (typeof question.clarification_projection_note === "string") {
      item.append(createText("p", question.clarification_projection_note, "help"));
    }
    if (question.queued_clarification) {
      item.append(renderQueuedClarification(question, runId));
    } else if (question.status === "awaiting_clarification") {
      item.append(renderClarificationForm(question, runId));
    } else if (!hasConversation && answer) {
      item.append(answer);
    }
    item.append(metrics);
    if (typeof question.failure_message === "string" && question.failure_message) {
      item.append(createText("p", question.failure_message, "notice error"));
    } else if (Number.isInteger(question.error_count) && question.error_count > 0) {
      item.append(createText(
        "p",
        `另有 ${question.error_count} 筆錯誤紀錄；工作台不顯示 provider 原始錯誤內容。`,
        "notice",
      ));
    }

    item.append(conversation);
    const hasReviewCandidate = question.clarification_review_candidate === true;
    const annotation = question.classification_annotation;
    if (hasReviewCandidate || annotation || showAnnotationTools) {
      const review = document.createElement("details");
      review.className = "classification-review";
      review.open = hasReviewCandidate || Boolean(annotation);
      review.append(createText(
        "summary",
        hasReviewCandidate ? "查看澄清複核提示與註記" : "新增／查看管理員分類註記",
      ));
      if (hasReviewCandidate) {
        review.append(createText(
          "p",
          question.clarification_review_reason
            || `複核線索：${question.clarification_review_source || "舊 run 文字提示"}。此線索不會自動改變題目狀態。`,
        ));
      } else if (!annotation) {
        review.append(createText(
          "p",
          "可先開啟原對話核對，再留下分類與理由。此註記會獨立保存，不改寫 checkpoint。",
        ));
      }
      if (annotation) {
        const labels = {
          missed_clarification: "管理員標註為漏判追問",
          answered_with_followup: "管理員標註為已作答並附建議問句",
          needs_review: "管理員標註為仍需複核",
        };
        review.append(
          createText(
            "p",
            `${labels[annotation.classification] || "管理員分類"} · ${annotation.actor || "管理員未識別"} · ${annotation.recorded_at || "時間未提供"}`,
            "classification-note",
          ),
          createText("p", `原 checkpoint 狀態：${annotation.original_status || question.status}`, "classification-note"),
          createText("p", `理由：${annotation.reason || "未提供"}`, "classification-note"),
        );
      }
      if (showAnnotationTools && safeRunId(runId)) {
        const label = createText("label", "人工複核理由（必填）");
        const reason = document.createElement("textarea");
        reason.required = true;
        reason.maxLength = 1000;
        reason.setAttribute("aria-label", `Q${question.id} 人工複核理由`);
        const actions = document.createElement("div");
        actions.className = "actions";
        for (const [classification, text] of [
          ["missed_clarification", "標註為漏判追問"],
          ["answered_with_followup", "標註為已作答"],
          ["needs_review", "保留待複核"],
        ]) {
          const button = createText("button", text, "secondary");
          button.type = "button";
          button.addEventListener("click", async () => {
            if (!reason.value.trim()) {
              reason.setCustomValidity("請填寫人工複核理由。");
              reason.reportValidity();
              return;
            }
            reason.setCustomValidity("");
            button.disabled = true;
            try {
              const path = `/runs/${encodeURIComponent(runId)}/questions/${encodeURIComponent(String(question.id))}/classification-annotation`;
              const saved = await api(path, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ classification, reason: reason.value.trim() }),
              });
              setNotice(`Q${question.id} 人工分類註記已另存；原始 checkpoint 未改寫。`);
              if (saved && saved.run) {
                const updated = saved.run.questions.find(
                  (item) => String(item.id) === String(question.id),
                );
                if (updated) openQuestionDetails(updated, runId, true, closeDetailButton);
              }
              await refresh();
            } catch (error) {
              setNotice(error.message, true);
              button.disabled = false;
            }
          });
          actions.append(button);
        }
        label.htmlFor = `annotation-reason-${question.id}`;
        reason.id = label.htmlFor;
        review.append(label, reason, actions);
      }
      item.append(review);
    }
    return item;
  }

  function openQuestionDetails(question, runId, showAnnotationTools, sourceElement) {
    clearConversationPreview();
    const alreadyOpen = detailDialog.open;
    if (!alreadyOpen) {
      detailReturnFocus = sourceElement || document.activeElement;
    }
    activeDetail = { questionId: String(question.id), runId };
    detailTitle.textContent = `Q${question.id} · ${questionStateName(question)}`;
    detailContent.replaceChildren(renderQuestion(question, runId, showAnnotationTools));
    if (!alreadyOpen) detailDialog.showModal();
  }

  function renderQuestionMatrix(container, questions, runId, showAnnotationTools) {
    const focusedTile = container.contains(document.activeElement)
      ? document.activeElement.closest(".question-tile")
      : null;
    const focusedQuestionId = focusedTile?.dataset.questionId || null;
    container.replaceChildren();
    if (!Array.isArray(questions) || questions.length === 0) {
      container.append(createText("li", "尚無題目狀態可顯示。", "empty"));
      return;
    }
    questions.forEach((question, index) => {
      const cell = document.createElement("li");
      const state = questionState(question);
      const button = document.createElement("button");
      button.type = "button";
      button.className = `question-tile status-${state}`;
      button.dataset.questionId = String(question.id);
      button.dataset.runId = String(runId || "");
      button.setAttribute("aria-haspopup", "dialog");
      button.setAttribute("aria-controls", "question-detail-dialog");
      button.setAttribute(
        "aria-label",
        `第 ${index + 1} 題，共 ${questions.length} 題：Q${question.id}，${questionStateName(question)}。開啟題目詳情。`,
      );
      button.append(
        createText("span", `Q${question.id}`, "tile-number"),
        createText("span", question.manual_retry_succeeded === true ? "重試後成功" : shortStatusNames[state] || "未知", "tile-status"),
      );
      button.addEventListener("click", () => {
        const canAnnotate = showAnnotationTools
          && safeRunId(runId)
          && !["pending", "running"].includes(question.status);
        openQuestionDetails(question, runId, canAnnotate, button);
      });
      button.addEventListener("keydown", (event) => {
        const deltas = { ArrowLeft: -1, ArrowRight: 1 };
        const computedColumns = window.getComputedStyle(container).gridTemplateColumns;
        const columns = computedColumns && computedColumns !== "none"
          ? computedColumns.split(/\s+/).length
          : Math.max(1, Math.floor(container.clientWidth / 64));
        if (event.key === "ArrowUp") deltas.ArrowUp = -columns;
        if (event.key === "ArrowDown") deltas.ArrowDown = columns;
        if (!Object.hasOwn(deltas, event.key)) return;
        const tiles = Array.from(container.querySelectorAll(".question-tile"));
        const target = tiles[index + deltas[event.key]];
        if (target) {
          event.preventDefault();
          target.focus();
        }
      });
      cell.append(button);
      const retry = createRetryButton(question, runId);
      if (retry) cell.append(retry);
      container.append(cell);
    });
    if (focusedQuestionId) {
      const replacement = Array.from(container.querySelectorAll(".question-tile"))
        .find((tile) => tile.dataset.questionId === focusedQuestionId);
      replacement?.focus({ preventScroll: true });
    }
  }

  function renderClarificationForm(question, runId) {
    const section = document.createElement("section");
    section.className = "clarification-detail";
    section.append(createText("h4", "待補答"));
    const original = createText(
      "p",
      question.assistant_text || "模型未提供文字內容。",
      "clarification-text",
    );
    section.append(original);

    const formElement = document.createElement("form");
    formElement.className = "clarification";
    formElement.setAttribute("aria-label", `補答第 ${question.id} 題`);
    const choices = Array.isArray(question.choices)
      ? question.choices.filter((choice) => typeof choice === "string")
      : [];
    if (choices.length) {
      const fieldset = document.createElement("fieldset");
      fieldset.append(createText("legend", "模型提供的選項（可不選，改用自由輸入）"));
      const choicesBox = document.createElement("div");
      choicesBox.className = "choices";
      choices.slice(0, 12).forEach((choice, index) => {
        const label = document.createElement("label");
        label.className = "choice";
        const radio = document.createElement("input");
        radio.type = "radio";
        radio.name = `choice-${question.id}`;
        radio.value = choice;
        radio.id = `choice-${question.id}-${index}`;
        label.append(radio, createText("span", choice));
        choicesBox.append(label);
      });
      fieldset.append(choicesBox);
      formElement.append(fieldset);
    }
    const label = createText("label", "補答內容（必填，保存後送回同一對話）");
    label.htmlFor = `answer-${question.id}`;
    const textarea = document.createElement("textarea");
    textarea.id = `answer-${question.id}`;
    textarea.name = "answer";
    textarea.required = !choices.length;
    textarea.maxLength = 8000;
    textarea.setAttribute("aria-describedby", `answer-help-${question.id}`);
    const help = createText("span", "若已選上方選項，可留白；自由輸入會優先採用。", "help");
    help.id = `answer-help-${question.id}`;
    const submit = createText("button", "送出補答");
    submit.type = "submit";
    formElement.append(label, textarea, help, submit);
    formElement.addEventListener("submit", async (event) => {
      event.preventDefault();
      const selected = formElement.querySelector(`input[name="choice-${CSS.escape(String(question.id))}"]:checked`);
      const answer = textarea.value.trim() || (selected ? selected.value : "");
      if (!answer) {
        textarea.setCustomValidity("請選擇一個選項或輸入補答內容。");
        textarea.reportValidity();
        return;
      }
      textarea.setCustomValidity("");
      submit.disabled = true;
      try {
        const result = await api("/clarify", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            run_id: safeRunId(runId) ? runId : undefined,
            question_id: String(question.id),
            answer,
          }),
        });
        setNotice(result?.already_queued
          ? `Q${question.id} 補答已在佇列中。`
          : `Q${question.id} 補答已保存並排入佇列。`);
        detailDialog.close();
        await refresh();
      } catch (error) {
        setNotice(error.message, true);
        submit.disabled = false;
      }
    });
    section.append(formElement);
    return section;
  }

  function renderQueuedClarification(question, runId) {
    const queued = question.queued_clarification;
    const isExecuting = queued.status === "executing";
    const section = document.createElement("section");
    section.className = "clarification-detail";
    section.setAttribute("aria-live", "polite");
    section.append(createText("h4", isExecuting ? "補答執行中" : "補答已排隊"));
    section.append(createText(
      "p",
      question.assistant_text || "模型未提供文字內容。",
      "clarification-text",
    ));
    section.append(createText("p", "已保存補答", "help"));
    section.append(createText(
      "p",
      queued.answer || "沒有可顯示的已保存補答。",
      "clarification-text queued-answer",
    ));
    if (isExecuting) {
      section.append(createText("p", "此補答已開始送回原對話，目前無法修改或取消。", "help"));
      return section;
    }

    const formElement = document.createElement("form");
    formElement.className = "clarification queued-clarification-edit";
    formElement.setAttribute("aria-label", `修改第 ${question.id} 題已排隊補答`);
    const label = createText("label", "修改已保存補答");
    const textarea = document.createElement("textarea");
    textarea.name = "answer";
    textarea.required = true;
    textarea.maxLength = 8000;
    textarea.value = queued.answer || "";
    label.htmlFor = `queued-answer-${question.id}`;
    textarea.id = label.htmlFor;
    const actions = document.createElement("div");
    actions.className = "queued-clarification-actions";
    const save = createText("button", "更新補答");
    save.type = "submit";
    const cancel = createText("button", "取消排隊", "secondary");
    cancel.type = "button";
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      try {
        await api(`/clarification-queue/${encodeURIComponent(runId)}/${encodeURIComponent(String(question.id))}`, {
          method: "DELETE",
        });
        setNotice(`Q${question.id} 已取消排隊補答。`);
        detailDialog.close();
        await refresh();
      } catch (error) {
        setNotice(error.message, true);
        cancel.disabled = false;
      }
    });
    actions.append(save, cancel);
    formElement.append(label, textarea, actions);
    formElement.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!textarea.value.trim()) {
        textarea.setCustomValidity("請輸入補答內容。");
        textarea.reportValidity();
        return;
      }
      textarea.setCustomValidity("");
      save.disabled = true;
      cancel.disabled = true;
      try {
        await api(`/clarification-queue/${encodeURIComponent(runId)}/${encodeURIComponent(String(question.id))}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ answer: textarea.value.trim() }),
        });
        setNotice(`Q${question.id} 已更新排隊補答。`);
        detailDialog.close();
        await refresh();
      } catch (error) {
        setNotice(error.message, true);
        save.disabled = false;
        cancel.disabled = false;
      }
    });
    section.append(formElement);
    return section;
  }

  function renderClarifications(questions, runId) {
    const focusedQuestionId = clarificationBox.contains(document.activeElement)
      ? document.activeElement.dataset.questionId
      : null;
    const waiting = questions.filter((question) =>
      question.status === "awaiting_clarification" || question.queued_clarification,
    );
    clarificationBox.replaceChildren();
    if (!waiting.length) {
      clarificationBox.append(createText("li", "目前沒有待補答題目。", "empty"));
      return;
    }
    for (const question of waiting) {
      const item = document.createElement("li");
      item.className = "clarification-queue-item";
      const queueStatus = question.queued_clarification?.status;
      const label = queueStatus === "executing"
        ? "補答執行中"
        : queueStatus === "queued"
          ? "補答已排隊"
          : "待補答";
      const button = createText("button", `Q${question.id} · ${label}`, "secondary");
      button.type = "button";
      button.dataset.questionId = String(question.id);
      button.dataset.runId = String(runId || "");
      button.setAttribute("aria-haspopup", "dialog");
      button.setAttribute("aria-controls", "question-detail-dialog");
      button.setAttribute("aria-label", `Q${question.id} ${label}；開啟題目詳情`);
      button.addEventListener("click", () => openQuestionDetails(question, runId, true, button));
      item.append(button);
      if (question.assistant_text) item.append(createText("p", question.assistant_text, "muted"));
      clarificationBox.append(item);
    }
    if (focusedQuestionId) {
      const replacement = Array.from(clarificationBox.querySelectorAll("button[data-question-id]"))
        .find((button) => button.dataset.questionId === focusedQuestionId);
      replacement?.focus({ preventScroll: true });
    }
  }

  detailDialog.addEventListener("click", (event) => {
    if (event.target !== detailDialog) return;
    const bounds = detailDialog.getBoundingClientRect();
    const outside = event.clientX < bounds.left
      || event.clientX > bounds.right
      || event.clientY < bounds.top
      || event.clientY > bounds.bottom;
    if (outside) detailDialog.close();
  });
  closeDetailButton.addEventListener("click", () => detailDialog.close());
  detailDialog.addEventListener("close", () => {
    clearConversationPreview();
    const returnTarget = detailReturnFocus;
    const context = activeDetail;
    detailReturnFocus = null;
    activeDetail = null;
    if (returnTarget && returnTarget.isConnected) {
      returnTarget.focus();
      return;
    }
    if (!context) return;
    const replacement = Array.from(document.querySelectorAll(".question-tile"))
      .find((tile) => tile.dataset.questionId === context.questionId && tile.dataset.runId === String(context.runId || ""));
    if (replacement) replacement.focus();
  });

  function renderRun(state) {
    const run = state.run;
    const runStatus = displayRunStatus(state.run_status, run?.questions);
    statusText.textContent = statusNames[runStatus] || runStatus;
    canStart = state.can_start === true;
    stopButton.disabled = state.can_stop !== true;
    resumeButton.disabled = !state.can_resume;
    updateSelectionState();
    downloadBox.replaceChildren();
    runSummary.replaceChildren();
    if (!run) {
      renderedRunId = null;
      selectedRunNotice.hidden = true;
      downloadBox.hidden = true;
      renderClarifications([], "");
      renderQuestionMatrix(questionMatrix, [], "", false);
      return;
    }
    const runId = safeRunId(state.run_id) ? state.run_id : (safeRunId(run.run_id) ? run.run_id : "");
    renderedRunId = safeRunId(runId) ? runId : null;
    const activeRunId = safeRunId(state.active_run_id) ? state.active_run_id : runId;
    selectedRunNotice.hidden = !runId || activeRunId === runId;
    if (!selectedRunNotice.hidden) {
      selectedRunNotice.textContent = activeRunId
        ? `目前檢視 run ${runId.slice(0, 8)}…；目前輪次 ${activeRunId.slice(0, 8)}… 保持獨立。`
        : `目前檢視歷史 run ${runId.slice(0, 8)}…。`;
    }
    if (runId) {
      downloadBox.hidden = false;
      for (const [label, suffix, openInTab] of [
        ["下載完整 JSON", "json", false],
        ["下載易讀摘要", "summary.txt", false],
        ["下載離線 HTML 報告", "report.html", false],
        ["下載 PDF 報告", "report.pdf", false],
        ["列印／另存 PDF", "report/print", true],
      ]) {
        const link = document.createElement("a");
        link.href = `${apiBase}/runs/${runId}/${suffix}`;
        link.textContent = label;
        if (openInTab) {
          link.target = "_blank";
          link.rel = "noopener noreferrer";
        } else {
          link.setAttribute("download", "");
        }
        downloadBox.append(link);
      }
      const printNote = document.createElement("p");
      printNote.textContent = "可直接下載 PDF；列印頁仍可在瀏覽器手動調整列印設定。";
      downloadBox.append(printNote);
    }
    const questions = Array.isArray(run.questions) ? run.questions : [];
    const measuredElapsed = questions
      .map((question) => processingElapsedMs(question))
      .filter((value) => Number.isInteger(value) && value >= 0);
    const elapsedSummary = measuredElapsed.length
      ? `${formatMs(measuredElapsed.reduce((total, value) => total + value, 0))}（${measuredElapsed.length}/${questions.length} 題有值）`
      : "未提供";
    const stats = document.createElement("dl");
    stats.className = "summary-grid";
    for (const [label, value] of [
        ["總題數", String(questions.length)],
      ["重試後成功", `${questions.filter((question) => question.manual_retry_succeeded === true).length} 題`],
      ["已排隊補答", `${Number.isInteger(run.queued_clarification_count) ? run.queued_clarification_count : 0} 題`],
      ["已排隊 retry", `${Number.isInteger(run.queued_retry_count) ? run.queued_retry_count : 0} 題`],
      ["整體狀態", statusNames[runStatus] || runStatus],
      ["總處理耗時（不含等待補答）", elapsedSummary],
      ["Token 總計", usageText({
        attempt_count: run.usage_attempt_count,
        has_usage: Object.values(run.token_coverage || {}).some((field) => field.known_attempt_count > 0),
        fields: run.token_coverage,
      })],
    ]) {
      const stat = document.createElement("div");
      stat.className = "stat";
      stat.append(createText("dt", label), createText("dd", value));
      stats.append(stat);
    }
    runSummary.append(stats);
    const counts = Object.fromEntries(questionStatuses.map((status) => [status, 0]));
    let unknownCount = 0;
    for (const question of questions) {
      const status = questionState(question);
      if (Object.hasOwn(counts, status)) counts[status] += 1;
      else unknownCount += 1;
    }
    const statusCounts = document.createElement("div");
    statusCounts.className = "status-counts";
    statusCounts.setAttribute("aria-label", "各題目狀態數量");
    for (const status of questionStatuses) {
      const count = document.createElement("span");
      count.className = `status-count status-${status}`;
      count.append(
        createText("span", statusNames[status]),
        createText("strong", String(counts[status])),
      );
      statusCounts.append(count);
    }
    if (unknownCount) {
      const count = document.createElement("span");
      count.className = "status-count status-unknown";
      count.append(createText("span", "狀態未知"), createText("strong", String(unknownCount)));
      statusCounts.append(count);
    }
    runSummary.append(statusCounts);

    const context = document.createElement("details");
    context.className = "run-context";
    context.append(createText("summary", "執行快照與版本"));
    const contextStats = document.createElement("dl");
    contextStats.className = "context-grid";
    for (const [label, value] of [
      ["題目檔 SHA-256", run.question_source?.sha256 || "未提供"],
      ["模型快照", run.model_snapshot?.model_id || "未提供"],
      ["資料快照", run.data_snapshot?.snapshot_id || "未提供"],
    ]) {
      const stat = document.createElement("div");
      stat.append(createText("dt", label), createText("dd", value, "snapshot"));
      contextStats.append(stat);
    }
    context.append(contextStats);
    runSummary.append(context);
    renderQuestionMatrix(questionMatrix, questions, runId, true);
    renderClarifications(questions, runId);
  }

  async function refresh() {
    if (refreshInProgress) return;
    refreshInProgress = true;
    try {
      const state = await api(statusPath());
      renderRun(state);
      if (state.last_error) setNotice("最近一次執行已中斷；請檢查狀態並續跑。", true);
      await renderRecentRuns();
    } catch (error) {
      statusText.textContent = "狀態讀取失敗";
      setNotice(error.message, true);
    } finally {
      refreshInProgress = false;
    }
  }

  async function waitForWorkerIdle(timeoutMs = 180000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const state = await api("/status");
      if (!state.worker_running) return;
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
    throw new Error("目前回合仍在安全收尾；紀錄已保留，稍後可重新開始新輪。");
  }

  async function renderRecentRuns() {
    const payload = await api("/runs");
    runsList.replaceChildren();
    if (!payload.runs || !payload.runs.length) {
      runsList.append(createText("li", "尚無歷史 run。", "empty"));
      return;
    }
    for (const run of payload.runs) {
      if (typeof run.run_id !== "string" || !/^[a-f0-9]{32}$/.test(run.run_id)) continue;
      const item = document.createElement("li");
      item.className = "run-item";
      const link = document.createElement("a");
      link.href = `${apiBase}/runs/${run.run_id}/summary.txt`;
      link.textContent = `摘要 ${run.run_id.slice(0, 8)}…`;
      const htmlLink = document.createElement("a");
      htmlLink.href = `${apiBase}/runs/${run.run_id}/report.html`;
      htmlLink.textContent = `HTML ${run.run_id.slice(0, 8)}…`;
      htmlLink.setAttribute("download", "");
      const printLink = document.createElement("a");
      printLink.href = `${apiBase}/runs/${run.run_id}/report/print`;
      printLink.textContent = `列印 ${run.run_id.slice(0, 8)}…`;
      printLink.target = "_blank";
      printLink.rel = "noopener noreferrer";
      const info = createText("span", `${run.question_count} 題 · ${run.created_at || "時間未提供"}`);
      const annotationInfo = createText("span", `人工註記 ${run.classification_annotation_count || 0} 筆`);
      const reviewLink = document.createElement("a");
      reviewLink.href = `/badmintonai/evaluation?run_id=${encodeURIComponent(run.run_id)}`;
      reviewLink.textContent = "複核題目";
      item.append(link, htmlLink, printLink, info, annotationInfo, reviewLink);
      runsList.append(item);
    }
  }

  form.addEventListener("change", (event) => {
    if (event.target === confirmRun) return;
    preview = null;
    previewBox.hidden = true;
    previewList.replaceChildren();
    updateSelectionState();
    confirmRun.checked = false;
    sourceFile.disabled = selectedSource() !== "upload";
  });
  confirmRun.addEventListener("change", () => {
    updateSelectionState();
  });
  selectAllButton.addEventListener("click", () => {
    for (const checkbox of previewList.querySelectorAll('input[name="question-selection"]')) {
      checkbox.checked = true;
    }
    updateSelectionState("已全選預覽題目。");
  });
  clearSelectionButton.addEventListener("click", () => {
    for (const checkbox of previewList.querySelectorAll('input[name="question-selection"]')) {
      checkbox.checked = false;
    }
    updateSelectionState();
  });
  applyRangeButton.addEventListener("click", applyQuestionRange);
  rangeInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      applyQuestionRange();
    }
  });

  previewButton.addEventListener("click", async () => {
    previewButton.disabled = true;
    try {
      let payload;
      if (selectedSource() === "upload") {
        const file = sourceFile.files && sourceFile.files[0];
        if (!file) throw new Error("請先選擇 .txt 檔案。");
        if (!file.name.toLowerCase().endsWith(".txt")) throw new Error("僅接受 .txt 檔案。");
        const sources = await api("/sources");
        if (file.size > sources.upload_max_bytes) throw new Error("檔案超過允許大小。");
        payload = await api("/preview-upload", {
          method: "POST",
          headers: {
            "Content-Type": "text/plain; charset=utf-8",
            "X-Upload-Filename": encodeURIComponent(file.name),
          },
          body: file,
        });
        payload.uploadFile = file;
      } else {
        payload = await api("/preview", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source: selectedSource() }),
        });
      }
      renderPreview(payload);
    } catch (error) {
      preview = null;
      previewBox.hidden = true;
      previewList.replaceChildren();
      updateSelectionState();
      setNotice(error.message, true);
    } finally {
      previewButton.disabled = false;
    }
  });

  startButton.addEventListener("click", async () => {
    if (startInProgress) return;
    const sourceSnapshot = selectedSource();
    const previewSnapshot = preview ? { ...preview } : null;
    const uploadFileSnapshot = previewSnapshot?.uploadFile || null;
    const questionIds = selectedQuestionIds().slice();
    if (!previewSnapshot || !confirmRun.checked) return;
    if (!questionIds.length) {
      updateSelectionState("至少選取一題才能開始評測。請先選取題目。" );
      return;
    }
    startInProgress = true;
    updateSelectionState();
    try {
      const beforeStart = await api("/status");
      const safeSwitch = beforeStart.worker_running === true
        && beforeStart.can_stop === true
        && safeRunId(beforeStart.run_id);
      const confirmation = safeSwitch
        ? `目前 run ${beforeStart.run_id.slice(0, 8)}… 正在執行。確定先安全停止目前回合、保留舊進度，再開始 ${questionIds.length} 題新評測？`
        : `確定開始 ${questionIds.length} 題評測？這會使用既有模型建立對話。`;
      if (!window.confirm(confirmation)) return;
      if (beforeStart.worker_running) {
        if (safeSwitch) {
          setNotice("已要求目前 run 安全停止；等待回合完成後開始新輪…");
          await api("/stop", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ run_id: beforeStart.run_id }),
          });
        } else {
          setNotice("目前有評測工作正在收尾；等待執行鎖釋放後開始新輪…");
        }
        await waitForWorkerIdle();
      }
      setNotice("正在讀取模型與資料快照，尚未送出題目…");
      let state;
      if (sourceSnapshot === "upload") {
        const file = uploadFileSnapshot;
        if (!file) throw new Error("上傳檔案已失效，請重新預覽。");
        state = await api("/start-upload", {
          method: "POST",
          headers: {
            "Content-Type": "text/plain; charset=utf-8",
            "X-Upload-Filename": encodeURIComponent(file.name),
            "X-Expected-SHA256": previewSnapshot.sha256,
            "X-Selected-Question-Ids": JSON.stringify(questionIds),
          },
          body: file,
        });
      } else {
        state = await api("/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            source: sourceSnapshot,
            sha256: previewSnapshot.sha256,
            question_ids: questionIds,
          }),
        });
      }
      renderRun(state);
      setNotice("評測已開始；結果會逐題保存，可隨時停止或續跑。");
      window.location.assign("/badmintonai/evaluation");
    } catch (error) {
      setNotice(error.message, true);
      await refresh();
    } finally {
      startInProgress = false;
      updateSelectionState();
    }
  });

  stopButton.addEventListener("click", async () => {
    stopButton.disabled = true;
    try {
      await api("/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_id: renderedRunId }),
      });
      setNotice("已要求停止；目前正在進行的回合會安全完成後停下。");
      await refresh();
    } catch (error) {
      setNotice(error.message, true);
    }
  });

  resumeButton.addEventListener("click", async () => {
    resumeButton.disabled = true;
    try {
      await api("/resume", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_id: renderedRunId }),
      });
      setNotice("已續跑；完成題不會重跑。");
      await refresh();
    } catch (error) {
      setNotice(error.message, true);
      await refresh();
    }
  });

  sourceFile.disabled = selectedSource() !== "upload";
  refresh();
  window.setInterval(refresh, 5000);
})();
