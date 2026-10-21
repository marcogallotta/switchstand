from __future__ import annotations

import httpx
import pytest

from switchstand.repository_candidate import GATES, _policy_evidence, qualify_repository_candidate
from switchstand.stacked_delivery import layer_qualification_is_sufficient

BASE, HEAD, COMPOSITION, PREFIX, TARGET = (
    "a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40,
)


def client(*, omitted: str | None = None, mismatch: bool = False,
           overrides: dict[str, tuple[str, str | None]] | None = None,
           malformed: bool = False, wrong_head: str | None = None,
           bad_run: bool = False, changed: bool = False,
           job_failure: bool = False, stacked: bool = False,
           selected: bool = False, omit_policy_evidence: bool = False,
           affected: bool = False, docker_result: str = "success",
           selected_shard: int = 0, full_shards: tuple[int, ...] | None = None,
           shard_override: dict[str, object] | None = None) -> httpx.AsyncClient:
    overrides = overrides or {}
    pr_reads = 0
    def response(request: httpx.Request) -> httpx.Response:
        nonlocal pr_reads
        path = request.url.path
        if malformed:
            return httpx.Response(200, content=b"not-json")
        if path.endswith("/pulls/7"):
            pr_reads += 1
            payload = {
                "base": {"sha": BASE},
                "head": {"sha": "d" * 40 if changed and pr_reads > 1 else HEAD},
                "merge_commit_sha": COMPOSITION,
            }
            if stacked:
                payload["stack"] = {"base": {"ref": "main"}, "position": 2, "size": 2}
        elif path.endswith(f"/commits/{COMPOSITION}") and "check-runs" not in path:
            payload = {"parents": [
                {"sha": PREFIX if stacked else BASE},
                {"sha": "d" * 40 if mismatch else HEAD},
            ]}
        elif path.endswith(f"/commits/{PREFIX}"):
            payload = {"parents": [{"sha": TARGET}, {"sha": BASE}]}
        elif path.endswith("/branches/main"):
            payload = {"commit": {"sha": TARGET}}
        elif path.endswith("/check-runs"):
            checks = []
            for index, (name, kind) in enumerate(GATES, 1):
                if name == omitted:
                    continue
                status, conclusion = overrides.get(name, ("completed", "success"))
                checks.append({
                    "id": 100 + index, "name": name, "head_sha": wrong_head or HEAD,
                    "app": {"slug": "github-actions"}, "check_suite": {"id": 10 + index},
                    "status": status,
                    "conclusion": conclusion, "started_at": "2026-09-30T10:00:00Z",
                    "completed_at": "2026-09-30T10:00:02Z" if status == "completed" else None,
                    "details_url": f"https://github.com/marcogallotta/switchstand/actions/runs/{index}/job/{100 + index}",
                    "output": {"title": "failure title", "summary": "short summary"},
                })
                if not omit_policy_evidence:
                    mode = "PROMOTE_TEST_MODULE_ONLY_V1" if selected else "FULL_FALLBACK"
                    if affected:
                        mode = "PROMOTE_AFFECTED_INPUTS_V1"
                    execution_name = f"Quality execution / {mode}"
                    if selected or affected:
                        execution_name += f" / shard {selected_shard}"
                    execution_names = [execution_name] if full_shards is None else [
                        f"Quality execution / {mode} / shard {shard}" for shard in full_shards
                    ]
                    companions = (
                        ("Quality policy", "success"),
                        *((name, "success") for name in execution_names),
                        (
                            "Exact-head Docker lifecycle" if kind == "exact_head"
                            else "PR composition Docker lifecycle",
                            "skipped" if selected else docker_result,
                        ),
                    )
                    for offset, (companion, companion_conclusion) in enumerate(companions, 1):
                        checks.append({
                            "id": 1000 + index * 10 + offset, "name": companion,
                            "head_sha": wrong_head or HEAD,
                            "app": {"slug": "github-actions"},
                            "check_suite": {"id": 10 + index}, "status": "completed",
                            "conclusion": companion_conclusion,
                            "details_url": "https://github.com/marcogallotta/switchstand/actions/"
                                           f"runs/{index}/job/{1000 + index * 10 + offset}",
                        })
                        if companion.endswith(" / shard 3"):
                            checks[-1].update(shard_override or {})
            payload = {"check_runs": checks}
        elif "/actions/runs/" in path:
            index = int(path.rsplit("/", 1)[1])
            kind = GATES[index - 1][1]
            payload = {
                "id": index, "run_attempt": 2, "check_suite_id": 10 + index,
                "path": ".github/workflows/other.yml" if bad_run else ".github/workflows/quality.yml",
                "event": "push" if kind == "exact_head" else "pull_request",
                "name": "Quality / exact-head" if kind == "exact_head" else "Quality / composition",
                "head_sha": HEAD, "pull_requests": [{
                    "number": 7, "base": {"sha": BASE}, "head": {"sha": HEAD},
                }],
            }
        elif "/actions/jobs/" in path:
            if job_failure:
                return httpx.Response(503)
            payload = {"steps": [
                {"name": "checkout", "conclusion": "success"},
                {"name": "tests", "conclusion": "failure"},
            ]}
        else:
            raise AssertionError(str(request.url))
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(response))


