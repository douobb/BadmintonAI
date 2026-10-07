"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

async function main() {
  const modulePath = path.join(
    __dirname,
    "..",
    "openwebui_patch",
    "evaluation_message_sync.js",
  );
  const source = fs.readFileSync(modulePath, "utf8");
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
  const { createEvaluationMessageSynchronizer } = await import(moduleUrl);

  let activeChatId = "chat-current";
  let messages = {
    previous: { id: "previous", content: "既有回答", childrenIds: [] },
  };
  const draft = "尚未送出的本地草稿";
  let fetchCount = 0;
  let resolveFetch;
  const dispatched = [];
  const remoteChat = {
    chat: {
      history: {
        currentId: "assistant-1",
        messages: {
          previous: { id: "previous", content: "伺服器舊文不可覆蓋", childrenIds: ["user-1"] },
          "user-1": { id: "user-1", role: "user", parentId: "previous", childrenIds: ["assistant-1"] },
          "assistant-1": {
            id: "assistant-1",
            role: "assistant",
            parentId: "user-1",
            childrenIds: [],
            content: "Hello",
            done: false,
            statusHistory: [{ description: "thinking", done: false }],
          },
        },
      },
    },
  };
  const syncUnknown = createEvaluationMessageSynchronizer({
    getActiveChatId: () => activeChatId,
    getMessage: (id) => messages[id],
    fetchChat: async (chatId) => {
      assert.equal(chatId, "chat-current");
      fetchCount += 1;
      return new Promise((resolve) => {
        resolveFetch = () => resolve(remoteChat);
      });
    },
    mergeBranch: (chat, requestedId) => {
      const remote = chat.chat.history.messages;
      const branch = [];
      let cursor = remote[requestedId];
      while (cursor && !messages[cursor.id]) {
        branch.unshift(cursor);
        cursor = cursor.parentId ? remote[cursor.parentId] : null;
      }
      for (const item of branch) messages[item.id] = structuredClone(item);
      if (cursor) {
        messages[cursor.id].childrenIds = [
          ...new Set([...(messages[cursor.id].childrenIds ?? []), branch[0].id]),
        ];
      }
    },
    dispatchEvent: async (event, callback) => {
      dispatched.push({ event, callback });
      const message = messages[event.message_id];
      const payload = event.data?.data;
      if (event.data?.type === "chat:message:delta") message.content += payload.content;
      if (event.data?.type === "status") message.statusHistory = [...(message.statusHistory ?? []), payload];
    },
  });

  const firstCallback = () => "native callback";
  const events = [
    { chat_id: "chat-current", message_id: "assistant-1", data: { type: "chat:active", data: { active: true } } },
    { chat_id: "chat-current", message_id: "assistant-1", data: { type: "status", data: { description: "thinking", done: false } } },
    { chat_id: "chat-current", message_id: "assistant-1", data: { type: "chat:message:delta", data: { content: "Hel" } } },
    { chat_id: "chat-current", message_id: "assistant-1", data: { type: "chat:message:delta", data: { content: "lo" } } },
  ];
  const pending = events.map((event, index) => syncUnknown(event, index === 0 ? firstCallback : undefined));
  assert.equal(fetchCount, 1, "同一個 in-flight chat 只能觸發一次讀回");
  resolveFetch();
  await Promise.all(pending);

  assert.equal(messages.previous.content, "既有回答", "不得以讀回資料覆蓋本地既有節點");
  assert.ok(messages.previous.childrenIds.includes("user-1"), "讀回分支須掛回既有父節點");
  assert.equal(messages["assistant-1"].content, "Hello", "讀回已含串流文字時不可重播造成重複");
  assert.equal(messages["assistant-1"].statusHistory.length, 1);
  assert.equal(dispatched[0].callback, firstCallback, "native event callback 必須沿用");
  assert.deepEqual(dispatched.map(({ event }) => event.data.type), ["chat:active"]);
  assert.equal(draft, "尚未送出的本地草稿", "同步不得改動 composer draft");

  const knownMessageDelta = {
    chat_id: "chat-current",
    message_id: "assistant-1",
    data: { type: "chat:message:delta", data: { content: " there" } },
  };
  assert.equal(await syncUnknown(knownMessageDelta), false, "已知節點直接走既有 event handler");
  messages["assistant-1"].content += knownMessageDelta.data.data.content;
  assert.equal(messages["assistant-1"].content, "Hello there");
  assert.equal(fetchCount, 1, "之後每個串流 token 不得再次 GET chat");

  activeChatId = "chat-current";
  const beforeNoncurrent = fetchCount;
  const noncurrentResult = await syncUnknown({
    chat_id: "chat-other",
    message_id: "assistant-other",
    data: { type: "chat:active", data: { active: true } },
  });
  assert.equal(noncurrentResult, false);
  assert.equal(fetchCount, beforeNoncurrent, "非目前聊天室事件不得觸發讀回");

  console.log("evaluation message sync harness: ok");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
