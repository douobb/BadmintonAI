"""為固定 Open WebUI v0.11.3 安裝評測聊天室原生同步與安全階段紀錄。"""

from __future__ import annotations

import shutil
from pathlib import Path

PATCH_DIR = Path(__file__).resolve().parent
FRONTEND_ROOT = Path("/app")
BACKEND_ROOT = Path("/app/backend/open_webui")

CHAT_COMPONENT = Path("src/lib/components/chat/Chat.svelte")
CHAT_SYNC_MODULE = Path("src/lib/utils/evaluation_message_sync.js")

CHAT_IMPORT_ANCHOR = "\timport { applyResponseStreamEvent, getOutputText } from './Messages/structuredOutput';"
CHAT_HANDLER_ANCHOR = "\tconst chatEventHandler = async (event, cb) => {"
CHAT_MESSAGE_ANCHOR = "\t\t\tlet message = history.messages[event.message_id];\n\n\t\t\tif (message) {"

CHAT_SYNC_IMPORT = "\n\timport { createEvaluationMessageSynchronizer } from '$lib/utils/evaluation_message_sync';"

CHAT_SYNC_FUNCTIONS = '''
	let evaluationMessageSynchronizer;
	const mergeEvaluationMessageBranch = (remoteChat, requestedMessageId) => {
		const remoteHistory = remoteChat?.chat?.history ?? remoteChat?.history;
		const remoteMessages = remoteHistory?.messages;
		if (!remoteMessages || typeof remoteMessages !== 'object') return false;

		const branch = [];
		const visited = new Set();
		let cursor = remoteMessages[requestedMessageId];
		while (
			cursor?.id &&
			!history.messages[cursor.id] &&
			!visited.has(cursor.id) &&
			branch.length < 10000
		) {
			visited.add(cursor.id);
			branch.unshift(cursor);
			cursor = cursor.parentId ? remoteMessages[cursor.parentId] : null;
		}
		if (branch.length === 0) return Boolean(history.messages[requestedMessageId]);

		const anchorId = branch[0].parentId;
		if (anchorId && !history.messages[anchorId]) return false;
		const messages = { ...history.messages };
		if (anchorId) {
			const anchor = messages[anchorId];
			messages[anchorId] = {
				...anchor,
				childrenIds: [...new Set([...(anchor.childrenIds ?? []), branch[0].id])]
			};
		}
		for (const remoteMessage of branch) {
			if (!messages[remoteMessage.id]) {
				messages[remoteMessage.id] = {
					...remoteMessage,
					childrenIds: [...new Set(remoteMessage.childrenIds ?? [])]
				};
			}
		}

		if (!history.currentId || history.currentId === anchorId) {
			const remoteCurrentId = remoteHistory.currentId;
			history.currentId = branch.some((message) => message.id === remoteCurrentId)
				? remoteCurrentId
				: requestedMessageId;
		}
		history.messages = messages;
		history = { ...history, messages };
		return Boolean(history.messages[requestedMessageId]);
	};
'''

CHAT_SYNC_UNKNOWN_MESSAGE = '''			let message = history.messages[event.message_id];
			if (!message && !event?.__evaluationMessageReplay) {
				evaluationMessageSynchronizer ??= createEvaluationMessageSynchronizer({
					getActiveChatId: () => $chatId,
					getMessage: (messageId) => history.messages[messageId],
					fetchChat: (activeChatId) => getChatById(localStorage.token, activeChatId),
					mergeBranch: mergeEvaluationMessageBranch,
					dispatchEvent: (replayedEvent, callback) => chatEventHandler(replayedEvent, callback)
				});
				if (await evaluationMessageSynchronizer(event, cb)) return;
				message = history.messages[event.message_id];
			}

			if (message) {'''


def _replace_once(source: str, original: str, replacement: str, label: str) -> str:
	"""對固定上游片段做唯一替換，遇到漂移即停止建置。"""
	if replacement in source:
		return source
	if source.count(original) != 1:
		raise RuntimeError(f"Open WebUI v0.11.3 原始碼錨點不符：{label}")
	return source.replace(original, replacement, 1)


def patch_chat_source(source: str) -> str:
	source = _replace_once(
		source,
		CHAT_IMPORT_ANCHOR,
		CHAT_IMPORT_ANCHOR + CHAT_SYNC_IMPORT,
		"Chat.svelte 評測同步 import",
	)
	source = _replace_once(
		source,
		CHAT_HANDLER_ANCHOR,
		CHAT_SYNC_FUNCTIONS + "\n" + CHAT_HANDLER_ANCHOR,
		"Chat.svelte 評測同步函式",
	)
	return _replace_once(
		source,
		CHAT_MESSAGE_ANCHOR,
		CHAT_SYNC_UNKNOWN_MESSAGE,
		"Chat.svelte 未知訊息分支",
	)


