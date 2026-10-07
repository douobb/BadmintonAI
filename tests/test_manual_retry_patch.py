"""固定原生 Retry 補丁的分支保留、防重與漂移檢查。"""

import hashlib
import json
import shutil
import subprocess

import pytest

from openwebui_patch import apply_manual_retry as patch
from scripts.sync_analysis_skill import content_hash, update_payload


def test_native_retry_patch_preserves_original_callback_and_is_idempotent(monkeypatch):
    region = patch.START + "\n\t\tawait sendMessage(history, userMessage.id);\n\t};\n\n"
    monkeypatch.setattr(
        patch, "REGENERATE_SHA256", hashlib.sha256(region.encode()).hexdigest()
    )
    original = region + patch.END + "\n\t};"
    result = patch.patch_chat_source(original)
    assert "await sendMessage(history, userMessage.id);" in result
    assert patch.patch_chat_source(result) == result
    response = "\texport let regenerateResponse: Function;\n" + patch.ERROR_LINE
    changed = patch.patch_response_source(response)
    assert patch.patch_response_source(changed) == changed
    assert "await regenerateResponse(message)" in changed
    assert "!readOnly" in changed and "regenerate_response" in changed
    assert "disabled={badmintonaiRetryPending || !message.done" in changed
    with pytest.raises(RuntimeError, match="已變更"):
        patch.patch_chat_source(
            original.replace("await sendMessage", "await otherSend")
        )


def test_native_retry_callback_blocks_double_click_active_and_unknown_tasks():
    node = shutil.which("node")
    if not node:
        pytest.skip("本機沒有 Node")
    harness = """
const assert = require('node:assert/strict');
const wrapper = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
 let generating=false, taskIds=[], $chatId='chat-1', localStorage={token:'test'};
 let history={currentId:'failed', messages:{failed:{done:true}}};
 let calls=0, query=0, result={task_ids:[]}, reject=false;
 let toast={error(){}}, getTaskIdsByChatId=async () => {query++; if(reject) throw Error(); return result;};
 let nativeRegenerateResponse=async (message, prompt) => {calls++; assert.equal(message.id,'failed');};
 eval(wrapper + '\\nglobalThis.retry = regenerateResponse;');
 const failed={id:'failed',error:true,done:true};
 await Promise.all([retry(failed),retry(failed)]); assert.equal(calls,1);
 result={task_ids:['active']}; await retry(failed); assert.equal(calls,1);
 result={}; await retry(failed); assert.equal(calls,1);
 reject=true; await retry(failed); assert.equal(calls,1);
 generating=true; const before=query; await retry(failed); assert.equal(query,before);
 generating=false; await retry({...failed,done:false}); assert.equal(calls,1);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(
        [node, "-e", harness],
        input=json.dumps(patch.RETRY_WRAPPER),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("is_active", [False, True])
def test_skill_sync_changes_only_content_and_requires_reviewed_hash(is_active):
    live = {
        "id": "badminton-analysis",
        "content": "舊內容",
        "name": "羽球資料分析",
        "description": "原說明",
        "meta": {"icon": "原值"},
        "is_active": is_active,
        "access_grants": [{"private": True}],
    }
    payload = update_payload(live, "新內容", content_hash("舊內容"))
    assert payload == {
        key: value
        for key, value in {**live, "content": "新內容"}.items()
        if key != "access_grants"
    }
    with pytest.raises(ValueError, match="已變更"):
        update_payload(live, "新內容", "0" * 64)
    with pytest.raises(ValueError, match="ID"):
        update_payload({**live, "id": "other-skill"}, "新內容", content_hash("舊內容"))
