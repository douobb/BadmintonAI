"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function makeElement(id = "", dataset = {}) {
  const listeners = new Map();
  return {
    id,
    dataset,
    children: [],
    dataset: {},
    className: "",
    textContent: "",
    hidden: false,
    disabled: false,
    checked: false,
    value: "",
    classList: { toggle() {} },
    addEventListener(type, listener) {
      listeners.set(type, listener);
    },
    append(...items) {
      this.children.push(...items);
    },
    appendChild(item) {
      this.children.push(item);
    },
    replaceChildren(...items) {
      this.children = [...items];
    },
    setAttribute() {},
    removeAttribute() {},
    contains() {
      return false;
    },
    querySelector(selector) {
      return selector === 'input[name="source"]:checked'
        ? { value: "v2-natural-100" }
        : null;
    },
    querySelectorAll() {
      return [];
    },
  };
}

async function executePage(html, script) {
  const elements = new Map();
  for (const match of html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const tag = match[0];
    const dataset = {};
    for (const dataMatch of tag.matchAll(/\bdata-([\w-]+)="([^"]*)"/g)) {
      const key = dataMatch[1].replace(/-([a-z])/g, (_all, letter) => letter.toUpperCase());
      dataset[key] = dataMatch[2];
    }
    elements.set(match[1], makeElement(match[1], dataset));
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
    createElement() {
      return makeElement();
    },
  };
  const window = {
    location: { pathname: "/badmintonai/evaluation" },
    setInterval() {},
    getComputedStyle() {
      return { gridTemplateColumns: "none" };
    },
    confirm() {
      return false;
    },
  };
  const fetch = async (url) => {
    requests.push(String(url));
    const payload = String(url).endsWith("/status")
      ? {
          run: null,
          run_status: "idle",
          can_start: false,
          worker_running: false,
          can_resume: false,
          last_error: null,
        }
      : { runs: [] };
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
  return {
    error,
    requests,
    status: elements.get("run-status")?.textContent,
    recentText: elements.get("recent-runs")?.children
      .map((item) => item.textContent)
      .join(" "),
  };
}

async function main() {
  const input = JSON.parse(fs.readFileSync(0, "utf8"));
  const current = await executePage(input.html, input.script);
  assert.equal(current.error, null, `目前頁面初始化拋出錯誤：${current.error}`);
  assert.deepEqual(current.requests, [
    "/badmintonai/evaluation/api/status",
    "/badmintonai/evaluation/api/runs",
  ]);
  assert.equal(current.status, "尚無評測紀錄");
  assert.match(current.recentText, /尚無歷史 run/);

  // 用部署時可能出現的舊 HTML 形狀確認測試能攔截 JS 初始化失敗。
  const staleHtml = input.html.replace(
    'id="question-detail-dialog"',
    'id="stale-question-detail-dialog"',
  );
  const stale = await executePage(staleHtml, input.script);
  if (stale.error) {
    assert.match(stale.error.message, /addEventListener/);
  } else {
    assert.ok(
      stale.requests.includes("/badmintonai/evaluation/api/status")
        && stale.status !== "讀取中",
      "舊版標記不應讓介面無聲停在讀取中",
    );
  }

  process.stdout.write(JSON.stringify({
    current: "initialized",
    staleMarkup: stale.error ? "initialization error intercepted" : "initialized",
    status: current.status,
    recentText: current.recentText,
  }));
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
