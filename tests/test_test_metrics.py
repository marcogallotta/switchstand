from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from switchstand.qualification_lineage import (
    advisory_warning,
    qualification_attempt_observation,
    summarize_lineage,
)
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
    result = subprocess.run([sys.executable, ROOT / 'src/switchstand/test_metrics.py',
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
    keys: list[str | None] = []
    for image, runtime in [('source-a', 'packages-a'), ('source-b', 'packages-a'), ('source-b', 'packages-b')]:
        identity.write_text(json.dumps({'candidate_image': image, 'environment': {'python': '3.14.4', 'runtime_packages': runtime}}))
        subprocess.run([sys.executable, ROOT / 'src/switchstand/test_metrics.py',
                        '--identity', identity, '--execution-kind', 'full',
                        '--junit', tmp_path / 'missing', '--output', output], check=True)
        keys.append(json.loads(output.read_text())['environment_key'])
    assert keys[0] == keys[1] and keys[1] != keys[2]


def test_github_junit_identity_maps_to_advisory_attempt() -> None:
    identity: dict[str, Any] = {
        'subject_sha': 'a' * 40, 'base_sha': 'b' * 40,
        'qualification_subject': 'pull-request:42', 'foreground': True,
        'candidate_image': 'sha256:image', 'environment_key': 'environment',
        'started_at': '2026-10-07T10:00:00+00:00',
        'completed_at': '2026-10-07T10:01:00+00:00',
        'quality_outcome': 'failure', 'current': True,
        'failure_classification': {'decisive_failure_at': '2026-10-07T10:00:20+00:00'},
        'environment': {'workflow_blob': 'workflow', 'manifest_blobs': 'manifests'},
        'github': {'repository': 'owner/repo', 'workflow': 'Quality', 'job': 'quality',
                   'run_id': '17', 'run_attempt': '1'},
        'qualification_rework': {'retry_reason': 'EVIDENCE_GATHERING', 'harness_seconds': 1,
                                 'wrapper_seconds': 2, 'fixture_seconds': 3,
                                 'setup_seconds': 4},
    }
    result = qualification_attempt_observation(identity, {'mode': 'FULL_FALLBACK'})
    assert result['lineage_id'].startswith('ql-')
    assert result['provider']['executed_attempt_id'].startswith('qa-')
    assert result['scope']['candidate_sha'] == 'a' * 40
    assert result['decisive_failure_at'] == '2026-10-07T10:00:20+00:00'
    assert result['foreground_seconds'] == 60
    assert result['retry_reason'] == 'EVIDENCE_GATHERING'
    assert result['outcome'] == 'FAILURE'

    rerun = qualification_attempt_observation(
        identity | {'github': identity['github'] | {'run_attempt': '2'},
                    'qualification_rework': None},
        {'mode': 'FULL_FALLBACK'},
    )
    assert rerun['retry_reason'] == 'PROVIDER_RETRY'

    same_lineage = qualification_attempt_observation(
        identity | {'subject_sha': 'c' * 40}, {'mode': 'FULL_FALLBACK'},
    )
    other_lineage = qualification_attempt_observation(
        identity | {'qualification_subject': 'pull-request:43'}, {'mode': 'FULL_FALLBACK'},
    )
    assert same_lineage['lineage_id'] == result['lineage_id']
    assert other_lineage['lineage_id'] != result['lineage_id']

    exact_head = identity | {
        'qualification_subject': 'exact-head:refs/heads/main',
        'subject_sha': 'd' * 40,
    }
    next_exact_head = exact_head | {
        'subject_sha': 'e' * 40,
        'github': identity['github'] | {'run_id': '18'},
    }
    assert qualification_attempt_observation(
        exact_head, {'mode': 'FULL_FALLBACK'},
    )['lineage_id'] == qualification_attempt_observation(
        next_exact_head, {'mode': 'FULL_FALLBACK'},
    )['lineage_id']

    unclassified = qualification_attempt_observation(
        identity | {'failure_classification': None, 'qualification_rework': None},
        {'mode': 'FULL_FALLBACK'},
    )
    assert unclassified['decisive_failure_at'] is None
    assert unclassified['retry_reason'] == 'UNKNOWN'

    failed_without_classifier = qualification_attempt_observation(
        identity | {'failure_classification': None}, {'mode': 'FULL_FALLBACK'},
    )
    assert summarize_lineage((failed_without_classifier,))['reason'] == 'failure-classification-unknown'


def _attempt(run_attempt: int = 1, **changes: object) -> dict[str, object]:
    identity: dict[str, object] = {
        'subject_sha': 'a' * 40, 'base_sha': 'b' * 40,
        'qualification_subject': 'pull-request:42', 'foreground': True,
        'candidate_image': 'image', 'environment_key': 'environment',
        'started_at': f'2026-10-07T10:0{run_attempt - 1}:00+00:00',
        'completed_at': f'2026-10-07T10:0{run_attempt}:00+00:00',
        'quality_outcome': 'success', 'current': True,
        'failure_classification': {},
        'environment': {'workflow_blob': 'workflow', 'manifest_blobs': 'manifests'},
        'github': {'repository': 'owner/repo', 'workflow': 'Quality', 'job': 'quality',
                   'run_id': '17', 'run_attempt': str(run_attempt)},
        'qualification_rework': {'retry_reason': 'EVIDENCE_GATHERING',
                                 'harness_seconds': 1, 'wrapper_seconds': 2,
                                 'fixture_seconds': 3, 'setup_seconds': 4},
    }
    identity.update(changes)
    return qualification_attempt_observation(identity, {'mode': 'FULL_FALLBACK'})


def test_lineage_scope_metrics_and_provider_dedupe() -> None:
    first = _attempt(1, quality_outcome='failure',
                     failure_classification={'decisive_failure_at': '2026-10-07T10:00:20+00:00'})
    second = _attempt(2)
    metrics = summarize_lineage((first, second))
    assert metrics['status'] == 'AVAILABLE'
    assert [metrics[key] for key in (
        'attempt_count', 'cumulative_wall_seconds', 'cumulative_foreground_seconds',
        'repeated_scope_retry_count', 'rework_seconds',
        'time_to_first_decisive_failure_seconds', 'time_after_decisive_failure_seconds',
    )] == [2, 120, 120, 1, 20, 20, 100]
    assert summarize_lineage((first, first))['attempt_count'] == 1
    provider = dict(cast(dict[str, object], first['provider']))
    provider.pop('executed_attempt_id')
    missing_derived_id = first | {'provider': provider}
    assert summarize_lineage((missing_derived_id,))['attempt_count'] == 1
    wrong_provider = dict(cast(dict[str, object], first['provider']))
    wrong_provider['executed_attempt_id'] = 'qa-wrong'
    wrong_derived_id = first | {'provider': wrong_provider}
    assert summarize_lineage((wrong_derived_id,))['reason'] == 'evidence-missing'
    conflict = first | {'foreground_seconds': 12}
    assert summarize_lineage((first, conflict))['reason'] == 'attempt-conflict'


def test_same_scope_requires_exact_subject_plan_harness_and_environment() -> None:
    first = _attempt(1)
    second = _attempt(2, subject_sha='c' * 40)
    assert summarize_lineage((first, second))['repeated_scope_retry_count'] == 0
    for field in ('base_sha', 'environment_key', 'candidate_image'):
        changed = _attempt(2, **{field: None})
        assert summarize_lineage((first, changed))['status'] == 'UNKNOWN'


def test_lineage_evidence_conflicts_fail_closed() -> None:
    assert summarize_lineage((_attempt(1, current=False),))['reason'] == 'currentness-conflict'
    assert summarize_lineage((_attempt(1, qualification_rework=None),))['reason'] == 'retry-reason-unknown'
    assert summarize_lineage((_attempt(1, quality_outcome=None),))['status'] == 'UNKNOWN'
    assert summarize_lineage((_attempt(1, subject_sha=None),))['status'] == 'UNKNOWN'
    assert summarize_lineage((_attempt(1, completed_at='2026-10-07T09:59:00+00:00'),))['status'] == 'UNKNOWN'


def test_rework_warning_is_inert_deduplicated_and_unknown_safe() -> None:
    metrics = summarize_lineage((_attempt(1), _attempt(2)))
    warning = advisory_warning(metrics, repeated_scope_threshold=1, rework_seconds_threshold=100)
    assert warning['disposition'] == 'EMIT'
    assert warning['attention_kind'] == 'CI_QUALIFICATION_REWORK_WARNING'
    assert warning['route'] == 'tests-ci-to-wakeful-attention'
    repeated = advisory_warning(metrics, repeated_scope_threshold=1, rework_seconds_threshold=100,
                                emitted_warning_ids=frozenset({warning['warning_id']}))
    assert repeated['disposition'] == 'DEDUPED'
    assert advisory_warning(metrics, repeated_scope_threshold=3,
                            rework_seconds_threshold=100)['disposition'] == 'NOT_DUE'
    unknown = summarize_lineage((_attempt(1, current=None),))
    assert advisory_warning(unknown, repeated_scope_threshold=0,
                            rework_seconds_threshold=0)['disposition'] == 'UNKNOWN'


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
    assert authoritative.split('--junitxml=/metrics/junit.xml', 1)[0].rstrip().endswith('\\')
    assert 'shift 1 &&' in authoritative
    assert 'needs.plan.outputs.selected_tests' in authoritative
    assert '--planner ' in execution and "'planner_revision': plan.planner_revision" in execution
    assert '--method GET' in execution
    assert 'selector_health_clear(' in execution
    assert 'broad_backstop_required(event_kind, ref_name, default_branch)' in execution
    assert "vars.SWITCHSTAND_SELECTIVE_FOREGROUND_ENABLED == 'true'" in text
    assert "health = selective_enabled == 'true' and not force_full" in execution
    assert 'evaluate_full_suite_due' in execution
    assert 'adaptive_full_suite_shadow' in execution
    assert 'adaptive_json=' in execution
    assert 'planner_policy_changed = policy_identity(baseline_sha) != policy_identity(head)' in execution
    assert "unresolved_hard_miss = any(" in execution
    assert 'planner_policy_changed=False' not in execution
    assert 'unresolved_hard_miss=False' not in execution
    assert 'history_complete=history_complete' in execution
    assert 'test "${push_count:-1000}" -lt 1000' in execution
    assert 'test "${schedule_count:-1000}" -lt 1000' in execution
    assert 'test "$push_complete" = true' in execution
    assert 'test "$schedule_complete" = true' in execution
    assert '--paginate' in execution
    assert 'github.event.pull_request.stack.position == github.event.pull_request.stack.size' not in execution
    assert "classify_cumulative_stack_top" in execution
    assert '.head.repo.full_name == env.PR_BASE_REPO' in execution
    assert '.base.repo.full_name == env.PR_HEAD_REPO' in execution
    assert 'test "$stack_top" != unknown || subject_verified=false' in execution
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
    assert 'id: quality_run' in execution
    assert 'id: quality_subject' in execution
    assert 'qualification_rework' in execution
    assert 'QUALIFICATION_SUBJECT' in execution
    assert "format('exact-head:{0}', github.ref)" in execution
    assert '"foreground": e["SUBJECT_KIND"] == "composition"' in execution
    assert 'started-at' in execution and 'completed-at' in execution
    assert 'steps.quality_run.outcome' in execution
    assert 'steps.quality_subject.outcome' in execution
    assert '"current": e["SUBJECT_BINDING_OUTCOME"] == "success"' in execution
    assert '"current": True' not in execution
    identity = ('scripts/verify-quality-composition', 'QUALITY_COMPOSITION_SHA',
                'QUALITY_SUBJECT_SHA', 'QUALITY_BASE_SHA', 'STACK_BASE_REF', 'STACK_POSITION')
    assert all(token in execution for token in identity)
    assert all(token in lifecycle for token in identity)
    assert 'SWITCHSTAND_REAL_DOCKER=1' in lifecycle
