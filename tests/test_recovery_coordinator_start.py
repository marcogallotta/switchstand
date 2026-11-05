"""Coordinator startup pins policy for new and resumed threads."""
import asyncio
import runpy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize('fresh, existing', [(False, True), (True, True), (False, False)])
def test_existing_thread_resume_is_explicit(tmp_path, monkeypatch, fresh, existing):
    module = runpy.run_path(str(Path(__file__).parents[1] / 'recovery/coordinator/start'))
    start = module['start']
    thread_file = tmp_path / 'thread-id'
    if existing:
        thread_file.write_text('old-thread\n')
    calls = []

    async def call(method, params):
        calls.append((method, params))
        return {'thread': {'id': 'new-thread'}} if method == 'thread/start' else {}

    rpc = AsyncMock()
    rpc.call.side_effect = call
    monkeypatch.setitem(start.__globals__, 'THREAD_FILE', thread_file)
    monkeypatch.setitem(start.__globals__, 'DaemonConnection', lambda *args: rpc)
    asyncio.run(start(fresh=fresh))
    assert [method for method, _ in calls] == [
        'thread/start' if fresh or not existing else 'thread/resume', 'turn/start']
    expected_thread = 'new-thread' if fresh or not existing else 'old-thread'
    expected_params = {
        'cwd': module['ROOT'], 'sandbox': 'danger-full-access', 'approvalPolicy': 'never',
    }
    if existing and not fresh:
        expected_params.update(threadId='old-thread', excludeTurns=True)
    assert calls[0][1] == expected_params
    assert thread_file.read_text().strip() == expected_thread
    assert calls[-1][1]['threadId'] == expected_thread
    prompt = calls[-1][1]['input'][0]['text']
    assert prompt.index('40581c1e') < prompt.index('332857d1') < prompt.index('a8ec90d7')
    assert 'next approved step for every item without a running job' in prompt
    assert 'at most 3 jobs running' in prompt
    assert 'Ask Marco via claude-coordinator only' in prompt
    assert 'wait for mail' not in prompt
