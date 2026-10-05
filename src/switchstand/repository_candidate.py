from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal, cast
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

REPOSITORY = "marcogallotta/switchstand"
API = f"https://api.github.com/repos/{REPOSITORY}"
CATALOGUE = "switchstand-quality-v2"
GATES = (
    ("Exact-head Quality", "exact_head"),
    ("PR composition Quality", "composition"),
)
DETAILS_RE = re.compile(r"/actions/runs/(?P<run>[0-9]+)(?:/job/(?P<job>[0-9]+))?")


class QualificationGate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    subject_kind: Literal["exact_head", "composition"]
    subject_sha: str | None = None
    state: str
    conclusion: str | None = None
    reason: str | None = None
    run_id: int | None = None
    job_id: int | None = None
    attempt: int | None = None
    provider_url: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    running_for_ms: int | None = None
    age_ms: int | None = None
    failed_steps: list[str] = Field(default_factory=list)
    failure_excerpt: str | None = None
    detail_reason: str | None = None


class RepositoryCandidateQualification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["READY", "NOT_READY", "UNKNOWN"]
    repository: str = REPOSITORY
    pull_request: int
    base_sha: str | None = None
    head_sha: str | None = None
    composition_sha: str | None = None
    composition_parents: list[str] = Field(default_factory=list)
    catalogue: str = CATALOGUE
    gates: list[QualificationGate]
    reason: str | None = None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _elapsed(started: datetime | None, ended: datetime) -> int | None:
    return max(0, int((ended - started).total_seconds() * 1000)) if started else None


def _closed_reason(status: str, conclusion: str | None) -> str | None:
    if status == "queued":
        return "queued"
    if status != "completed":
        return "running" if status == "in_progress" else "provider-unavailable"
    if conclusion == "success":
        return None
    if conclusion in {"cancelled", "skipped"}:
        return conclusion
    return "failed" if conclusion else "provider-unavailable"


def _composition_reason(parents: list[str], base: str, head: str) -> str | None:
    if len(parents) != 2:
        return "wrong-composition"
    if parents[0] != base:
        return "wrong-base"
    return "wrong-head" if parents[1] != head else None


async def _stack_composition_reason(
    http: httpx.AsyncClient, parents: list[str], base: str, head: str,
    target: str, position: int,
) -> str | None:
    if len(parents) != 2:
        return "wrong-composition"
    if parents[1] != head:
        return "wrong-head"
    if position < 1:
        return "wrong-composition"
    cursor = parents[0]
    for offset in range(position - 1):
        commit = await _json(http, f"/commits/{cursor}")
        prefix = [str(parent["sha"]) for parent in commit["parents"]]
        if len(prefix) != 2:
            return "wrong-composition"
        if offset == 0 and prefix[1] != base:
            return "wrong-base"
        cursor = prefix[0]
    return "wrong-base" if cursor != target else None


def _missing_gates(reason: str, head: str | None, composition: str | None) -> list[QualificationGate]:
    return [QualificationGate(
        name=name, subject_kind=cast(Literal["exact_head", "composition"], kind),
        subject_sha=head if kind == "exact_head" else composition,
        state="missing" if reason == "missing" else "unknown", reason=reason,
    ) for name, kind in GATES]


async def _json(http: httpx.AsyncClient, path: str, **params: Any) -> dict[str, Any]:
    response = await http.get(f"{API}{path}", params=params or None)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise TypeError("GitHub response must be an object")
    return cast(dict[str, Any], payload)


def _excerpt(output: Any) -> str | None:
    if not isinstance(output, dict):
        return None
    output = cast(dict[str, Any], output)
    text = "\n".join(str(output.get(key) or "") for key in ("title", "summary", "text")).strip()
    if not text:
        return None
    return text[:1000] + ("…" if len(text) > 1000 else "")


