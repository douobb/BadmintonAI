"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function makeElement(tagName = "div", id = "", dataset = {}, selectedSource = "", iframeRequests = [], iframeElements = []) {
  const listeners = new Map();
  const attributes = new Map();
  const classTokens = new Set();
  const element = {
    tagName,
    id,
    dataset,
    children: [],
    parentElement: null,
    attributes,
    listeners,
    className: "",
    textContent: "",
    hidden: false,
    disabled: false,
    checked: false,
    isConnected: true,
    focusCount: 0,
    value: "",
    name: "",
    type: "",
    files: [],
    get src() {
      return attributes.get("src") || "";
    },
    set src(value) {
      const source = String(value);
      attributes.set("src", source);
      if (tagName === "iframe") iframeRequests.push(source);
    },
    classList: {
      toggle(name, force) {
        const enabled = force === undefined ? !classTokens.has(name) : Boolean(force);
        if (enabled) classTokens.add(name);
        else classTokens.delete(name);
        return enabled;
      },
      contains(name) {
        return classTokens.has(name) || element.className.split(/\s+/).includes(name);
      },
    },
    addEventListener(type, listener) {
      listeners.set(type, listener);
    },
    async trigger(type, event = {}) {
      const listener = listeners.get(type);
      if (listener) return listener({ target: element, preventDefault() {}, ...event });
    },
    append(...items) {
      for (const item of items) {
        item.parentElement = element;
        this.children.push(item);
      }
    },
    appendChild(item) {
      this.append(item);
    },
    remove() {
      if (this.parentElement) {
        this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
        this.parentElement = null;
      }
    },
    replaceChildren(...items) {
      this.children = [];
      this.append(...items);
    },
    setAttribute(name, value) {
      attributes.set(name, String(value));
    },
    removeAttribute(name) {
      attributes.delete(name);
    },
    getAttribute(name) {
      return attributes.get(name) ?? null;
    },
    contains(target) {
      return this.children.some((child) => child === target || child.contains(target));
    },
    closest() {
      return null;
    },
    focus() {
      this.focusCount += 1;
    },
    setCustomValidity(message) {
      this.validationMessage = String(message);
    },
    reportValidity() {
      return !this.validationMessage;
    },
    showModal() {
      this.open = true;
    },
    close() {
      this.open = false;
      listeners.get("close")?.({ target: element });
    },
    getBoundingClientRect() {
      return { left: 100, right: 500, top: 100, bottom: 500 };
    },
    querySelector(selector) {
      if (selector === 'input[name="source"]:checked') {
        return { value: typeof selectedSource === "function" ? selectedSource() : selectedSource };
      }
      return this.querySelectorAll(selector)[0] || null;
    },
    querySelectorAll(selector) {
      const match = /^input\[name="([^"]+)"\](?::checked)?$/.exec(selector);
      const wantChecked = selector.endsWith(":checked");
      const results = [];
      const visit = (parent) => {
        for (const child of parent.children) {
          const matched = match
            ? child.tagName === "input"
              && child.name === match[1]
              && (!wantChecked || child.checked)
            : /^\.[\w-]+$/.test(selector)
              ? child.className.split(/\s+/).includes(selector.slice(1))
              : false;
          if (matched) results.push(child);
          visit(child);
        }
      };
      visit(this);
      return results;
    },
  };
  if (tagName === "iframe") {
    iframeElements.push(element);
    Object.defineProperty(element, "contentWindow", {
      get() {
        const error = new Error("Blocked a frame with opaque origin");
        error.name = "SecurityError";
        throw error;
      },
    });
  }
  return element;
}

