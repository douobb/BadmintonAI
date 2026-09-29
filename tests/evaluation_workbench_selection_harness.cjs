"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function makeElement(tagName = "div", id = "", dataset = {}, selectedSource = "", iframeRequests = []) {
  const listeners = new Map();
  const attributes = new Map();
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
    classList: { toggle() {} },
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
    focus() {},
    showModal() {
      this.open = true;
    },
    querySelector(selector) {
      if (selector === 'input[name="source"]:checked') {
        return { value: selectedSource };
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
            : selector === ".question-tile"
              ? child.className.split(/\s+/).includes("question-tile")
              : false;
          if (matched) results.push(child);
          visit(child);
        }
      };
      visit(this);
      return results;
    },
  };
  return element;
}

async function executePage(html, script, {
  pathname,
  source = "v2-natural-100",
  initialState = null,
  conversationAvailable = true,
}) {
  let selectedSource = source;
  let navigatedTo = null;
  const elements = new Map();
  const iframeRequests = [];
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
      selectedSource,
      iframeRequests,
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
      return makeElement(tagName, "", {}, selectedSource, iframeRequests);
    },
  };
  const window = {
    location: {
      pathname,
      assign(path) {
        navigatedTo = path;
      },
    },
    setInterval() {},
    getComputedStyle() {
      return { gridTemplateColumns: "none" };
    },
    confirm() {
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
    let payload = { runs: [] };
    if (request.url.endsWith("/status")) payload = state;
    else if (request.url.endsWith("/sources")) payload = { upload_max_bytes: 1024 };
    else if (request.url.endsWith("/preview") || request.url.endsWith("/preview-upload")) payload = questionPayload;
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
    vm.runInNewContext(script, { document, window, fetch });
  } catch (caught) {
    error = caught;
  }
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  return {
    error,
    elements,
    requests,
    iframeRequests,
    documentTitle: document.title,
    get navigatedTo() { return navigatedTo; },
    setSource(value) { selectedSource = value; },
  };
}

async function click(element) {
  assert.ok(element, "預期 UI 元素存在");
  await element.trigger("click");
}

function descendants(element) {
  return element.children.flatMap((child) => [child, ...descendants(child)]);
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

  const builtin = await executePage(input.html, input.script, {
    pathname: "/badmintonai/evaluation/new",
  });
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
  let frame = descendants(questionDetails.elements.get("question-detail-content"))
    .find((element) => element.tagName === "iframe");
  assert.ok(frame, "開啟 Q3 詳情後應直接顯示對話 iframe");
  assert.equal(dialog.open, true);
  assert.equal(frame.hidden, false);
  assert.equal(frame.loading, "eager");
  assert.equal(
    frame.src,
    `/badmintonai/evaluation/api/runs/${detailRunId}/questions/3/conversation.html#chart-0`,
  );
  assert.equal(frame.getAttribute("sandbox"), "allow-scripts");
  const q3DetailsText = textContentDeep(questionDetails.elements.get("question-detail-content"));
  assert.ok(!q3DetailsText.includes("Q3 原題"), "原對話成功時不重複顯示純文字題目");
  assert.ok(!q3DetailsText.includes("Q3 回答"), "原對話成功時不重複顯示純文字回答");
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
  assert.equal(frame.hidden, false);
  assert.equal(
    frame.src,
    `/badmintonai/evaluation/api/runs/${detailRunId}/questions/4/conversation.html`,
  );
  assert.equal(q3Frame.getAttribute("src"), null, "切換題目時應清除舊對話來源");
  assert.deepEqual(questionDetails.iframeRequests, [
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
  assert.ok(!descendants(unavailable.elements.get("question-detail-content"))
    .some((element) => element.tagName === "iframe"), "原對話失敗時移除無法讀取的 iframe");
  assert.ok(fallbackText.includes("Q3 原題"), "原對話不可用時仍顯示原題備援");
  assert.ok(fallbackText.includes("Q3 回答"), "原對話不可用時仍顯示安全回答備援");
  assert.ok(fallbackText.includes("安全的題目與回答備援"));

  process.stdout.write(JSON.stringify({
    activePage: "new",
    builtinQuestionIds: JSON.parse(builtinStart.options.body).question_ids,
    uploadQuestionIds: JSON.parse(uploadStart.options.headers["X-Selected-Question-Ids"]),
    conversationQuestionIds: questionDetails.iframeRequests.map((url) => url.match(/questions\/(\d+)\//)?.[1]),
    fallbackVisible: fallbackText.includes("Q3 原題") && fallbackText.includes("Q3 回答"),
  }));
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