def install_frontend(frontend_root: Path = FRONTEND_ROOT) -> None:
	"""補丁目前開啟聊天室；既有 native event handler 保持唯一串流入口。"""
	component = frontend_root / CHAT_COMPONENT
	helper = frontend_root / CHAT_SYNC_MODULE
	component.parent.mkdir(parents=True, exist_ok=True)
	helper.parent.mkdir(parents=True, exist_ok=True)
	patched = patch_chat_source(component.read_text(encoding="utf-8"))
	component.write_text(patched, encoding="utf-8")
	shutil.copyfile(PATCH_DIR / "evaluation_message_sync.js", helper)


def _replace_in_file(path: Path, original: str, replacement: str, label: str) -> None:
	source = path.read_text(encoding="utf-8")
	updated = _replace_once(source, original, replacement, label)
	path.write_text(updated, encoding="utf-8")


def patch_main_source(source: str) -> str:
	source = _replace_once(
		source,
		"from open_webui.utils.misc import get_response_error_detail, merge_model_params",
		"from open_webui.utils.misc import get_response_error_detail, merge_model_params\nfrom open_webui.evaluation_observability import (\n    is_evaluation_metadata,\n    evaluation_failure_visible_content,\n    log_evaluation_stage,\n    safe_evaluation_error,\n)",
		"main.py safe helper import",
	)
	source = _replace_once(
		source,
		"            'assistant_message_id': form_data.pop('assistant_message_id', None),",
		"\n".join(
			[
				"            'assistant_message_id': form_data.pop('assistant_message_id', None),",
				"            'badmintonai_evaluation': form_data.pop('badmintonai_evaluation', False) is True,",
				"            'badmintonai_operation_id': form_data.pop('badmintonai_operation_id', None),",
			]
		),
		"main.py evaluator correlation metadata",
	)
	start = source.index("    async def process_chat(request, form_data, user, metadata, model, tasks=None):")
	end = source.index("\n    # Fan out: one task per model", start)
	function = source[start:end]
	provider_error_anchors = [
		"\n".join(
			[
				"            if isinstance(response, JSONResponse) and response.status_code >= 400:",
				"                raise Exception(get_response_error_detail(response))",
			]
		),
		"\n".join(
			[
				"            if isinstance(response, Response) and response.status_code >= 400:",
				"                raise Exception(get_response_error_detail(response))",
			]
		),
	]
	provider_status_replacement = "\n".join(
		[
			"            if isinstance(response, Response) and response.status_code >= 400:",
			"                raise HTTPException(",
			"                    status_code=response.status_code,",
			"                    detail=get_response_error_detail(response),",
			"                )",
		]
	)
	if provider_status_replacement not in function:
		matching_provider_anchors = [
			anchor for anchor in provider_error_anchors if function.count(anchor) == 1
		]
		if len(matching_provider_anchors) != 1:
			raise RuntimeError(
				"Open WebUI 原始碼與已驗證的 v0.11.3 不符：main.py provider status preservation"
			)
		function = _replace_once(
			function,
			matching_provider_anchors[0],
			provider_status_replacement,
			"main.py provider status preservation",
		)
	preprocessing_replacement = "\n".join(
		[
			"            preprocessing_started_at = time.perf_counter()",
			"            if is_evaluation_metadata(metadata):",
			"                log_evaluation_stage(log, 'preprocessing_start', metadata)",
			"            try:",
			"                form_data, metadata, events = await process_chat_payload(request, form_data, user, metadata, model)",
			"            except Exception as error:",
			"                if is_evaluation_metadata(metadata):",
			"                    log_evaluation_stage(",
			"                        log,",
			"                        'preprocessing_error',",
			"                        metadata,",
			"                        duration_ms=int((time.perf_counter() - preprocessing_started_at) * 1000),",
			"                        exception_type=type(error).__name__,",
			"                    )",
			"                raise",
			"            if is_evaluation_metadata(metadata):",
			"                log_evaluation_stage(",
			"                    log,",
			"                    'preprocessing_done',",
			"                    metadata,",
			"                    duration_ms=int((time.perf_counter() - preprocessing_started_at) * 1000),",
			"                )",
		]
	)
	function = _replace_once(
		function,
		"            form_data, metadata, events = await process_chat_payload(request, form_data, user, metadata, model)",
		preprocessing_replacement,
		"main.py preprocessing stages",
	)
	persistence_replacement = "\n".join(
		[
			"            persistence_started_at = time.perf_counter()",
			"            result = await process_chat_response(response, ctx)",
			"            if is_evaluation_metadata(metadata):",
			"                log_evaluation_stage(",
			"                    log,",
			"                    'chat_persistence_complete',",
			"                    metadata,",
			"                    duration_ms=int((time.perf_counter() - persistence_started_at) * 1000),",
			"                    outcome='success',",
			"                )",
			"            return result",
		]
	)
	function = _replace_once(
		function,
		"            return await process_chat_response(response, ctx)",
		persistence_replacement,
		"main.py successful persistence stage",
	)
	error_replacement = "\n".join(
		[
			"        except Exception as e:",
			"            evaluation_request = is_evaluation_metadata(metadata)",
			"            error_detail = safe_evaluation_error(e) if evaluation_request else (",
			"                e.detail if isinstance(e, HTTPException) else str(e)",
			"            )",
			"            evaluation_existing_message = None",
			"            evaluation_message_read = False",
			"            if evaluation_request:",
			"                try:",
			"                    evaluation_existing_message = await Chats.get_message_by_id_and_message_id(",
			"                        metadata.get('chat_id'),",
			"                        metadata.get('message_id') or metadata.get('assistant_message_id'),",
			"                    )",
			"                    evaluation_message_read = evaluation_existing_message is not None",
			"                except Exception:",
			"                    pass",
			"            evaluation_visible_message = evaluation_failure_visible_content(",
			"                form_data, metadata, error_detail,",
			"                existing_message=evaluation_existing_message,",
			"                existing_message_known=evaluation_message_read,",
			"            ) if evaluation_request else None",
			"            if evaluation_request:",
			"                log_evaluation_stage(",
			"                    log,",
			"                    'chat_processing_error',",
			"                    metadata,",
			"                    exception_type=type(e).__name__,",
			"                )",
			"            else:",
			"                log.error('Error processing chat payload: %s', error_detail)",
		]
	)
	function = _replace_once(
		function,
		"\n".join(
			[
				"        except Exception as e:",
				"            error_detail = e.detail if isinstance(e, HTTPException) else str(e)",
				"            log.error('Error processing chat payload: %s', error_detail)",
			]
		),
		error_replacement,
		"main.py safe evaluation error selection",
	)
	function = _replace_once(
		function,
		"\n".join(["                                'error': {'content': error_detail},", "                            },"]),
		"\n".join(
			[
				"                                'error': {'content': error_detail},",
				"                                **({'done': True} if evaluation_request else {}),",
				"                                **({'content': evaluation_visible_message} if evaluation_visible_message else {}),",
				"                            },",
			]
		),
		"main.py terminal evaluation error persistence",
	)
	function = _replace_once(
		function,
		"\n".join(
			[
				"                        )",
				"",
				"                    event_emitter = await get_event_emitter(metadata)",
				"                    if event_emitter:",
			]
		),
		"\n".join(
			[
				"                        )",
				"                        if evaluation_request:",
				"                            log_evaluation_stage(log, 'chat_persistence_complete', metadata, outcome='error')",
				"                    event_emitter = await get_event_emitter(metadata)",
				"                    if event_emitter:",
			]
		),
		"main.py failed persistence stage",
	)
	function = _replace_once(
		function,
		"\n".join(
			[
				"                        await event_emitter(",
				"                            {",
				"                                'type': 'chat:message:error',",
				"                                'data': {'error': {'content': error_detail}},",
				"                            }",
				"                        )",
				"                        await event_emitter(",
				"                            {'type': 'chat:tasks:cancel'},",
				"                        )",
			]
		),
		"\n".join(
			[
				"                        await event_emitter(",
				"                            {",
				"                                'type': 'chat:message:error',",
				"                                'data': {'error': {'content': error_detail}},",
				"                            }",
				"                        )",
				"                        if evaluation_request:",
				"                            completion_data = {",
				"                                'done': True,",
				"                                'error': {'content': error_detail},",
				"                            }",
				"                            if evaluation_visible_message:",
				"                                completion_data['output'] = [{",
				"                                    'type': 'message',",
				"                                    'id': metadata.get('assistant_message_id') or metadata.get('message_id'),",
				"                                    'status': 'completed',",
				"                                    'role': 'assistant',",
				"                                    'content': [{'type': 'output_text', 'text': evaluation_visible_message}],",
				"                                }]",
				"                            await event_emitter({'type': 'chat:completion', 'data': completion_data})",
				"                        await event_emitter(",
				"                            {'type': 'chat:tasks:cancel'},",
				"                        )",
			]
		),
		"main.py evaluation terminal completion event",
	)
	return source[:start] + function + source[end:]



