"""Exercise the detached launcher boundary without starting a real worker."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest


@pytest.mark.parametrize('admitted', [True, False])
def test_background_worker_policy_and_admission(tmp_path, admitted):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    source = Path(__file__).parents[1] / 'scripts/codex-job'
    shutil.copy(source, scripts / 'codex-job')
    admission = scripts / 'agent-resource-admission'
    admission.write_text(f'#!/bin/sh\nexit {0 if admitted else 1}\n')
    admission.chmod(0o755)
    record = tmp_path / 'invocation.json'
    systemd = scripts / 'systemd-run'
    systemd.write_text(
        '#!/usr/bin/env python3\nimport json, os, pathlib, sys\n'
        f'pathlib.Path({str(record)!r}).write_text(json.dumps({{'
        '"args": sys.argv[1:], "home": os.environ["CODEX_HOME"], '
        '"prompt": sys.stdin.read(), "session": os.getsid(0), "pid": os.getpid()}))\n'
    )
    systemd.chmod(0o755)
    prompt = tmp_path / 'worker prompt.txt'
    prompt.write_text('Only do the assigned step.\n')
    worktree = tmp_path / 'worker tree'
    worktree.mkdir()
    result = subprocess.run(
        [str(scripts / 'codex-job'), 'test', '128', str(worktree), str(prompt),
         str(tmp_path / 'worker.log')],
        env={**os.environ, 'PATH': f'{scripts}:{os.environ["PATH"]}', 'HOME': str(tmp_path)},
        capture_output=True, text=True, timeout=5, check=False,
    )
    if not admitted:
        assert result.returncode != 0
        assert not record.exists()
        assert 'started' not in result.stdout
        return
    assert result.returncode == 0, result.stderr
    deadline = time.monotonic() + 5
    while not record.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    invocation = json.loads(record.read_text())
    assert invocation['home'] == str(tmp_path / '.local/state/switchstand/codex-bg-home')
    assert invocation['args'][:4] == ['--user', '--scope', '--quiet', '--slice=agents.slice']
    assert invocation['args'][4].startswith('--unit=ss-codex-test-')
    assert invocation['args'][5:] == [
        'codex', 'exec', '--dangerously-bypass-approvals-and-sandbox', '-C', str(worktree), '-',
    ]
    assert invocation['prompt'] == prompt.read_text()
    assert invocation['session'] == invocation['pid']