async function executePage(html, script, {
  pathname,
  source = "v2-natural-100",
  initialState = null,
  conversationAvailable = true,
  search = "",
  recentRuns = [],
  holdStop = false,
  apiResponses = {},
}) {
  let selectedSource = source;
  let navigatedTo = null;
  let intervalCallback = null;
  let releaseStopResolver = null;
  const confirmationMessages = [];
  const elements = new Map();
  const iframeRequests = [];
  const iframeElements = [];
  for (const match of html.matchAll(/<[^>]+>/g)) {
    const tag = match[0];
    const dataset = {};
    for (const dataMatch of tag.matchAll(/\bdata-([\w-]+)="([^"]*)"/g)) {
      const key = dataMatch[1].replace(/-([a-z])/g, (_all, letter) => letter.toUpperCase());
      dataset[key] = dataMatch[2];
    }
    const id = /\bid="([^"]+)"/.exec(tag)?.[1] || "";
    if (!id && !dataset.pageView && !dataset.pageLink) continue;
    const element = makeElement(
      tag.startsWith("<input") ? "input" : "div",
      id,
      dataset,
      () => selectedSource,
      iframeRequests,
      iframeElements,
    );
    element.hidden = /\shidden(?:\s|>)/.test(tag);
    element.name = /\bname="([^"]+)"/.exec(tag)?.[1] || "";
    element.type = /\btype="([^"]+)"/.exec(tag)?.[1] || "";
    element.value = /\bvalue="([^"]+)"/.exec(tag)?.[1] || "";
    if (id) elements.set(id, element);
    if (dataset.pageView) elements.set(`page-view-${dataset.pageView}`, element);
    if (dataset.pageLink) elements.set(`nav-${dataset.pageLink}`, element);
  }
  const requests = [];
  const document = {
    activeElement: null,
    querySelector(selector) {
      const match = /^#([\w-]+)$/.exec(selector);
      return match ? elements.get(match[1]) || null : null;
    },
    querySelectorAll(selector) {
      const dataKey = selector === "[data-page-view]" ? "pageView"
        : selector === "[data-page-link]" ? "pageLink"
          : null;
      return dataKey
        ? [...elements.values()].filter((element) => element.dataset[dataKey] !== undefined)
        : [];
    },
    createElement(tagName) {
      return makeElement(tagName, "", {}, () => selectedSource, iframeRequests, iframeElements);
    },
  };
  const window = {
    location: {
      pathname,
      search,
      assign(path) {
        navigatedTo = path;
      },
    },
    setTimeout(callback) {
      callback();
      return 0;
    },
    setInterval(callback) {
      intervalCallback = callback;
    },
    getComputedStyle() {
      return { gridTemplateColumns: "none" };
    },
    confirm(message) {
      confirmationMessages.push(message);
      return true;
    },
  };
  const questionPayload = {
    question_count: 3,
    sha256: "a".repeat(64),
    questions: [
      { id: "1", prompt: "第一題原文" },
      { id: "4", prompt: "第四題原文" },
      { id: "10", prompt: "第十題原文" },
    ],
  };
  const state = initialState || {
    run: null,
    run_status: "idle",
    can_start: true,
    worker_running: false,
    can_resume: false,
    last_error: null,
  };
  const fetch = async (url, options = {}) => {
    const request = { url: String(url), options };
    requests.push(request);
    if (request.url.includes("/conversation.html")) {
      return {
        ok: conversationAvailable,
        status: conversationAvailable ? 200 : 502,
        headers: { get: () => "text/html; charset=utf-8" },
        body: { cancel: async () => {} },
      };
    }
    const apiPath = request.url.startsWith("/badmintonai/evaluation/api")
      ? request.url.slice("/badmintonai/evaluation/api".length)
      : null;
    const apiOverride = apiPath
      ? apiResponses[`${options.method || "GET"} ${apiPath}`]
      : null;
    if (apiOverride) {
      return {
        ok: apiOverride.status < 400,
        status: apiOverride.status,
        headers: { get: () => "application/json" },
        json: async () => apiOverride.payload,
      };
    }
    let payload = { runs: [] };
    if (request.url.includes("/status")) payload = state;
    else if (request.url.endsWith("/sources")) payload = { upload_max_bytes: 1024 };
    else if (request.url.endsWith("/preview") || request.url.endsWith("/preview-upload")) payload = questionPayload;
    else if (request.url.endsWith("/runs")) payload = { runs: recentRuns };
    else if (request.url.endsWith("/stop") && options.method === "POST") {
      if (holdStop) {
        await new Promise((resolve) => {
          releaseStopResolver = resolve;
        });
      }
      state.can_stop = false;
      const selectedRunId = JSON.parse(options.body || "{}").run_id;
      if (selectedRunId && selectedRunId !== state.active_run_id) {
        state.run_status = "stopped";
        state.can_resume = true;
      } else {
        state.worker_running = false;
      }
      payload = state;
    }
    else if (request.url.endsWith("/resume") && options.method === "POST") {
      const selectedRunId = JSON.parse(options.body || "{}").run_id;
      state.can_resume = false;
      state.can_stop = true;
      state.run_status = state.active_run_id === selectedRunId ? "running" : "queued";
      payload = state;
    }
    else if (request.url.endsWith("/start") || request.url.endsWith("/start-upload")) {
      payload = { ...state, can_start: false };
    }
    return {
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => payload,
    };
  };

  let error = null;
  try {
    // 僅在測試中暴露原有 helper，確認非 JSON body 不被通用 API 改寫。
    assert.match(script, /\}\)\(\);\s*$/);
    const testScript = script.replace(/\}\)\(\);\s*$/, "window.testApi = api;\n})();");
    vm.runInNewContext(testScript, {
      document,
      window,
      fetch,
      CSS: { escape: (value) => String(value) },
    });
  } catch (caught) {
    error = caught;
  }
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  return {
    error,
    elements,
    requests,
    api: window.testApi,
    iframeRequests,
    iframeElements,
    confirmationMessages,
    documentTitle: document.title,
    get navigatedTo() { return navigatedTo; },
    setSource(value) { selectedSource = value; },
    refresh() { return intervalCallback?.(); },
    releaseStop() { releaseStopResolver?.(); },
  };
}

async function click(element) {
  assert.ok(element, "預期 UI 元素存在");
  await element.trigger("click");
}

function descendants(element) {
  return element.children.flatMap((child) => [child, ...descendants(child)]);
}

function childIndexByClass(element, className) {
  return element.children.findIndex((child) => child.className.split(/\s+/).includes(className));
}

function assertDetailLead(article, questionId) {
  assert.equal(article.children[0].className, "question-head", "詳情第一列應是題號與狀態");
  assert.equal(article.children[0].children[0].textContent, `Q${questionId}`);
  assert.ok(article.children[0].children[1].className.includes("status-pill"), "狀態應與題號同列");
  assert.equal(article.children[1].children[0].textContent, "原始題目", "原題應緊接在題號列之後");
}

function assertConversationThenReview(article) {
  const conversationIndex = childIndexByClass(article, "conversation-preview-section");
  const reviewIndex = childIndexByClass(article, "classification-review");
  assert.ok(conversationIndex >= 0, "詳情應保留原始對話區");
  assert.ok(reviewIndex > conversationIndex, "分類複核區應位於原始對話之後");
  assert.equal(reviewIndex, article.children.length - 1, "分類複核區應是詳情最後一區");
}