def patch_middleware_source(source: str) -> str:
	"""讓成功澄清與明確終止錯誤結束工具循環，並補齊未執行呼叫。"""
	source = _replace_once(
		source,
		"from open_webui.utils.ask_user import stage_ask_user_tool_calls",
		"\n".join(
			[
				"from open_webui.utils.ask_user import stage_ask_user_tool_calls",
				"from open_webui.evaluation_observability import (",
				"    EvaluationToolBatchGate,",
				"    EvaluationToolProgressGuard,",
				"    is_evaluation_metadata,",
				"    finish_successful_request_clarification,",
				"    finish_no_progress_final_failure,",
				"    finish_terminal_tool_turn,",
				"    finish_tool_iteration_limit_turn,",
				"    has_new_assistant_text,",
				"    decide_tool_batch_completion,",
				"    NO_PROGRESS_FINAL_PROMPT,",
				"    NO_PROGRESS_FINAL_FAILURE_MESSAGE,",
				"    NO_PROGRESS_FINAL_TOOL_INTENT_MESSAGE,",
				"    ANALYSIS_BUDGET_EXHAUSTED_MESSAGE,",
				"    ANALYSIS_BUDGET_RESERVED_MESSAGE,",
				"    TOOL_ITERATION_LIMIT_MESSAGE,",
				"    terminalize_pending_tool_calls,",
				"    is_badmintonai_stream_failure,",
				"    badmintonai_stream_failure_message,",
				")",
			]
		),
		"middleware.py evaluation helper import",
	)
	source = _replace_once(
		source,
		"            response_stream_task_id = metadata.get('task_id') or metadata.get('message_id')",
		"            response_stream_task_id = metadata.get('task_id') or metadata.get('message_id')\n"
		"            evaluation_request = is_evaluation_metadata(metadata)\n"
		"            tool_iteration_limit_reached = False\n"
		"            no_progress_final_sent = False\n"
		"            no_progress_final_failed = False\n"
			"            tool_turn_failed = is_badmintonai_stream_failure(metadata)",
		"middleware.py evaluation request state",
	)
	source = _replace_once(
		source,
		"            async def emit_message_error(error_content):\n                if save_to_chat:",
		"\n".join(
			[
				"            async def emit_message_error(error_content):",
				"                nonlocal no_progress_final_failed, tool_turn_failed",
				"                if no_progress_final_sent and not no_progress_final_failed and not is_badmintonai_stream_failure(metadata):",
				"                    finish_no_progress_final_failure(",
				"                        output, tool_calls,",
				"                        message=NO_PROGRESS_FINAL_FAILURE_MESSAGE,",
				"                        message_id_factory=lambda: output_id('msg'),",
				"                        result_id_factory=lambda: output_id('fco'),",
				"                    )",
				"                    no_progress_final_failed = True",
				"                    tool_turn_failed = True",
				"                    await event_emitter({'type': 'chat:completion', 'data': {'output': full_output()}})",
				"                terminalize_pending_tool_calls(",
				"                    output,",
				"                    tool_calls,",
				"                    reason='工具呼叫因本輪錯誤而未執行。',",
				"                    event_reason='turn_error',",
				"                    id_factory=lambda: output_id('fco'),",
				"                )",
				"                if save_to_chat:",
			]
		),
		"middleware.py terminalize calls on error",
	)
	execute_tool_anchor = "\n".join(
		[
			"                    async def execute_tool_call(tool_call):",
			"                        name = tool_call.get('function', {}).get('name', '')",
		]
	)
	source = _replace_once(
		source,
		execute_tool_anchor,
		"                    tool_batch_gate = EvaluationToolBatchGate(tool_progress_guard)\n"
		+ execute_tool_anchor
		+ "\n"
		+ "                        if not tool_batch_gate.should_execute(tool_call):\n"
		+ "                            return {}, None, None, None, False",
		"middleware.py execution-stage terminal gate",
	)
	loop_anchor = "                while tool_calls and ("
	source = _replace_once(
		source,
		loop_anchor,
		"                async def handle_badmintonai_stream_failure():\n"
		"                    nonlocal tool_turn_failed\n"
		"                    if not is_badmintonai_stream_failure(metadata):\n"
		"                        return False\n"
		"                    tool_turn_failed = True\n"
		"                    terminalize_pending_tool_calls(\n"
		"                        output, tool_calls,\n"
		"                        reason=badmintonai_stream_failure_message(metadata),\n"
		"                        event_reason='stream_failure',\n"
		"                        id_factory=lambda: output_id('fco'),\n"
		"                    )\n"
		"                    tool_calls.clear()\n"
		"                    await emit_message_error(badmintonai_stream_failure_message(metadata))\n"
		"                    return True\n"
		"\n"
		"                await handle_badmintonai_stream_failure()\n"
		"                tool_progress_guard = EvaluationToolProgressGuard()\n"
		+ loop_anchor,
		"middleware.py shared stream failure completion",
	)
	source = _replace_once(
		source,
		"                    tool_call_iterations += 1\n\n                    response_tool_calls = tool_calls.pop(0)",
		"                    if no_progress_final_sent and tool_calls:\n"
		"                        finish_no_progress_final_failure(\n"
		"                            output, tool_calls,\n"
		"                            message=NO_PROGRESS_FINAL_TOOL_INTENT_MESSAGE,\n"
		"                            message_id_factory=lambda: output_id('msg'),\n"
		"                            result_id_factory=lambda: output_id('fco'),\n"
		"                        )\n"
		"                        no_progress_final_failed = True\n"
		"                        tool_turn_failed = True\n"
		"                        await event_emitter({'type': 'chat:completion', 'data': {'output': full_output()}})\n"
		"                        await emit_message_error(NO_PROGRESS_FINAL_TOOL_INTENT_MESSAGE)\n"
		"                        break\n"
		"                    tool_call_iterations += 1\n\n                    response_tool_calls = tool_calls.pop(0)",
		"middleware.py refuse tools after the one-shot final",
	)
	source = _replace_once(
		source,
		"                        return params, result, tool, tool_type, direct_tool",
		"                        tool_batch_gate.observe_result(name, result)\n"
						"                        tool_progress_guard.observe_result(name, result)\n"
		"                        return params, result, tool, tool_type, direct_tool",
		"middleware.py observe tool result before the next native call",
	)
	result_loop_anchor = "\n".join(
		[
			"                    for tool_call in response_tool_calls:",
				"                        tool_call_id = tool_call.get('id', '')",
				"                        tool_function_name = tool_call.get('function', {}).get('name', '')",
				"                        tool_function_params, tool_result, tool, tool_type, direct_tool = tool_results[id(tool_call)]",
		]
	)
	source = _replace_once(
		source,
		result_loop_anchor,
		"\n".join(
			[
				"                    clarification_message = tool_batch_gate.clarification_message",
				"                    terminal_tool_message = tool_batch_gate.terminal_message",
				"                    unexecuted_terminal_calls = tool_batch_gate.unexecuted_calls",
				"                    unexecuted_budget_calls = tool_batch_gate.budget_unexecuted_calls",
				"                    for tool_call in response_tool_calls:",
				"                        if tool_batch_gate.was_unexecuted(tool_call):",
				"                            continue",
				"                        tool_call_id = tool_call.get('id', '')",
				"                        tool_function_name = tool_call.get('function', {}).get('name', '')",
				"                        tool_function_params, tool_result, tool, tool_type, direct_tool = tool_results[id(tool_call)]",
			]
		),
	"middleware.py clarification result scope",
	)
	no_progress_decision_anchor = "                    frontend_output = []\n                    for item in full_output():"
	if "tool_batch_action = decide_tool_batch_completion(" not in source:
		source = _replace_once(
			source,
			no_progress_decision_anchor,
			"                    near_iteration_limit = (\n"
			"                        max_tool_call_iterations is not None\n"
			"                        and tool_call_iterations >= max_tool_call_iterations - 1\n"
			"                    )\n"
			"                    tool_batch_action = decide_tool_batch_completion(\n"
			"                        clarification_message=clarification_message,\n"
			"                        terminal_message=terminal_tool_message,\n"
			"                        pending_tool_calls=tool_calls,\n"
			"                        progress_guard=tool_progress_guard,\n"
			"                        near_iteration_limit=near_iteration_limit,\n"
			"                        render_attempted=tool_batch_gate.executed_render_attempt,\n"
			"                    )\n"
			"                    no_progress_final_requested = tool_batch_action == 'safe_final'\n"
			"                    if no_progress_final_requested:\n"
			"                        tool_progress_guard.mark_final_attempted()\n"
			"                    if unexecuted_budget_calls:\n"
			"                        terminalize_pending_tool_calls(\n"
			"                            output, [unexecuted_budget_calls],\n"
			"                            reason=ANALYSIS_BUDGET_RESERVED_MESSAGE,\n"
			"                            event_reason='analysis_budget_reserved',\n"
			"                            id_factory=lambda: output_id('fco'),\n"
			"                        )\n"
			"                    if tool_batch_action == 'analysis_budget_failed':\n"
			"                        terminal_tool_message = ANALYSIS_BUDGET_EXHAUSTED_MESSAGE\n"
			"                        tool_turn_failed = True\n"
			"                    frontend_output = []\n                    for item in full_output():",
			"middleware.py decide no-progress after whole tool batch",
		)
	source = _replace_once(
		source,
		"                    frontend_output = []\n                    for item in full_output():",
		"                    if isinstance(tool_calls, list) and unexecuted_terminal_calls:\n"
		"                        tool_calls.insert(0, unexecuted_terminal_calls)\n"
		"                    terminal_finished = False\n"
		"                    if terminal_tool_message is not None:\n"
		"                        terminal_finished = finish_terminal_tool_turn(\n"
		"                            output,\n"
		"                            terminal_tool_message,\n"
		"                            tool_calls,\n"
		"                            message_id_factory=lambda: output_id('msg'),\n"
		"                            result_id_factory=lambda: output_id('fco'),\n"
		"                        )\n"
		"                    clarification_finished = False\n"
		"                    if clarification_message is not None and terminal_tool_message is None:\n"
		"                        clarification_finished = finish_successful_request_clarification(\n"
		"                            output,\n"
		"                            clarification_message,\n"
		"                            tool_calls,\n"
		"                            message_id_factory=lambda: output_id('msg'),\n"
		"                            result_id_factory=lambda: output_id('fco'),\n"
		"                        )\n"
		"                    frontend_output = []\n                    for item in full_output():",
		"middleware.py visible clarification output",
	)
	continuation_anchor = "\n".join(
		[
			"                    await event_emitter(",
			"                        {",
			"                            'type': 'chat:completion',",
			"                            'data': {",
			"                                'output': frontend_output,",
			"                            },",
			"                        }",
			"                    )",
			"",
			"                    try:",
			"                        new_form_data = {",
		]
	)
	source = _replace_once(
		source,
		continuation_anchor,
		"\n".join(
			[
				"                    await event_emitter(",
				"                        {",
				"                            'type': 'chat:completion',",
				"                            'data': {",
				"                                'output': frontend_output,",
				"                            },",
				"                        }",
				"                    )",
				"",
				"                    if clarification_finished or terminal_finished:",
				"                        break",
				"",
				"                    try:",
				"                        new_form_data = {",
			]
		),
		"middleware.py clarification ends model loop",
	)
	final_request_anchor = "                        new_form_data = normalize_messages_for_model(new_form_data)\n\n                        res = await generate_chat_completion("
	source = _replace_once(
		source,
		final_request_anchor,
		"                        new_form_data = normalize_messages_for_model(new_form_data)\n"
		"                        if no_progress_final_requested:\n"
		"                            new_form_data['tool_choice'] = 'none'\n"
		"                            new_form_data['messages'].append({'role': 'user', 'content': NO_PROGRESS_FINAL_PROMPT})\n"
		"                            no_progress_prior_message_ids = {item.get('id') for item in full_output() if isinstance(item, dict) and item.get('type') == 'message' and item.get('role') == 'assistant'}\n"
		"                            no_progress_final_sent = True\n\n"
			"                        res = await generate_chat_completion(",
		"middleware.py one-shot tool-disabled final request",
	)
	stream_final_anchor = "                            output = []\n                            await stream_body_handler(res, new_form_data)\n                            output[:0] = prior_output"
	source = _replace_once(
		source,
		stream_final_anchor,
		"                            output = []\n"
		"                            await stream_body_handler(res, new_form_data)\n"
		"                            output[:0] = prior_output\n"
		"                            prior_output = []\n"
		"                            if await handle_badmintonai_stream_failure():\n"
		"                                break\n"
		"                            if no_progress_final_sent and not has_new_assistant_text(full_output(), no_progress_prior_message_ids):\n"
		"                                finish_no_progress_final_failure(\n"
		"                                    output, tool_calls, message=NO_PROGRESS_FINAL_FAILURE_MESSAGE,\n"
		"                                    message_id_factory=lambda: output_id('msg'),\n"
		"                                    result_id_factory=lambda: output_id('fco'),\n"
		"                                )\n"
				"                                no_progress_final_failed = True\n"
				"                                tool_turn_failed = True\n"
				"                                await event_emitter({'type': 'chat:completion', 'data': {'output': full_output()}})\n"
				"                                await emit_message_error(NO_PROGRESS_FINAL_FAILURE_MESSAGE)\n"
				"                                break\n",
		"middleware.py require visible one-shot final answer",
	)
	nonstream_final_anchor = "\n".join(
		[
			"                        elif getattr(res, 'status_code', 200) >= 400:",
			"                            await emit_message_error(get_message_error_content(get_response_error_detail(res)))",
			"                            break",
			"                        else:",
			"                            break",
			"                    except Exception as e:",
		]
	)
	source = _replace_once(
		source,
		nonstream_final_anchor,
		"\n".join(
			[
				"                        elif getattr(res, 'status_code', 200) >= 400:",
				"                            await emit_message_error(get_message_error_content(get_response_error_detail(res)))",
				"                            break",
				"                        else:",
				"                            if no_progress_final_sent:",
				"                                await emit_message_error(NO_PROGRESS_FINAL_FAILURE_MESSAGE)",
				"                            break",
				"                    except Exception as e:",
			]
		),
		"middleware.py fail a non-streaming one-shot final",
	)
	iteration_limit_anchor = "\n".join(
		[
			"                if (",
			"                    max_tool_call_iterations is not None",
			"                    and tool_calls",
			"                    and tool_call_iterations >= max_tool_call_iterations",
			"                ):",
			"                    log.warning('Tool-call iteration limit reached (%s)', max_tool_call_iterations)",
			"                    error_content = f'Tool-call limit reached ({max_tool_call_iterations} iterations).'",
			"                    await emit_message_error(error_content)",
		]
	)
	source = _replace_once(
		source,
		iteration_limit_anchor,
		"\n".join(
			[
				"                if no_progress_final_sent and tool_calls:",
				"                    finish_no_progress_final_failure(",
				"                        output, tool_calls,",
				"                        message=NO_PROGRESS_FINAL_TOOL_INTENT_MESSAGE,",
				"                        message_id_factory=lambda: output_id('msg'),",
				"                        result_id_factory=lambda: output_id('fco'),",
				"                    )",
				"                    no_progress_final_failed = True",
				"                    tool_turn_failed = True",
				"                    await event_emitter({'type': 'chat:completion', 'data': {'output': full_output()}})",
				"                    await emit_message_error(NO_PROGRESS_FINAL_TOOL_INTENT_MESSAGE)",
				"                elif (",
				"                    max_tool_call_iterations is not None",
				"                    and tool_calls",
				"                    and tool_call_iterations >= max_tool_call_iterations",
				"                ):",
				"                    log.warning('Tool-call iteration limit reached (%s)', max_tool_call_iterations)",
				"                    error_content = TOOL_ITERATION_LIMIT_MESSAGE",
				"                    finish_tool_iteration_limit_turn(",
				"                        output,",
				"                        tool_calls,",
				"                        message_id_factory=lambda: output_id('msg'),",
				"                        result_id_factory=lambda: output_id('fco'),",
				"                    )",
				"                    tool_iteration_limit_reached = True",
				"                    await event_emitter(",
				"                        {'type': 'chat:completion', 'data': {'output': full_output()}}",
				"                    )",
				"                    await emit_message_error(error_content)",
			]
		),
		"middleware.py user-visible tool iteration limit",
	)
	source = _replace_once(
		source,
		"                if DETECT_CODE_INTERPRETER:\n"
		"                    MAX_RETRIES = 5",
		"                if DETECT_CODE_INTERPRETER and not tool_iteration_limit_reached and not tool_turn_failed and not no_progress_final_sent:\n"
		"                    MAX_RETRIES = 5",
		"middleware.py stop code-interpreter continuation at tool limit",
	)
	source = _replace_once(
		source,
		"                # Mark all in-progress items as completed\n"
		"                for item in output:\n"
		"                    if item.get('status') == 'in_progress':\n"
		"                        item['status'] = 'completed'",
		"                # Preserve failed partial output after the native call limit.\n"
		"                for item in output:\n"
		"                    if item.get('status') == 'in_progress':\n"
		"                        item['status'] = 'failed' if tool_iteration_limit_reached or tool_turn_failed else 'completed'",
		"middleware.py preserve incomplete output at tool limit",
	)
	return source


