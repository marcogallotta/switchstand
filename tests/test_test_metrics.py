from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from switchstand.test_metrics import collect

ROOT = Path(__file__).parents[1]


def test_junit_counts_timings_and_bounded_order(tmp_path: Path):
    xml = tmp_path / 'junit.xml'
    xml.write_text('<testsuites><testsuite time="25">' + ''.join(
        f'<testcase classname="tests.a" name="test_{i:02}" time="1">'
        + ('<failure/>' if i == 0 else '<error/>' if i == 1 else '<skipped/>' if i == 2 else '')
        + '</testcase>' for i in range(25)) + '</testsuite></testsuites>')
    result = collect(xml, {'subject_sha': 'exact'}, 'full')
    assert result['metrics_status'] == 'AVAILABLE'
    data = result['pytest']
    assert [data[k] for k in ('test_count', 'failure_count', 'error_count', 'skipped_count')] == [25, 1, 1, 1]
    assert data['test_seconds'] == data['suite_seconds'] == 25
    assert len(data['slowest_tests']) == 20
    assert data['slowest_tests'][0]['test'] == 'tests.a::test_00'
    assert result['selection_ratio'] is None


@pytest.mark.parametrize('content', [None, '<broken', '<other/>', '<testsuite><testcase time="nan"/></testsuite>'])
def test_unavailable_never_substitutes_quality(tmp_path: Path, content: str | None):
    xml = tmp_path / 'junit.xml'
    if content is not None:
        xml.write_text(content)
    result = collect(xml, {'subject_sha': 'exact'}, 'full')
    assert result['metrics_status'] == 'METRICS_UNAVAILABLE'
    assert result['subject_sha'] == 'exact'
    assert 'quality_result' not in result


def test_real_cli_planner_identity_and_unknown_environment(tmp_path: Path):
    identity = tmp_path / 'identity.json'
    planner = tmp_path / 'planner.json'
    output = tmp_path / 'metrics.json'
    summary = tmp_path / 'summary'
    identity.write_text(json.dumps({'environment': {'python': None}, 'github': {
        'run_id': '17', 'run_attempt': '2', 'job': 'quality'}}))
    plan = {'planner_revision': 'v1', 'base': 'base', 'head': 'head', 'mode': 'SELECTED',
            'selected_tests': ['tests/a.py'], 'fallback_reasons': []}
    planner.write_text(json.dumps(plan))
    result = subprocess.run([os.sys.executable, ROOT / 'src/switchstand/test_metrics.py',
                             '--identity', identity, '--planner', planner, '--execution-kind', 'selected',
                             '--junit', tmp_path / 'missing', '--output', output],
                            env=os.environ | {'GITHUB_STEP_SUMMARY': str(summary)}, check=False)
    assert result.returncode == 0
    data = json.loads(output.read_text())
    assert data['planner'] == {k: plan[k] for k in ('planner_revision', 'base', 'head', 'mode')}
    assert data['github']['run_attempt'] == '2'
    assert data['environment_key'] is None
    assert data['selected_count'] == 1 and data['selection_ratio'] is None
    assert 'METRICS_UNAVAILABLE' in summary.read_text()


def test_environment_key_ignores_source_image_but_changes_with_runtime_packages(tmp_path: Path):
    identity = tmp_path / 'identity.json'
    output = tmp_path / 'metrics.json'
    keys = []
    for image, runtime in [('source-a', 'packages-a'), ('source-b', 'packages-a'), ('source-b', 'packages-b')]:
        identity.write_text(json.dumps({'candidate_image': image, 'environment': {'python': '3.14.4', 'runtime_packages': runtime}}))
        subprocess.run([os.sys.executable, ROOT / 'src/switchstand/test_metrics.py',
                        '--identity', identity, '--execution-kind', 'full',
                        '--junit', tmp_path / 'missing', '--output', output], check=True)
        keys.append(json.loads(output.read_text())['environment_key'])
    assert keys[0] == keys[1] and keys[1] != keys[2]


def test_workflow_preserves_authority_and_attempts():
    text = (ROOT / '.github/workflows/quality.yml').read_text()
    execution, remainder = text.split('  docker_lifecycle:')
    lifecycle, terminal = remainder.split('\n  quality:\n')
    assert 'schedule:\n    - cron:' in text
    assert "github.event_name == 'schedule' && 'scheduled exact-head'" in text
    assert "github.event_name == 'schedule' && 'Scheduled exact-head Quality'" in terminal
    assert "github.event_name == 'schedule' && 'Scheduled exact-head Docker lifecycle'" in lifecycle
    assert "ref: ${{ github.event_name == 'pull_request' && github.ref || github.sha }}" in execution
    assert "ref: ${{ github.event_name == 'pull_request' && github.ref || github.sha }}" in lifecycle
    assert execution.count('if: always()') == execution.count('continue-on-error: true') == 2
    authoritative = execution.split(
        '      - name: Authoritative proportional or full-fallback Quality'
    )[1].split('      - name: Collect')[0]
    assert 'continue-on-error' not in authoritative
    assert '-v "$PWD:/workspace:ro"' in authoritative
    assert '-v "$RUNNER_TEMP/test-metrics:/metrics"' in authoritative
    assert '--junitxml=/metrics/junit.xml' in authoritative
    assert 'shift 1 &&' in authoritative
    assert 'needs.plan.outputs.selected_tests' in authoritative
    assert '--planner "$METRICS_DIR/planner.json"' in execution
    assert '--method GET' in execution
    assert 'selector_health_clear(' in execution
    assert 'history_complete=history_complete' in execution
    assert 'test "${push_count:-1000}" -lt 1000' in execution
    assert 'test "${schedule_count:-1000}" -lt 1000' in execution
    assert 'test "$push_complete" = true' in execution
    assert 'test "$schedule_complete" = true' in execution
    assert '--paginate' in execution
    assert "if: needs.plan.outputs.mode != 'PROMOTE_TEST_MODULE_ONLY_V1'" in lifecycle
    assert 'if: always()' in terminal
    assert 'test "$PLAN_RESULT" = success' in terminal
    assert 'test "$QUALITY_RESULT" = success' in terminal
    assert 'test "$DOCKER_RESULT" = skipped' in terminal
    assert 'test "$DOCKER_RESULT" = success' in terminal
    assert "&& 'PR composition Quality'" in terminal
    assert "|| 'Exact-head Quality'" in terminal
    assert 'github.run_id }}-${{ github.run_attempt }}-quality' in execution
    assert 'retention-days: 90' in execution
    identity = ('scripts/verify-quality-composition', 'QUALITY_COMPOSITION_SHA',
                'QUALITY_SUBJECT_SHA', 'QUALITY_BASE_SHA', 'STACK_BASE_REF', 'STACK_POSITION')
    assert all(token in execution for token in identity)
    assert all(token in lifecycle for token in identity)
    assert 'SWITCHSTAND_REAL_DOCKER=1' in lifecycle