function textContentDeep(element) {
  return [element.textContent, ...element.children.map(textContentDeep)].filter(Boolean).join(" ");
}

async function main() {
  const input = JSON.parse(fs.readFileSync(0, "utf8"));
  const progress = await executePage(input.html, input.script, {
    pathname: "/badmintonai/evaluation",
  });
  assert.equal(progress.error, null, `進度頁初始化失敗：${progress.error}`);
  assert.equal(progress.elements.get("page-view-progress").hidden, false);
  assert.equal(progress.elements.get("nav-progress").getAttribute("aria-current"), "page");
  assert.equal(progress.elements.get("page-title").textContent, "評測工作台");

  const history = await executePage(input.html, input.script, {
    pathname: "/badmintonai/evaluation/history",
  });
  assert.equal(history.error, null, `歷史頁初始化失敗：${history.error}`);
  assert.equal(history.elements.get("page-view-history").hidden, false);
  assert.equal(history.elements.get("page-view-progress").hidden, true);
  assert.equal(history.elements.get("nav-history").getAttribute("aria-current"), "page");
  assert.equal(history.elements.get("page-title").textContent, "近期紀錄");

  const historicalRunId = "b".repeat(32);
  const historyWithRuns = await executePage(input.html, input.script, {
    pathname: "/badmintonai/evaluation/history",
    recentRuns: [{
      run_id: historicalRunId,
      question_count: 5,
      created_at: "2026-10-03T00:00:00Z",
      classification_annotation_count: 2,
    }],
  });
  const reviewLink = descendants(historyWithRuns.elements.get("recent-runs"))
    .find((element) => element.textContent === "複核題目");
  assert.equal(
    reviewLink.href,
    `/badmintonai/evaluation?run_id=${historicalRunId}`,
    "歷史 run 應導向進度頁並選取該輪",
  );

  const selectedRun = await executePage(input.html, input.script, {
    pathname: "/badmintonai/evaluation",
    search: `?run_id=${historicalRunId}`,
    initialState: {
      run: {
        run_id: historicalRunId,
        question_source: {},
        questions: [{ id: "5", prompt: "舊輪原題", status: "completed" }],
        token_totals: {},
      },
      run_id: historicalRunId,
      active_run_id: "c".repeat(32),
      run_status: "completed",
      can_start: true,
      worker_running: true,
      can_stop: false,
      can_resume: false,
      last_error: null,
    },
  });
  assert.ok(selectedRun.requests.some((request) => request.url.endsWith(`status?run_id=${historicalRunId}`)));
  assert.match(selectedRun.elements.get("selected-run-notice").textContent, /舊輪原題|bbbbbbbb/);
  assert.equal(selectedRun.elements.get("stop-button").disabled, true, "檢視舊輪時不得停止目前新輪");
  assert.equal(selectedRun.elements.get("question-matrix").querySelectorAll(".question-tile").length, 1);

  const controlRunId = "d".repeat(32);
  const controlActiveRunId = "e".repeat(32);
  const runScopedControls = await executePage(input.html, input.script, {
    pathname: "/badmintonai/evaluation",
    search: `?run_id=${controlRunId}`,
    initialState: {
      run: {
        run_id: controlRunId,
        question_source: {},
        questions: [{ id: "1", prompt: "排隊輪原題", status: "pending" }],
        token_totals: {},
      },
      run_id: controlRunId,
      active_run_id: controlActiveRunId,
      run_status: "queued",
      can_start: true,
      worker_running: true,
      can_stop: true,
      can_resume: false,
      last_error: null,
    },
  });
  assert.equal(runScopedControls.elements.get("stop-button").disabled, false,
    "另一個 run 執行中時，仍可暫停選定的排隊 run");
  await click(runScopedControls.elements.get("stop-button"));
  const queuedStopRequest = runScopedControls.requests.find((request) => request.url.endsWith("/stop"));
  assert.equal(JSON.parse(queuedStopRequest.options.body).run_id, controlRunId);
  assert.equal(runScopedControls.elements.get("resume-button").disabled, false,
    "另一個 run 執行中時，仍可續跑已暫停的選定 run");
  await click(runScopedControls.elements.get("resume-button"));
  const queuedResumeRequest = runScopedControls.requests.find((request) => request.url.endsWith("/resume"));
  assert.equal(JSON.parse(queuedResumeRequest.options.body).run_id, controlRunId);
  assert.equal(runScopedControls.requests.some((request) => request.url.includes("/status")), true);

  const builtin = await executePage(input.html, input.script, {
    pathname: "/badmintonai/evaluation/new",
  });
  const retryState = {
    run_id: historicalRunId, run_status: "finished_with_errors", worker_running: false,
    run: {run_id: historicalRunId, question_source: {}, token_totals: {}, questions: [{id: "1", status: "failed", can_retry: true, manual_retry_count: 0}]},
  };
  const retryPage = await executePage(input.html, input.script, {
    pathname: "/badmintonai/evaluation", initialState: retryState,
  });
  const retryButton = retryPage.elements.get("question-matrix").querySelectorAll(".question-retry")[0];
  assert.ok(retryButton, "失敗題方格外應提供 retry button");
  assert.equal(retryButton.type, "button");
  assert.equal(retryButton.textContent, "retry");
  assert.match(retryButton.getAttribute("aria-label"), /Q1/);
  const retryTile = retryPage.elements.get("question-matrix").querySelectorAll(".question-tile")[0];
  assert.equal(retryButton.parentElement, retryTile.parentElement, "retry 應為方格外側的 sibling");
  assert.equal(retryTile.querySelectorAll(".question-retry").length, 0);
  await click(retryTile);
  assert.equal(retryPage.elements.get("question-detail-dialog").open, true);
  assert.equal(retryPage.elements.get("question-detail-content").querySelectorAll(".question-retry").length, 0, "詳情內不得出現 retry");
  const firstRetry = retryButton.trigger("click");
  await retryButton.trigger("click");
  await firstRetry;
  const retryCalls = retryPage.requests.filter((request) => request.url.endsWith("/1/retry"));
  assert.equal(retryCalls.length, 1, "重複點擊不得重送");
  assert.equal(retryCalls[0].options.method, "POST");
  assert.equal(retryCalls[0].options.headers["Content-Type"], "application/json");
  assert.equal(JSON.parse(retryCalls[0].options.body).expected_retry_count, 0);
  const multipartBody = new FormData();
  multipartBody.append("fixture", "非 JSON 上傳內容");
  await retryPage.api("/multipart-probe", { method: "POST", body: multipartBody });
  const multipartCall = retryPage.requests.find((request) => request.url.endsWith("/multipart-probe"));
  assert.equal(multipartCall.options.body, multipartBody);
  assert.equal(multipartCall.options.headers, undefined, "FormData 的 boundary 由瀏覽器提供，不得強制 JSON header");
  const busyRetryPage = await executePage(input.html, input.script, {
    pathname: "/badmintonai/evaluation", initialState: {...retryState, worker_running: true},
  });
  const busyRetryButton = busyRetryPage.elements.get("question-matrix").querySelectorAll(".question-retry")[0];
  assert.equal(busyRetryButton.disabled, false, "忙碌時仍可將 retry 追加至共享佇列");
  await click(busyRetryButton);
  assert.equal(
    busyRetryPage.requests.filter((request) => request.url.endsWith("/1/retry")).length,
    1,
    "忙碌時 retry 應以 JSON 提交",
  );

  const sixStatuses = [
    "queued",
    "running",
    "awaiting_clarification",
    "completed",
    "failed",
    "needs_review",
  ];
  const sixStatusPage = await executePage(input.html, input.script, {
    pathname: "/badmintonai/evaluation",
    initialState: {
      run_id: historicalRunId,
      active_run_id: historicalRunId,
      run_status: "running",
      worker_running: true,
      can_start: false,
      can_stop: true,
      can_resume: false,
      run: {
        run_id: historicalRunId,
        question_source: {},
        questions: sixStatuses.map((status, index) => ({
          id: String(index + 1),
          prompt: `狀態測試 ${status}`,
          status,
          display_status: status,
          usage_summary: {attempt_count: 0, fields: {}},
        })),
        token_totals: {},
        token_coverage: {},
      },
    },
  });
  await sixStatusPage.refresh();
  const sixTiles = sixStatusPage.elements.get("question-matrix")
    .querySelectorAll(".question-tile");
  assert.deepEqual(
    sixTiles.map((tile) => tile.className.split(" ").find((name) => name.startsWith("status-"))),
    sixStatuses.map((status) => `status-${status}`),
  );
  const statusCounts = descendants(sixStatusPage.elements.get("run-summary"))
    .find((element) => element.className === "status-counts");
  const statusCountItems = descendants(statusCounts)
    .filter((element) => element.className.startsWith("status-count status-"));
  assert.equal(statusCountItems.length, 6);
  assert.deepEqual(statusCountItems.map((element) => element.children[1].textContent), [
    "1", "1", "1", "1", "1", "1",
  ]);
  assert.equal(builtin.error, null, `選題頁初始化失敗：${builtin.error}`);
  assert.equal(builtin.elements.get("page-view-new").hidden, false);
  assert.equal(builtin.elements.get("page-view-progress").hidden, true);
  assert.equal(builtin.elements.get("nav-new").getAttribute("aria-current"), "page");
  assert.equal(builtin.elements.get("page-title").textContent, "預覽與選題");

  await click(builtin.elements.get("preview-button"));
  const selected = () => builtin.elements.get("preview-list")
    .querySelectorAll('input[name="question-selection"]');
  assert.deepEqual(selected().filter((item) => item.checked).map((item) => item.value), ["1", "4", "10"]);
  assert.equal(builtin.elements.get("selected-count").textContent, "已選 3 / 3 題");

  builtin.elements.get("range-input").value = "4-10";
  await click(builtin.elements.get("apply-range-button"));
  assert.deepEqual(selected().filter((item) => item.checked).map((item) => item.value), ["4", "10"]);
  assert.equal(builtin.elements.get("selected-count").textContent, "已選 2 / 3 題");

  await click(builtin.elements.get("clear-selection-button"));
  assert.equal(builtin.elements.get("start-button").disabled, true);
  await builtin.elements.get("start-button").trigger("click");
  assert.equal(builtin.requests.some((request) => request.url.endsWith("/start")), false);
  assert.match(builtin.elements.get("selection-message").textContent, /至少選取一題/);

  builtin.elements.get("range-input").value = "4-10";
  await click(builtin.elements.get("apply-range-button"));
  builtin.elements.get("confirm-run").checked = true;
  await builtin.elements.get("confirm-run").trigger("change");
  await click(builtin.elements.get("start-button"));
  const builtinStart = builtin.requests.find((request) => request.url.endsWith("/start"));
  assert.deepEqual(JSON.parse(builtinStart.options.body).question_ids, ["4", "10"]);
  assert.equal(JSON.parse(builtinStart.options.body).sha256, "a".repeat(64));
  assert.equal(builtin.navigatedTo, "/badmintonai/evaluation");

  const upload = await executePage(input.html, input.script, {
    pathname: "/badmintonai/evaluation/new",
    source: "upload",
  });
  assert.equal(upload.error, null, `上傳選題頁初始化失敗：${upload.error}`);
  upload.elements.get("question-file").files = [{ name: "questions.txt", size: 20 }];
  await click(upload.elements.get("preview-button"));
  upload.elements.get("confirm-run").checked = true;
  await upload.elements.get("confirm-run").trigger("change");
  await click(upload.elements.get("start-button"));
  const uploadStart = upload.requests.find((request) => request.url.endsWith("/start-upload"));
  assert.equal(uploadStart.options.headers["X-Selected-Question-Ids"], '["1","4","10"]');
  assert.equal(uploadStart.options.headers["X-Expected-SHA256"], "a".repeat(64));
  assert.equal(upload.navigatedTo, "/badmintonai/evaluation");

  const activeRunId = "d".repeat(32);
  const switching = await executePage(input.html, input.script, {
    pathname: "/badmintonai/evaluation/new",
    initialState: {
      run: {
        run_id: activeRunId,
        question_source: {},
        questions: [{ id: "1", prompt: "舊輪題目", status: "running" }],
        token_totals: {},
      },
      run_id: activeRunId,
      active_run_id: activeRunId,
      run_status: "running",
      can_start: true,
      worker_running: true,
      can_stop: true,
      can_resume: false,
      last_error: null,
    },
  });
  await click(switching.elements.get("preview-button"));
  switching.elements.get("confirm-run").checked = true;
  await switching.elements.get("confirm-run").trigger("change");
  await click(switching.elements.get("start-button"));
  const stopRequest = switching.requests.find((request) => request.url.endsWith("/stop"));
  const startRequest = switching.requests.find((request) => request.url.endsWith("/start"));
  assert.ok(stopRequest && startRequest, "新輪應先安全停止舊輪再開始");
  assert.ok(switching.requests.indexOf(stopRequest) < switching.requests.indexOf(startRequest));
  assert.deepEqual(JSON.parse(stopRequest.options.body), { run_id: activeRunId });
  assert.match(switching.confirmationMessages.at(-1), /安全停止目前回合/);
  assert.equal(switching.navigatedTo, "/badmintonai/evaluation");

  const switchingGuard = await executePage(input.html, input.script, {
    pathname: "/badmintonai/evaluation/new",
    holdStop: true,
    initialState: {
      run: {
        run_id: activeRunId,
        question_source: {},
        questions: [{ id: "1", prompt: "舊輪題目", status: "running" }],
        token_totals: {},
      },
      run_id: activeRunId,
      active_run_id: activeRunId,
      run_status: "running",
      can_start: true,
      worker_running: true,
      can_stop: true,
      can_resume: false,
      last_error: null,
    },
  });
  await click(switchingGuard.elements.get("preview-button"));
  switchingGuard.elements.get("confirm-run").checked = true;
  await switchingGuard.elements.get("confirm-run").trigger("change");
  const guardedStart = switchingGuard.elements.get("start-button");
  const pendingStart = guardedStart.trigger("click");
  for (let attempt = 0; attempt < 10; attempt += 1) {
    if (switchingGuard.requests.some((request) => request.url.endsWith("/stop"))) break;
    await new Promise((resolve) => setImmediate(resolve));
  }
  const guardedStops = () => switchingGuard.requests
    .filter((request) => request.url.endsWith("/stop"));
  assert.equal(guardedStops().length, 1, "開始前應只要求停止一次");
  assert.equal(guardedStart.disabled, true, "停止等待期間開始按鈕應維持停用");
  await switchingGuard.refresh();
  assert.equal(guardedStart.disabled, true, "週期刷新不得解除開始中的防重入狀態");
  switchingGuard.setSource("upload");
  await switchingGuard.elements.get("source-form").trigger("change", {
    target: switchingGuard.elements.get("source-upload"),
  });
  switchingGuard.elements.get("question-file").files = [{ name: "changed.txt", size: 2 }];
  await guardedStart.trigger("click");
  assert.equal(guardedStops().length, 1, "等待期間再次點擊不得重送停止要求");
  switchingGuard.releaseStop();
  await pendingStart;
  const guardedStarts = switchingGuard.requests.filter((request) =>
    request.url.endsWith("/start") || request.url.endsWith("/start-upload"));
  assert.equal(guardedStarts.length, 1, "等待期間再次點擊只能送出一筆新輪");
  assert.equal(guardedStarts[0].url.endsWith("/start"), true, "提交來源應使用點擊時的內建來源快照");
  assert.deepEqual(JSON.parse(guardedStarts[0].options.body), {
    source: "v2-natural-100",
    sha256: "a".repeat(64),
    question_ids: ["1", "4", "10"],
  });

  const detailRunId = "a".repeat(32);
  const detailQuestion = (id) => ({
    id: String(id),
    prompt: `Q${id} 原題`,
    status: "completed",
    display_status: "completed",
    conversation_id: `chat-q${id}`,
    assistant_text: `Q${id} 回答`,
    elapsed_ms: 259918,
    user_wait_ms: 224405,
    chart_count: id === 3 ? 1 : 0,
    clarification_review_candidate: false,
    error_count: 0,
  });
  const questionDetails = await executePage(input.html, input.script, {
    pathname: "/badmintonai/evaluation",
    initialState: {
      run: {
        run_id: detailRunId,
        question_source: {},
        questions: [detailQuestion(3), detailQuestion(4)],
        token_totals: { input_tokens: null, output_tokens: null, total_tokens: null },
      },
      run_id: detailRunId,
      run_status: "completed",
      can_start: false,
      worker_running: false,
      can_resume: false,
      last_error: null,
    },
  });
  assert.equal(questionDetails.error, null, `題目詳情頁初始化失敗：${questionDetails.error}`);
  const tiles = questionDetails.elements.get("question-matrix")
    .querySelectorAll(".question-tile");
  assert.equal(tiles.length, 2);
  const dialog = questionDetails.elements.get("question-detail-dialog");

  await click(tiles[0]);
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(dialog.open, true);
  await dialog.trigger("click", { target: {}, clientX: 200, clientY: 200 });
  assert.equal(dialog.open, true, "dialog 內部點擊不得關閉");
  await dialog.trigger("click", { target: dialog, clientX: 20, clientY: 20 });
  assert.equal(dialog.open, false, "點擊 dialog 外側空白應關閉");
  assert.ok(tiles[0].focusCount > 0, "關閉 dialog 後焦點應返回題目格");

  await click(tiles[0]);
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  let frame = descendants(questionDetails.elements.get("question-detail-content"))
    .find((element) => element.tagName === "iframe");
  assert.ok(frame, "開啟 Q3 詳情後應直接顯示對話 iframe");
  assert.equal(dialog.open, true);
  assert.equal(frame.hidden, true, "內容完成載入前應隱藏 about:blank frame");
  assert.equal(frame.loading, "eager");
  assert.equal(
    frame.src,
    `/badmintonai/evaluation/api/runs/${detailRunId}/questions/3/conversation.html#chart-0`,
  );
  assert.equal(frame.getAttribute("sandbox"), "allow-scripts");
  await frame.trigger("load");
  assert.equal(frame.hidden, false, "原對話載入後應顯示 frame");
  assert.match(frame.parentElement.children[1].textContent, /原對話已載入/);
  const q3Article = descendants(questionDetails.elements.get("question-detail-content"))
    .find((element) => element.tagName === "article" && element.className === "question-detail");
  const q3DetailsText = textContentDeep(q3Article);
  assertDetailLead(q3Article, 3);
  assert.ok(q3DetailsText.includes("Q3 原題"), "原對話成功時仍須在詳情上方顯示原題");
  assert.ok(!q3DetailsText.includes("Q3 回答"), "原對話成功時不重複顯示純文字回答");
  assert.ok(childIndexByClass(q3Article, "detail-metrics") > 1, "metrics 應排在題號列與原題之後");
  assertConversationThenReview(q3Article);
  assert.ok(!q3DetailsText.includes("已讀回"), "原對話成功時不再顯示圖表摘要區塊");
  assert.ok(!q3DetailsText.includes("人工等待補答"), "詳情不可顯示人工等待時間");
  assert.ok(!q3DetailsText.includes("端到端耗時（含等待）"), "詳情不可顯示含人工等待的耗時");
  assert.ok(q3DetailsText.includes("00:35.513"), "處理耗時應扣除人工等待時間");
  assert.ok(!q3DetailsText.includes("03:44.405"), "不可展示人工等待補答的時長值");

  const q3Frame = frame;
  await click(tiles[1]);
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  frame = descendants(questionDetails.elements.get("question-detail-content"))
    .find((element) => element.tagName === "iframe");
  assert.ok(frame, "開啟 Q4 詳情後應直接顯示對話 iframe");
  assert.equal(frame.hidden, true);
  assert.equal(
    frame.src,
    `/badmintonai/evaluation/api/runs/${detailRunId}/questions/4/conversation.html`,
  );
  await frame.trigger("load");
  assert.equal(frame.hidden, false);
  assert.equal(q3Frame.getAttribute("src"), null, "切換題目時應清除舊對話來源");
  assert.deepEqual(questionDetails.iframeRequests, [
    `/badmintonai/evaluation/api/runs/${detailRunId}/questions/3/conversation.html#chart-0`,
    `/badmintonai/evaluation/api/runs/${detailRunId}/questions/3/conversation.html#chart-0`,
    `/badmintonai/evaluation/api/runs/${detailRunId}/questions/4/conversation.html`,
  ]);

  const unavailable = await executePage(input.html, input.script, {
    pathname: "/badmintonai/evaluation",
    conversationAvailable: false,
    initialState: {
      run: {
        run_id: detailRunId,
        question_source: {},
        questions: [detailQuestion(3)],
        token_totals: { input_tokens: null, output_tokens: null, total_tokens: null },
      },
      run_id: detailRunId,
      run_status: "completed",
      can_start: false,
      worker_running: false,
      can_resume: false,
      last_error: null,
    },
  });
  const unavailableTile = unavailable.elements.get("question-matrix")
    .querySelectorAll(".question-tile")[0];
  await click(unavailableTile);
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  const fallbackText = textContentDeep(unavailable.elements.get("question-detail-content"));
  const unavailableArticle = descendants(unavailable.elements.get("question-detail-content"))
    .find((element) => element.tagName === "article" && element.className === "question-detail");
  const unavailableConversation = unavailableArticle.children
    .find((element) => element.className.includes("conversation-preview-section"));
  assert.ok(!descendants(unavailable.elements.get("question-detail-content"))
    .some((element) => element.tagName === "iframe"), "原對話失敗時移除無法讀取的 iframe");
  assert.ok(fallbackText.includes("Q3 原題"), "原對話不可用時仍顯示原題備援");
  assert.ok(fallbackText.includes("Q3 回答"), "原對話不可用時仍顯示安全回答備援");
  assert.ok(fallbackText.includes("原題保留於上方，以下提供安全回答備援。"));
  assert.equal(unavailableArticle.children.at(-1).className, "classification-review");
  assert.ok(unavailableArticle.children.indexOf(unavailableConversation)
    < unavailableArticle.children.length - 1, "備援原對話應在分類複核區之前");
  assert.ok(!textContentDeep(unavailableConversation).includes("Q3 原題"), "fallback 不得重複顯示原題");

  const noConversation = await executePage(input.html, input.script, {
    pathname: "/badmintonai/evaluation",
    initialState: {
      run: {
        run_id: detailRunId,
        question_source: {},
        questions: [{ ...detailQuestion(7), conversation_id: null, chart_count: 0 }],
        token_totals: {},
      },
      run_id: detailRunId,
      run_status: "completed",
      can_start: false,
      worker_running: false,
      can_resume: false,
      last_error: null,
    },
  });
  const noConversationTile = noConversation.elements.get("question-matrix")
    .querySelectorAll(".question-tile")[0];
  await click(noConversationTile);
  await new Promise((resolve) => setImmediate(resolve));
  const noConversationArticle = descendants(noConversation.elements.get("question-detail-content"))
    .find((element) => element.tagName === "article" && element.className === "question-detail");
  assertDetailLead(noConversationArticle, 7);
  assert.ok(noConversationArticle.children[2].className.includes("detail-block"), "無原對話時仍須顯示安全回答");
  assert.ok(childIndexByClass(noConversationArticle, "detail-metrics") > 2);
  assertConversationThenReview(noConversationArticle);

  const queuedRunId = "e".repeat(32);
  const queuedState = {
    run: {
      run_id: queuedRunId,
      question_source: {},
      questions: [{
        id: "9",
        prompt: "需要釐清的原題",
        status: "awaiting_clarification",
        display_status: "queued",
        assistant_text: "請補充必要資訊。",
        queued_clarification: { status: "queued", answer: "第一次保存的補答" },
        conversation_id: null,
        processing_elapsed_ms: 4200,
        token_totals: {},
        usage: null,
        retry_count: 0,
        tool_call_count: 0,
        error_count: 0,
      }],
      queued_clarification_count: 1,
      token_totals: {},
    },
    run_id: queuedRunId,
    active_run_id: queuedRunId,
    run_status: "running",
    can_start: false,
    worker_running: true,
    can_stop: true,
    can_resume: false,
    last_error: null,
  };
  const queueOps = await executePage(input.html, input.script, {
    pathname: "/badmintonai/evaluation",
    initialState: queuedState,
    apiResponses: {
      [`PATCH /clarification-queue/${queuedRunId}/9`]: { status: 200, payload: queuedState },
      [`DELETE /clarification-queue/${queuedRunId}/9`]: { status: 200, payload: queuedState },
    },
  });
  assert.equal(queueOps.error, null, `排隊補答頁初始化失敗：${queueOps.error}`);
  await queueOps.refresh();
  const queuedTile = queueOps.elements.get("question-matrix")
    .querySelectorAll(".question-tile")[0];
  assert.ok(queuedTile.className.includes("status-queued"));
  assert.ok(textContentDeep(queuedTile).includes("排隊中"));
  assert.ok(!queuedTile.className.includes("status-completed"), "刷新後排隊題不得投影成已完成");
  assert.match(textContentDeep(queueOps.elements.get("run-summary")), /已排隊補答 1 題/);
  await click(queuedTile);
  await new Promise((resolve) => setImmediate(resolve));
  let queueDetailText = textContentDeep(queueOps.elements.get("question-detail-content"));
  const queuedArticle = descendants(queueOps.elements.get("question-detail-content"))
    .find((element) => element.tagName === "article" && element.className === "question-detail");
  assertDetailLead(queuedArticle, 9);
  assert.equal(childIndexByClass(queuedArticle, "clarification-detail"), 2,
    "已排隊補答內容應在原題後、metrics 前");
  assert.ok(childIndexByClass(queuedArticle, "clarification-detail")
    < childIndexByClass(queuedArticle, "detail-metrics"));
  assertConversationThenReview(queuedArticle);
  assert.ok(queueDetailText.includes("補答已排隊"));
  assert.ok(queueDetailText.includes("已保存補答"));
  assert.ok(queueDetailText.includes("第一次保存的補答"));

  const queuedEditForm = descendants(queueOps.elements.get("question-detail-content"))
    .find((element) => element.tagName === "form" && element.className.includes("queued-clarification-edit"));
  const queuedEditAnswer = descendants(queuedEditForm)
    .find((element) => element.tagName === "textarea");
  assert.equal(queuedEditAnswer.value, "第一次保存的補答");
  queuedEditAnswer.value = "修改後的補答";
  await queuedEditForm.trigger("submit");
  const patchRequest = queueOps.requests.find((request) => request.options.method === "PATCH");
  assert.ok(patchRequest, "待執行補答應可送出 PATCH 修改");
  assert.equal(patchRequest.url, `/badmintonai/evaluation/api/clarification-queue/${queuedRunId}/9`);
  assert.deepEqual(JSON.parse(patchRequest.options.body), { answer: "修改後的補答" });

  const refreshedQueuedTile = queueOps.elements.get("question-matrix")
    .querySelectorAll(".question-tile")[0];
  await click(refreshedQueuedTile);
  await new Promise((resolve) => setImmediate(resolve));
  const cancelButton = descendants(queueOps.elements.get("question-detail-content"))
    .find((element) => element.tagName === "button" && element.textContent === "取消排隊");
  await click(cancelButton);
  const deleteRequest = queueOps.requests.find((request) => request.options.method === "DELETE");
  assert.ok(deleteRequest, "待執行補答應可送出 DELETE 取消");
  assert.equal(deleteRequest.url, `/badmintonai/evaluation/api/clarification-queue/${queuedRunId}/9`);

  const executingState = {
    ...queuedState,
    run: {
      ...queuedState.run,
      questions: [{
        ...queuedState.run.questions[0],
        display_status: "running",
        queued_clarification: { status: "executing", answer: "開始執行的補答" },
      }],
      queued_clarification_count: 0,
    },
  };
  const executing = await executePage(input.html, input.script, {
    pathname: "/badmintonai/evaluation",
    initialState: executingState,
  });
  const executingTile = executing.elements.get("question-matrix")
    .querySelectorAll(".question-tile")[0];
  assert.ok(executingTile.className.includes("status-running"));
  await click(executingTile);
  await new Promise((resolve) => setImmediate(resolve));
  const executingDetail = descendants(executing.elements.get("question-detail-content"));
  assert.ok(textContentDeep(executing.elements.get("question-detail-content"))
    .includes("此補答已開始送回原對話，目前無法修改或取消。"));
  assert.equal(executingDetail.some((element) => element.tagName === "form"), false);
  assert.equal(executingDetail.some((element) => element.tagName === "button"
    && ["更新補答", "取消排隊"].includes(element.textContent)), false);

  const clarificationQuestion = {
    ...queuedState.run.questions[0],
    display_status: "awaiting_clarification",
    queued_clarification: null,
    choices: ["選項甲", "選項乙"],
  };
  const duplicate = await executePage(input.html, input.script, {
    pathname: "/badmintonai/evaluation",
    initialState: {
      ...queuedState,
      run: { ...queuedState.run, questions: [clarificationQuestion], queued_clarification_count: 0 },
    },
    apiResponses: {
      "POST /clarify": { status: 200, payload: { ...queuedState, already_queued: true } },
    },
  });
  const clarificationTile = duplicate.elements.get("question-matrix")
    .querySelectorAll(".question-tile")[0];
  await click(clarificationTile);
  await new Promise((resolve) => setImmediate(resolve));
  const clarificationForm = descendants(duplicate.elements.get("question-detail-content"))
    .find((element) => element.tagName === "form" && element.className === "clarification");
  const awaitingArticle = descendants(duplicate.elements.get("question-detail-content"))
    .find((element) => element.tagName === "article" && element.className === "question-detail");
  assertDetailLead(awaitingArticle, 9);
  assert.equal(childIndexByClass(awaitingArticle, "clarification-detail"), 2,
    "待補答說明與選項應位於原題之後");
  assert.ok(childIndexByClass(awaitingArticle, "clarification-detail") < childIndexByClass(awaitingArticle, "detail-metrics"));
  assertConversationThenReview(awaitingArticle);
  assert.equal(descendants(clarificationForm)
    .filter((element) => element.tagName === "input" && element.type === "radio").length, 2);
  assert.equal(descendants(awaitingArticle).filter((element) => element.className === "answer").length, 0,
    "澄清文字應位於待補答區，不可再當成回答重複顯示");
  assert.equal(textContentDeep(awaitingArticle).split("請補充必要資訊。").length - 1, 1);
  const clarificationAnswer = descendants(clarificationForm)
    .find((element) => element.tagName === "textarea");
  clarificationAnswer.value = "這是重複送出的補答";
  await clarificationForm.trigger("submit");
  assert.equal(duplicate.elements.get("notice").textContent, "Q9 補答已在佇列中。");
  assert.equal(duplicate.elements.get("notice").classList.contains("error"), false);
  assert.equal(duplicate.requests.find((request) => request.url.endsWith("/clarify")).options.method, "POST");

  process.stdout.write(JSON.stringify({
    activePage: "new",
    builtinQuestionIds: JSON.parse(builtinStart.options.body).question_ids,
    uploadQuestionIds: JSON.parse(uploadStart.options.headers["X-Selected-Question-Ids"]),
    conversationQuestionIds: questionDetails.iframeRequests.map((url) => url.match(/questions\/(\d+)\//)?.[1]),
    fallbackVisible: fallbackText.includes("Q3 原題") && fallbackText.includes("Q3 回答"),
    queuedClarification: "tested",
  }));
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