def patch_openai_source(source: str) -> str:
	source = _replace_once(
		source,
		"import re\nfrom typing import Optional",
		"import re\nimport time\nfrom typing import Optional",
		"openai.py perf_counter import",
	)
	source = _replace_once(
		source,
		"from pydantic import BaseModel, ConfigDict\nfrom sqlalchemy.ext.asyncio import AsyncSession",
		"from pydantic import BaseModel, ConfigDict\nfrom sqlalchemy.ext.asyncio import AsyncSession\nfrom open_webui.evaluation_observability import (\n    is_evaluation_metadata,\n    is_badmintonai_stream_request,\n    badmintonai_stream_timeouts,\n    apply_badmintonai_stream_timeout,\n    guarded_badmintonai_stream,\n    badmintonai_stream_failure_body,\n    mark_badmintonai_stream_failure,\n    classify_badmintonai_request_failure,\n    log_badmintonai_stream_failure,\n    logged_evaluation_stream,\n    log_evaluation_stage,\n    safe_evaluation_error,\n    safe_http_error_message,\n)",
		"openai.py safe helper import",
	)
	start = source.index("@router.post('/chat/completions')")
	end = source.index("\nasync def embeddings(", start)
	function = source[start:end]
	function = _replace_once(
		function,
		"    metadata = payload.pop('metadata', None)\n",
		"    metadata = payload.pop('metadata', None)\n    evaluation_request = is_evaluation_metadata(metadata)\n    badmintonai_model_request = is_badmintonai_stream_request(form_data.get('model'), metadata)\n",
		"OpenAI completion evaluation marker",
	)
	function = _replace_once(
		function,
		"    is_streaming_request = bool(payload.get('stream', False))",
		"    is_streaming_request = bool(payload.get('stream', False))\n"
		"    badmintonai_stream_guard = bool(is_streaming_request and badmintonai_model_request)\n"
		"    badmintonai_timeouts = badmintonai_stream_timeouts() if badmintonai_stream_guard else None",
		"OpenAI completion BadmintonAI stream scope",
	)
	request_anchor = "\n".join(
		[
			"    try:",
			"        session = await get_session()",
			"",
			"        r = await session.request(",
			"            method='POST',",
			"            url=request_url,",
			"            data=payload,",
			"            headers=headers,",
			"            cookies=cookies,",
			"            ssl=AIOHTTP_CLIENT_SESSION_SSL,",
			"            timeout=get_client_timeout(stream=is_streaming_request),",
			"        )",
			"",
			"        # Check if response is SSE",
		]
	)
	request_replacement = "\n".join(
		[
			"    upstream_started_at = time.perf_counter()",
			"    badmintonai_stream_started_at = upstream_started_at if badmintonai_stream_guard else None",
			"    if evaluation_request:",
			"        log_evaluation_stage(log, 'upstream_start', metadata)",
			"    try:",
			"        session = await get_session()",
			"",
			"        r = await session.request(",
			"            method='POST',",
			"            url=request_url,",
			"            data=payload,",
			"            headers=headers,",
			"            cookies=cookies,",
			"            ssl=AIOHTTP_CLIENT_SESSION_SSL,",
			"            timeout=apply_badmintonai_stream_timeout(get_client_timeout(stream=True), badmintonai_timeouts) if badmintonai_stream_guard else get_client_timeout(stream=is_streaming_request),",
			"        )",
			"        if evaluation_request:",
			"            log_evaluation_stage(",
			"                log,",
			"                'upstream_headers',",
			"                metadata,",
			"                duration_ms=int((time.perf_counter() - upstream_started_at) * 1000),",
			"                http_status=r.status,",
			"            )",
				"            if r.status >= 400:",
				"                return JSONResponse(",
				"                    status_code=r.status,",
				"                    content={'error': {'message': safe_http_error_message(r.status), 'code': r.status}},",
				"                )",
				"            if is_streaming_request and 'text/event-stream' not in r.headers.get('Content-Type', ''):",
				"                log_evaluation_stage(",
				"                    log, 'upstream_error', metadata, http_status=502, outcome='unexpected_content_type'",
				"                )",
				"                return JSONResponse(",
				"                    status_code=502,",
				"                    content={'error': {'message': '模型服務未回傳預期串流；本輪已記錄失敗。', 'code': 502}},",
				"                )",
				"        if badmintonai_stream_guard and r.status < 400 and 'text/event-stream' not in r.headers.get('Content-Type', ''):",
				"            code = 'stream_upstream_error'",
				"            mark_badmintonai_stream_failure(metadata, code)",
				"            log_badmintonai_stream_failure(log, metadata, code, duration_ms=int((time.perf_counter() - upstream_started_at) * 1000), character_count=0)",
				"            return StreamingResponse(badmintonai_stream_failure_body(code), status_code=200, media_type='text/event-stream')",
				"        # Check if response is SSE",
		]
	)
	function = _replace_once(
		function,
		request_anchor,
		request_replacement,
		"OpenAI completion upstream start/headers",
	)
	function = _replace_once(
		function,
		"                stream_wrapper(r),\n                status_code=r.status,",
		"                guarded_badmintonai_stream(r, metadata, badmintonai_stream_started_at, stream_wrapper, log, badmintonai_timeouts)\n                if badmintonai_stream_guard\n                else logged_evaluation_stream(r, metadata, upstream_started_at, stream_wrapper, log)\n                if evaluation_request\n                else stream_wrapper(r),\n                status_code=r.status,",
		"OpenAI completion native stream wrapper",
	)
	except_original = "\n".join(
		[
			"    except Exception as e:",
			"        log.exception(e)",
			"",
			"        raise HTTPException(",
			"            status_code=r.status if r else 500,",
			"            detail=ERROR_MESSAGES.SERVER_CONNECTION_ERROR,",
			"        )",
		]
	)
	except_replacement = "\n".join(
		[
			"    except Exception as e:",
			"        if badmintonai_stream_guard and isinstance(e, HTTPException) and not evaluation_request:",
			"            raise",
			"        if badmintonai_stream_guard and not isinstance(e, HTTPException):",
			"            duration_seconds = time.perf_counter() - upstream_started_at",
			"            code = classify_badmintonai_request_failure(duration_seconds, badmintonai_timeouts)",
			"            mark_badmintonai_stream_failure(metadata, code)",
			"            log_badmintonai_stream_failure(log, metadata, code, duration_ms=int(duration_seconds * 1000), character_count=0, exception_type=type(e).__name__)",
			"            return StreamingResponse(badmintonai_stream_failure_body(code), status_code=200, media_type='text/event-stream')",
			"        if evaluation_request:",
			"            log_evaluation_stage(",
			"                log,",
			"                'upstream_error',",
			"                metadata,",
			"                duration_ms=int((time.perf_counter() - upstream_started_at) * 1000),",
			"                http_status=r.status if r is not None else None,",
			"                exception_type=type(e).__name__,",
			"            )",
			"            is_timeout = isinstance(e, (asyncio.TimeoutError, TimeoutError)) or type(e).__name__ in {",
			"                'ServerTimeoutError', 'SocketTimeoutError'",
			"            }",
			"            raise HTTPException(",
			"                status_code=504 if is_timeout else 502,",
			"                detail=safe_evaluation_error(e),",
			"            ) from e",
			"        log.exception(e)",
			"",
			"        raise HTTPException(",
			"            status_code=r.status if r else 500,",
			"            detail=ERROR_MESSAGES.SERVER_CONNECTION_ERROR,",
			"        )",
		]
	)
	function = _replace_once(
		function,
		except_original,
		except_replacement,
		"OpenAI completion safe exception",
	)
	return source[:start] + function + source[end:]


def install_backend(backend_root: Path = BACKEND_ROOT) -> None:
	"""加上評測限定的 upstream stage log 與持久化安全失敗。"""
	shutil.copyfile(PATCH_DIR / "evaluation_observability.py", backend_root / "evaluation_observability.py")
	openai = backend_root / "routers" / "openai.py"
	openai.write_text(patch_openai_source(openai.read_text(encoding="utf-8")), encoding="utf-8")

	main = backend_root / "main.py"
	main.write_text(patch_main_source(main.read_text(encoding="utf-8")), encoding="utf-8")

	middleware = backend_root / "utils" / "middleware.py"
	middleware.write_text(
		patch_middleware_source(middleware.read_text(encoding="utf-8")),
		encoding="utf-8",
	)


if __name__ == "__main__":
	import argparse

	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--frontend", action="store_true")
	parser.add_argument("--backend", action="store_true")
	args = parser.parse_args()
	if args.frontend == args.backend:
		parser.error("必須且只能指定 --frontend 或 --backend")
	if args.frontend:
		install_frontend()
	else:
		install_backend()