async def test_two_stable_terminal_gates_are_ready_with_exact_identity_and_timing():
    async with client() as http:
        result = await qualify_repository_candidate(7, client=http)
    assert (result.status, result.base_sha, result.head_sha, result.composition_sha) == (
        "READY", BASE, HEAD, COMPOSITION,
    )
    assert result.composition_parents == [BASE, HEAD]
    assert [gate.name for gate in result.gates] == [name for name, _ in GATES]
    assert all(gate.attempt == 2 and gate.duration_ms == 2000 for gate in result.gates)
    assert all(not gate.failed_steps and gate.failure_excerpt is None for gate in result.gates)
    assert all("RUNTIME_LIFECYCLE" in gate.evidence_dimensions for gate in result.gates)
    catalogue = result.proportional_evidence()
    assert layer_qualification_is_sufficient(catalogue)


async def test_selected_policy_evidence_is_typed_and_missing_evidence_fails_closed():
    async with client(selected=True) as http:
        selected = await qualify_repository_candidate(7, client=http)
    assert selected.status == "READY"
    assert all("RUNTIME_LIFECYCLE" not in gate.evidence_dimensions for gate in selected.gates)

    async with client(affected=True) as http:
        affected = await qualify_repository_candidate(7, client=http)
    assert affected.status == "READY"
    assert all("RUNTIME_LIFECYCLE" in gate.evidence_dimensions for gate in affected.gates)
    assert all("BROAD_QUALITY" not in gate.evidence_dimensions for gate in affected.gates)

    async with client(omit_policy_evidence=True) as http:
        missing = await qualify_repository_candidate(7, client=http)
    assert missing.status == "NOT_READY"
    assert all(gate.reason == "policy-evidence-missing" for gate in missing.gates)


@pytest.mark.parametrize("kind", ["exact_head", "composition"])
@pytest.mark.parametrize("override", [
    {}, {"name": "arbitrary Docker lifecycle"}, {"status": "in_progress"},
    {"conclusion": "failure"}, {"conclusion": "success"},
    {"head_sha": "f" * 40}, {"app": {"slug": "other"}},
    {"details_url": "https://github.com/actions/runs/999/job/123"},
    {"details_url": "malformed"},
])
def test_real_skipped_docker_name_preserves_identity_and_mode_checks(
    kind: str, override: dict[str, object],
):
    # Projection of PR1041 GitHub checks, run39485815393, head7f0893bd5608eebf….
    # Keep the literal provider name independent of the parser's compatibility alias.
    head = "7f0893bd5608eebfb99043f5778a11f692694bc2"
    names = [
        "Quality policy", "Quality execution / PROMOTE_TEST_MODULE_ONLY_V1 / shard 0",
        (
            "github.event_name == 'pull_request' && 'PR composition Docker lifecycle' || "
            "github.event_name == 'schedule' && 'Scheduled exact-head Docker lifecycle' || "
            "'Exact-head Docker lifecycle'"
        ),
    ]
    checks = [{
        "name": name, "head_sha": head, "app": {"slug": "github-actions"},
        "status": "completed", "conclusion": "skipped" if index == 2 else "success",
        "details_url": "https://github.com/marcogallotta/switchstand/actions/"
                       f"runs/39485815393/job/{job}",
    } for index, (name, job) in enumerate(zip(names, [118845817540, 118845920358, 118845922580]))]
    checks[-1].update(override)
    dimensions, reason = _policy_evidence(checks, 39485815393, kind, head)
    assert (reason is None) == (not override)
    assert ("LAYER_CAUSAL_QUALITY" in dimensions) == (not override)
    assert "RUNTIME_LIFECYCLE" not in dimensions
    if override:
        return
    checks.append({**checks[-1], "name": (
        "Exact-head Docker lifecycle" if kind == "exact_head" else "PR composition Docker lifecycle"
    )})
    assert _policy_evidence(checks, 39485815393, kind, head) == ([], "policy-evidence-conflicting")
    checks.pop()
    for mode in ("FULL_FALLBACK", "PROMOTE_AFFECTED_INPUTS_V1 / shard 0"):
        checks[1]["name"] = f"Quality execution / {mode}"
        assert _policy_evidence(checks, 39485815393, kind, head) == (
            [], "policy-evidence-conflicting",
        )