def _run_matches(
    run: dict[str, Any], check: dict[str, Any], kind: str, pull_request: int,
    base: str, head: str, run_id: int | None, job_id: int | None,
) -> bool:
    suite = check.get("check_suite")
    app = check.get("app")
    if not isinstance(suite, dict) or not isinstance(app, dict):
        return False
    suite = cast(dict[str, Any], suite)
    app = cast(dict[str, Any], app)
    expected_event = "push" if kind == "exact_head" else "pull_request"
    expected_name = "Quality / exact-head" if kind == "exact_head" else "Quality / composition"
    valid = (
        app.get("slug") == "github-actions" and check.get("id") == job_id
        and run.get("id") == run_id
        and run.get("check_suite_id") == suite.get("id")
        and run.get("path") == ".github/workflows/quality.yml"
        and run.get("event") == expected_event and run.get("name") == expected_name
        and run.get("head_sha") == head
    )
    if kind == "exact_head" or not valid:
        return valid
    prs = run.get("pull_requests")
    if not isinstance(prs, list):
        return False
    for value in cast(list[Any], prs):
        if not isinstance(value, dict):
            continue
        item = cast(dict[str, Any], value)
        pr_base, pr_head = item.get("base"), item.get("head")
        if (item.get("number") == pull_request and isinstance(pr_base, dict)
                and isinstance(pr_head, dict)
                and cast(dict[str, Any], pr_base).get("sha") == base
                and cast(dict[str, Any], pr_head).get("sha") == head):
            return True
    return False


