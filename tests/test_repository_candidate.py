from __future__ import annotations

import httpx

from switchstand.repository_candidate import GATES, qualify_repository_candidate
from switchstand.stacked_delivery import layer_qualification_is_sufficient

BASE, HEAD, COMPOSITION, PREFIX, TARGET = (
    "a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40,
)


def client(*, omitted: str | None = None, mismatch: bool = False,
           overrides: dict[str, tuple[str, str | None]] | None = None,
           malformed: bool = False, wrong_head: str | None = None,
           bad_run: bool = False, changed: bool = False,
           job_failure: bool = False, stacked: bool = False,
           selected: bool = False, omit_policy_evidence: bool = False) -> httpx.AsyncClient:
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
                    companions = (
                        ("Quality policy", "success"),
                        (f"Quality execution / {mode}", "success"),
                        (
                            "Exact-head Docker lifecycle" if kind == "exact_head"
                            else "PR composition Docker lifecycle",
                            "skipped" if selected else "success",
                        ),
                    )
                    for offset, (companion, companion_conclusion) in enumerate(companions, 1):
                        checks.append({
                            "id": 1000 + index * 10 + offset,
                            "name": companion,
                            "head_sha": wrong_head or HEAD,
                            "app": {"slug": "github-actions"},
                            "check_suite": {"id": 10 + index},
                            "status": "completed",
                            "conclusion": companion_conclusion,
                            "details_url": (
                                "https://github.com/marcogallotta/switchstand/actions/"
                                f"runs/{index}/job/{1000 + index * 10 + offset}"
                            ),
                        })
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
    assert layer_qualification_is_sufficient(result.proportional_evidence())


async def test_selected_policy_evidence_is_typed_and_missing_evidence_fails_closed():
    async with client(selected=True) as http:
        selected = await qualify_repository_candidate(7, client=http)
    assert selected.status == "READY"
    assert all("RUNTIME_LIFECYCLE" not in gate.evidence_dimensions for gate in selected.gates)

    async with client(omit_policy_evidence=True) as http:
        missing = await qualify_repository_candidate(7, client=http)
    assert missing.status == "NOT_READY"
    assert all(gate.reason == "policy-evidence-missing" for gate in missing.gates)


async def test_native_stack_composition_is_bound_through_prefix_to_current_target():
    async with client(stacked=True) as http:
        result = await qualify_repository_candidate(7, client=http)
    assert result.status == "READY"
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