@pytest.mark.parametrize(("shards", "override", "reason"), [
    ((0, 1, 2, 3), {}, None),
    ((0, 1, 2), {}, "policy-evidence-missing"),
    ((0, 1, 2, 3, 0), {}, "policy-evidence-conflicting"),
    ((0, 1, 2, 3), {"conclusion": "failure"}, "policy-evidence-failed"),
    ((0, 1, 2, 3), {"status": "in_progress"}, "policy-evidence-running"),
    ((0, 1, 2, 3), {"head_sha": "f" * 40}, "policy-evidence-missing"),
    ((0, 1, 2, 3), {"app": {"slug": "other"}}, "policy-evidence-missing"),
    ((0, 1, 2, 3), {"details_url": "https://github.com/actions/runs/999/job/123"},
     "policy-evidence-missing"),
    ((0, 1, 2, 3), {"details_url": "malformed"}, "policy-evidence-missing"),
    ((0, 1, 2, 3), {"name": "Quality execution / FULL_FALLBACK / shard x"},
     "policy-evidence-missing"),
])
async def test_full_fallback_requires_complete_successful_same_identity_shards(
    shards: tuple[int, ...], override: dict[str, object], reason: str | None,
):
    async with client(full_shards=shards, shard_override=override) as http:
        result = await qualify_repository_candidate(7, client=http)
    assert result.status == ("READY" if reason is None else "NOT_READY")
    assert all(gate.reason == reason for gate in result.gates)
    assert all(("BROAD_QUALITY" in gate.evidence_dimensions) == (reason is None)
               for gate in result.gates)
    assert layer_qualification_is_sufficient(result.proportional_evidence()) == (reason is None)


@pytest.mark.parametrize(("docker", "shard"), [("skipped", 0), ("failure", 0), ("success", 1)])
async def test_affected_policy_requires_successful_docker_and_single_selected_shard(
    docker: str, shard: int,
):
    async with client(affected=True, docker_result=docker, selected_shard=shard) as http:
        result = await qualify_repository_candidate(7, client=http)
    assert result.status == "NOT_READY"
    assert all(gate.reason in {"policy-evidence-conflicting", "policy-evidence-unknown"}
               for gate in result.gates)


async def test_native_stack_composition_is_bound_through_prefix_to_current_target():
    async with client(stacked=True) as http:
        result = await qualify_repository_candidate(7, client=http)
    assert result.status == "READY"
    assert result.catalogue == "switchstand-quality-v3"
    assert result.composition_parents == [PREFIX, HEAD]


async def test_missing_gate_and_old_composition_fail_closed():
    async with client(omitted="Exact-head Quality") as http:
        missing = await qualify_repository_candidate(7, client=http)
    assert missing.status == "NOT_READY"
    assert next(g for g in missing.gates if g.name == "Exact-head Quality").reason == "missing"
    evidence = missing.proportional_evidence()
    assert not evidence.passed
    assert not layer_qualification_is_sufficient(evidence)

    async with client(mismatch=True) as http:
        stale = await qualify_repository_candidate(7, client=http)
    assert stale.status == "NOT_READY" and stale.reason == "composition_mismatch"
    assert [g.reason for g in stale.gates if g.subject_kind == "composition"] == [
        "wrong-head",
    ]

    async with client(wrong_head="d" * 40) as http:
        wrong = await qualify_repository_candidate(7, client=http)
    assert wrong.status == "NOT_READY"
    assert all(g.reason == "wrong-head" for g in wrong.gates)

    async with client(bad_run=True) as http:
        untrusted = await qualify_repository_candidate(7, client=http)
    assert untrusted.status == "NOT_READY"
    assert all(g.reason == "conflicting" for g in untrusted.gates)


async def test_running_cancelled_and_detail_are_bounded_and_diagnostic():
    overrides = {
        "Exact-head Quality": ("in_progress", None),
        "PR composition Quality": ("completed", "cancelled"),
    }
    async with client(overrides=overrides) as http:
        compact = await qualify_repository_candidate(7, client=http)
    running = next(g for g in compact.gates if g.name == "Exact-head Quality")
    cancelled = next(g for g in compact.gates if g.name == "PR composition Quality")
    assert compact.status == "NOT_READY"
    assert running.reason == "running" and running.running_for_ms is not None
    assert cancelled.reason == "cancelled" and cancelled.duration_ms == 2000
    assert cancelled.failed_steps == [] and cancelled.failure_excerpt is None

    async with client(overrides=overrides) as http:
        detailed = await qualify_repository_candidate(7, True, http)
    cancelled = next(g for g in detailed.gates if g.name == "PR composition Quality")
    assert cancelled.failed_steps == ["tests"]
    assert cancelled.failure_excerpt == "failure title\nshort summary"
    assert len(cancelled.failure_excerpt) <= 1001

    async with client(overrides=overrides, job_failure=True) as http:
        partial = await qualify_repository_candidate(7, True, http)
    cancelled = next(g for g in partial.gates if g.name == "PR composition Quality")
    assert partial.status == "NOT_READY" and cancelled.reason == "cancelled"
    assert cancelled.detail_reason == "provider-unavailable"


async def test_provider_or_payload_failure_is_unknown():
    async with client(malformed=True) as http:
        result = await qualify_repository_candidate(7, client=http)
    assert result.status == "UNKNOWN" and result.reason == "github_unavailable"
    assert all(gate.reason == "provider-unavailable" for gate in result.gates)

    async with client(changed=True) as http:
        stale = await qualify_repository_candidate(7, client=http)
    assert stale.status == "NOT_READY" and stale.reason == "candidate_changed"
    assert all(gate.reason == "stale" for gate in stale.gates)