async def qualify_repository_candidate(
    pull_request: int, include_failure_detail: bool = False,
    client: httpx.AsyncClient | None = None,
) -> RepositoryCandidateQualification:
    owned = client is None
    http = client or httpx.AsyncClient(
        follow_redirects=True, timeout=10.0, trust_env=False,
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
    )
    base: str | None = None
    head: str | None = None
    composition: str | None = None
    parents: list[str] = []
    try:
        pr = await _json(http, f"/pulls/{pull_request}")
        base, head = str(pr["base"]["sha"]), str(pr["head"]["sha"])
        composition = str(pr["merge_commit_sha"])
        commit = await _json(http, f"/commits/{composition}")
        parents = [str(parent["sha"]) for parent in commit["parents"]]
        stack = pr.get("stack")
        target_ref: str | None = None
        target_sha: str | None = None
        if isinstance(stack, dict):
            stack = cast(dict[str, Any], stack)
            stack_base = stack.get("base")
            position = stack.get("position")
            if not isinstance(stack_base, dict) or not isinstance(position, int):
                raise TypeError("GitHub stack identity must include base and position")
            stack_base = cast(dict[str, Any], stack_base)
            target_ref = str(stack_base["ref"])
            branch = await _json(http, f"/branches/{quote(target_ref, safe='')}")
            target_sha = str(branch["commit"]["sha"])
            composition_reason = await _stack_composition_reason(
                http, parents, base, head, target_sha, position,
            )
        else:
            composition_reason = _composition_reason(parents, base, head)
        now = datetime.now(UTC)
        checks_payload = await _json(
            http, f"/commits/{head}/check-runs", filter="latest", per_page=100,
        )
        raw_checks = checks_payload.get("check_runs")
        if not isinstance(raw_checks, list):
            raise TypeError("GitHub check_runs must be a list of objects")
        raw_list = cast(list[Any], raw_checks)
        checks = [cast(dict[str, Any], item) for item in raw_list if isinstance(item, dict)]
        if len(checks) != len(raw_list):
            raise TypeError("GitHub check_runs must be a list of objects")
        runs: dict[int, dict[str, Any]] = {}
        gates: list[QualificationGate] = []
        for name, kind in GATES:
            subject = head if kind == "exact_head" else composition
            matches = [item for item in checks if item.get("name") == name]
            if len(matches) != 1:
                gates.append(QualificationGate(
                    name=name, subject_kind=cast(Literal["exact_head", "composition"], kind),
                    subject_sha=subject, state="missing" if not matches else "completed",
                    reason="missing" if not matches else "conflicting",
                ))
                continue
            check = matches[0]
            actual_subject = str(check.get("head_sha") or "")
            status = str(check.get("status") or "")
            conclusion = check.get("conclusion")
            conclusion = str(conclusion) if conclusion is not None else None
            details = str(check.get("details_url") or "")
            identity = DETAILS_RE.search(details)
            run_id = int(identity.group("run")) if identity else None
            job_id = int(identity.group("job")) if identity and identity.group("job") else None
            run: dict[str, Any] = {}
            if run_id is not None:
                if run_id not in runs:
                    runs[run_id] = await _json(http, f"/actions/runs/{run_id}")
                run = runs[run_id]
            attempt = run.get("run_attempt")
            started = _timestamp(check.get("started_at"))
            completed = _timestamp(check.get("completed_at"))
            reason = _closed_reason(status, conclusion)
            if actual_subject != head:
                reason = "wrong-head"
            elif not _run_matches(run, check, kind, pull_request, base, head, run_id, job_id):
                reason = "conflicting"
            elif kind == "composition" and composition_reason is not None:
                reason = composition_reason
            failed_steps: list[str] = []
            failure_excerpt = None
            detail_reason = None
            if include_failure_detail and reason in {"failed", "cancelled", "skipped"}:
                if job_id is not None:
                    try:
                        job = await _json(http, f"/actions/jobs/{job_id}")
                        failed_steps = [str(step["name"]) for step in job.get("steps", [])
                                        if step.get("conclusion") not in {None, "success"}][:20]
                    except (httpx.HTTPError, AttributeError, KeyError, TypeError, ValueError):
                        detail_reason = "provider-unavailable"
                failure_excerpt = _excerpt(check.get("output"))
            gates.append(QualificationGate(
                name=name, subject_kind=cast(Literal["exact_head", "composition"], kind),
                subject_sha=subject, state=status or "unknown", conclusion=conclusion,
                reason=reason, run_id=run_id, job_id=job_id,
                attempt=int(attempt) if isinstance(attempt, int) else None,
                provider_url=details or None,
                started_at=started.isoformat() if started else None,
                completed_at=completed.isoformat() if completed else None,
                duration_ms=_elapsed(started, completed) if completed else None,
                running_for_ms=_elapsed(started, now) if status == "in_progress" else None,
                age_ms=_elapsed(started, now) if status == "queued" else None,
                failed_steps=failed_steps, failure_excerpt=failure_excerpt,
                detail_reason=detail_reason,
            ))
        ready = composition_reason is None and all(g.reason is None for g in gates)
        result_reason = None if ready else (
            "composition_mismatch" if composition_reason is not None else "gates_not_ready"
        )
        if ready:
            current = await _json(http, f"/pulls/{pull_request}")
            current_identity = (
                str(current["base"]["sha"]), str(current["head"]["sha"]),
                str(current["merge_commit_sha"]),
            )
            target_changed = False
            if target_ref is not None:
                current_branch = await _json(http, f"/branches/{quote(target_ref, safe='')}")
                target_changed = str(current_branch["commit"]["sha"]) != target_sha
            if current_identity != (base, head, composition) or target_changed:
                ready, result_reason = False, "candidate_changed"
                for gate in gates:
                    gate.reason = "stale"
        return RepositoryCandidateQualification(
            status="READY" if ready else "NOT_READY", pull_request=pull_request,
            base_sha=base, head_sha=head, composition_sha=composition,
            composition_parents=parents, gates=gates,
            reason=result_reason,
        )
    except (httpx.HTTPError, AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return RepositoryCandidateQualification(
            status="UNKNOWN", pull_request=pull_request, base_sha=base, head_sha=head,
            composition_sha=composition, composition_parents=parents,
            gates=_missing_gates("provider-unavailable", head, composition),
            reason="github_unavailable",
        )
    finally:
        if owned:
            await http.aclose()
