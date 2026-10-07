/** 在目前已開啟的聊天室安全補齊遠端新增的原生訊息分支。 */
export function createEvaluationMessageSynchronizer({
	getActiveChatId,
	getMessage,
	fetchChat,
	mergeBranch,
	dispatchEvent
}) {
	const pendingByChat = new Map();
	const refreshByChat = new Map();
	const retryAfterByChat = new Map();
	const retryDelayMs = 1000;

	const alreadyPersistedDeltaCounts = (batch) => {
		const byMessage = new Map();
		for (const item of batch) {
			if (item.event?.data?.type !== 'chat:message:delta') continue;
			const content = item.event?.data?.data?.content;
			if (typeof content !== 'string' || content.length === 0) continue;
			const items = byMessage.get(item.messageId) ?? [];
			items.push(item);
			byMessage.set(item.messageId, items);
		}
		const counts = new Map();
		for (const [messageId, items] of byMessage) {
			const currentContent = String(getMessage(messageId)?.content ?? '');
			let prefix = '';
			let represented = 0;
			for (const item of items) {
				prefix += item.event.data.data.content;
				if (currentContent.startsWith(prefix)) represented += 1;
			}
			counts.set(messageId, represented);
		}
		return counts;
	};

	return async function synchronizeUnknownMessage(event, callback) {
		const chatId = getActiveChatId();
		const messageId = event?.message_id;
		if (
			!chatId ||
			event?.chat_id !== chatId ||
			!messageId ||
			getMessage(messageId) ||
			event?.__evaluationMessageReplay === true
		) {
			return false;
		}

		const pending = pendingByChat.get(chatId) ?? [];
		pending.push({ event, callback, messageId });
		pendingByChat.set(chatId, pending);

		let refresh = refreshByChat.get(chatId);
		if (!refresh && Date.now() >= (retryAfterByChat.get(chatId) ?? 0)) {
			refresh = (async () => {
				try {
					const remoteChat = await fetchChat(chatId);
					if (getActiveChatId() !== chatId) {
						pendingByChat.delete(chatId);
						return;
					}

					let batch;
					while ((batch = pendingByChat.get(chatId) ?? []).length > 0) {
						pendingByChat.set(chatId, []);
						const mergedMessageIds = new Set();
						for (const item of batch) {
							if (mergedMessageIds.has(item.messageId)) continue;
							mergedMessageIds.add(item.messageId);
							mergeBranch(remoteChat, item.messageId);
						}
						const representedDeltaCounts = alreadyPersistedDeltaCounts(batch);
						const replayedDeltaCounts = new Map();
						for (const item of batch) {
							if (getActiveChatId() !== chatId) {
								pendingByChat.delete(chatId);
								return;
							}
							if (!getMessage(item.messageId)) continue;
							if (item.event?.data?.type === 'chat:message:delta') {
								const replayed = replayedDeltaCounts.get(item.messageId) ?? 0;
								const represented = representedDeltaCounts.get(item.messageId) ?? 0;
								replayedDeltaCounts.set(item.messageId, replayed + 1);
								if (replayed < represented) continue;
							}
							if (item.event?.data?.type === 'status') {
								const status = item.event?.data?.data;
								const statusHistory = getMessage(item.messageId)?.statusHistory ?? [];
								if (
									statusHistory.some(
										(existing) => JSON.stringify(existing) === JSON.stringify(status)
									)
								) continue;
							}
							await dispatchEvent(
								{ ...item.event, __evaluationMessageReplay: true },
								item.callback
							);
						}
					}
					retryAfterByChat.delete(chatId);
				} catch {
					// 保留待處理事件；後續事件會受短暫退避限制，不逐 token 重讀。
					retryAfterByChat.set(chatId, Date.now() + retryDelayMs);
				} finally {
					refreshByChat.delete(chatId);
				}
			})();
			refreshByChat.set(chatId, refresh);
		}

		if (refresh) await refresh;
		return true;
	};
}
