"""Exercise the actual policy shell with real Git and the project interpreter."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from switchstand.affected_tests import foreground_authority, plan_exact, validate_execution_tree

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize('changed,history,enabled,event,expected_calls', [
    ('tests/test_example.py', 'healthy', 'true', 'push', 4),
    ('tests/test_example.py', 'unavailable', 'true', 'push', 1),
    ('README.md', 'healthy', 'true', 'push', 0),
    ('tests/test_example.py', 'healthy', 'false', 'push', 0),
    ('tests/test_example.py', 'healthy', 'true', 'schedule', 0),
])
def test_policy_skips_only_irrelevant_observations(
    tmp_path: Path, changed: str, history: str, enabled: str, event: str, expected_calls: int,
) -> None:
    def git(*args: str) -> str:
        return subprocess.check_output(
            ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', *args],
            cwd=tmp_path, text=True,
        ).strip()

    git('init', '-q')
    for path in ('.github/workflows/quality.yml', 'tests/test_example.py', 'README.md'):
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text('def test_example(): pass\n' if path.endswith('.py') else 'base\n')
    git('add', '.')
    git('commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/main', base)
    with (tmp_path / changed).open('a') as sink:
        sink.write('# change\n')
    git('add', '.')
    git('commit', '-qm', 'candidate')
    head = git('rev-parse', 'HEAD')
    tools = tmp_path / 'bin'
    tools.mkdir()
    (tools / 'python3').symlink_to(sys.executable)
    # Only the provider boundary is substituted; Git and the workflow run unmodified.
    gh = tools / 'gh'
    gh.write_text(textwrap.dedent('''\
        #!/bin/bash
        echo call >> "$RUNNER_TEMP/gh-calls"
        test "$HISTORY" != unavailable || exit 1
        if [[ "$*" == *total_count* ]]; then
          if [[ "$*" == *event=schedule* ]]; then echo 0; else echo 1; fi
        elif [[ "$*" == *branch=main* ]]; then
          printf 'push\t%s\tsuccess\t2026-10-19T12:00:00+00:00\n' "$PUSH_BASE_SHA"
        fi
        '''))
    gh.chmod(0o755)
    workflow = (ROOT / '.github/workflows/quality.yml').read_text()
    policy = textwrap.dedent(workflow.split('        run: |\n', 1)[1].split('\n  quality_execution:', 1)[0])
    environment = os.environ | {
        'PATH': f'{tools}:{os.environ["PATH"]}', 'PYTHONPATH': str(ROOT / 'src'),
        'RUNNER_TEMP': str(tmp_path), 'GITHUB_OUTPUT': str(tmp_path / 'outputs'),
        'GITHUB_REPOSITORY': 'owner/repo', 'EVENT_KIND': event, 'EVENT_SHA': head,
        'PUSH_BASE_SHA': base, 'PUSH_FORCED': 'false', 'DEFAULT_BRANCH': 'main',
        'REF_NAME': 'topic', 'SELECTIVE_FOREGROUND_ENABLED': enabled, 'HISTORY': history,
    }
    # The workflow explicitly sets PYTHONPATH=src; point it at the actual planner.
    (tmp_path / 'src').symlink_to(ROOT / 'src', target_is_directory=True)
    result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', policy], cwd=tmp_path,
                            env=environment, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    health = expected_calls == 4
    plan = validate_execution_tree(tmp_path, plan_exact(tmp_path, base, head), head)
    expected = foreground_authority(plan, subject_verified=True, selector_health_clear=health)
    assert summary['authority'] == expected.mode
    assert summary['authority_reasons'] == list(expected.reasons)
    assert summary['planner'] == {key: value for key, value in json.loads(
        (tmp_path / 'exact-quality-plan.json').read_text(),
    ).items() if key != 'reasons_by_test' and key != 'basis_kind'}
    assert summary['planner']['head'] == head
    calls = tmp_path / 'gh-calls'
    assert (len(calls.read_text().splitlines()) if calls.exists() else 0) == expected_calls
    if expected_calls == 0:
        assert summary['selector_health_observation'] == 'UNKNOWN'
        assert summary['adaptive_full_suite_shadow']['status'] == 'UNKNOWN'
        assert summary['adaptive_full_suite_shadow']['reasons'] == ['observations-skipped-full-fallback']

