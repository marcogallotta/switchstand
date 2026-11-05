"""Fresh startup must never attach the old coordinator thread."""
import asyncio
import runpy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize('fresh', [False, True])
def test_existing_thread_resume_is_explicit(tmp_path, monkeypatch, fresh):
    module = runpy.run_path(str(Path(__file__).parents[1] / 'recovery/coordinator/start'))
    start = module['start']
    thread_file = tmp_path / 'thread-id'
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
        'thread/start' if fresh else 'thread/resume', 'turn/start']
    expected_thread = 'new-thread' if fresh else 'old-thread'
    assert thread_file.read_text().strip() == expected_thread
    assert calls[-1][1]['threadId'] == expected_thread
    prompt = calls[-1][1]['input'][0]['text']
    assert prompt.index('40581c1e') < prompt.index('332857d1') < prompt.index('a8ec90d7')
