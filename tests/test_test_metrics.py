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
    quality, lifecycle = text.split('  docker-lifecycle:')
    assert 'schedule:\n    - cron:' in text
    assert "github.event_name == 'schedule' && 'scheduled exact-head'" in text
    assert "github.event_name == 'schedule' && 'Scheduled exact-head Quality'" in quality
    assert "github.event_name == 'schedule' && 'Scheduled exact-head Docker lifecycle'" in lifecycle
    assert "ref: ${{ github.event_name == 'pull_request' && github.ref || github.sha }}" in quality
    assert "ref: ${{ github.event_name == 'pull_request' && github.ref || github.sha }}" in lifecycle
    assert quality.count('if: always()') == quality.count('continue-on-error: true') == 2
    authoritative = quality.split('      - name: Authoritative Quality')[1].split('      - name: Collect')[0]
    assert 'continue-on-error' not in authoritative
    assert '-v "$PWD:/workspace:ro"' in authoritative
    assert '-v "$RUNNER_TEMP/test-metrics:/metrics"' in authoritative
    assert '--junitxml=/metrics/junit.xml' in authoritative
    assert 'github.run_id }}-${{ github.run_attempt }}-quality' in quality
    assert 'retention-days: 90' in quality
    assert 'test "${parents[1]}" = "$QUALITY_SUBJECT_SHA"' in quality
    assert 'test "${parents[1]}" = "$QUALITY_SUBJECT_SHA"' in lifecycle
    assert 'SWITCHSTAND_REAL_DOCKER=1' in lifecycle
