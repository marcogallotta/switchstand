from __future__ import annotations

import httpx

from switchstand.repository_candidate import GATES, qualify_repository_candidate

BASE, HEAD, COMPOSITION = "a" * 40, "b" * 40, "c" * 40


def client(*, omitted: str | None = None, mismatch: bool = False,
           overrides: dict[str, tuple[str, str | None]] | None = None,
           malformed: bool = False, wrong_head: str | None = None) -> httpx.AsyncClient:
    overrides = overrides or {}

    def response(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if malformed:
            return httpx.Response(200, content=b"not-json")
        if path.endswith("/pulls/7"):
            payload = {
                "base": {"sha": BASE}, "head": {"sha": HEAD},
                "merge_commit_sha": COMPOSITION,
            }
        elif path.endswith(f"/commits/{COMPOSITION}") and "check-runs" not in path:
            payload = {"parents": [{"sha": BASE}, {"sha": "d" * 40 if mismatch else HEAD}]}
        elif path.endswith("/check-runs"):
            subject = path.split("/")[-2]
            expected_kind = "exact_head" if subject == HEAD else "composition"
            checks = []
            for index, (name, kind) in enumerate(GATES, 1):
                if kind != expected_kind or name == omitted:
                    continue
                status, conclusion = overrides.get(name, ("completed", "success"))
                checks.append({
                    "name": name, "head_sha": wrong_head or subject, "status": status,
                    "conclusion": conclusion, "started_at": "2026-09-30T10:00:00Z",
                    "completed_at": "2026-09-30T10:00:02Z" if status == "completed" else None,
                    "details_url": f"https://github.com/marcogallotta/switchstand/actions/runs/{index}/job/{100 + index}",
                    "output": {"title": "failure title", "summary": "short summary"},
                })
            payload = {"check_runs": checks}
        elif "/actions/runs/" in path:
            payload = {"run_attempt": 2}
        elif "/actions/jobs/" in path:
            payload = {"steps": [
                {"name": "checkout", "conclusion": "success"},
                {"name": "tests", "conclusion": "failure"},
            ]}
        else:
            raise AssertionError(str(request.url))
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(response))


async def test_four_current_green_gates_are_ready_with_exact_identity_and_timing():
    async with client() as http:
        result = await qualify_repository_candidate(7, client=http)
    assert (result.status, result.base_sha, result.head_sha, result.composition_sha) == (
        "READY", BASE, HEAD, COMPOSITION,
    )
    assert result.composition_parents == [BASE, HEAD]
    assert [gate.name for gate in result.gates] == [name for name, _ in GATES]
    assert all(gate.attempt == 2 and gate.duration_ms == 2000 for gate in result.gates)
    assert all(not gate.failed_steps and gate.failure_excerpt is None for gate in result.gates)


async def test_missing_gate_and_old_composition_fail_closed():
    async with client(omitted="Exact-head Quality") as http:
        missing = await qualify_repository_candidate(7, client=http)
    assert missing.status == "NOT_READY"
    assert next(g for g in missing.gates if g.name == "Exact-head Quality").reason == "missing"

    async with client(mismatch=True) as http:
        stale = await qualify_repository_candidate(7, client=http)
    assert stale.status == "NOT_READY" and stale.reason == "composition_mismatch"
    assert [g.reason for g in stale.gates if g.subject_kind == "composition"] == [
        "wrong-head", "wrong-head",
    ]

    async with client(wrong_head="d" * 40) as http:
        wrong = await qualify_repository_candidate(7, client=http)
    assert wrong.status == "NOT_READY"
    assert all(g.reason in {"wrong-head", "wrong-composition"} for g in wrong.gates)


async def test_running_cancelled_and_detail_are_bounded_and_diagnostic():
    overrides = {
        "Exact-head Quality": ("in_progress", None),
        "Exact-head Docker lifecycle": ("completed", "skipped"),
        "PR composition Quality": ("completed", "cancelled"),
    }
    async with client(overrides=overrides) as http:
        compact = await qualify_repository_candidate(7, client=http)
    running = next(g for g in compact.gates if g.name == "Exact-head Quality")
    cancelled = next(g for g in compact.gates if g.name == "PR composition Quality")
    assert compact.status == "NOT_READY"
    assert running.reason == "running" and running.running_for_ms is not None
    assert cancelled.reason == "cancelled" and cancelled.duration_ms == 2000
    assert next(g for g in compact.gates if g.name == "Exact-head Docker lifecycle").reason == "skipped"
    assert cancelled.failed_steps == [] and cancelled.failure_excerpt is None

    async with client(overrides=overrides) as http:
        detailed = await qualify_repository_candidate(7, True, http)
    cancelled = next(g for g in detailed.gates if g.name == "PR composition Quality")
    assert cancelled.failed_steps == ["tests"]
    assert cancelled.failure_excerpt == "failure title\nshort summary"
    assert len(cancelled.failure_excerpt) <= 1001


async def test_provider_or_payload_failure_is_unknown():
    async with client(malformed=True) as http:
        result = await qualify_repository_candidate(7, client=http)
    assert result.status == "UNKNOWN" and result.reason == "github_unavailable"
    assert all(gate.reason == "provider-unavailable" for gate in result.gates)
