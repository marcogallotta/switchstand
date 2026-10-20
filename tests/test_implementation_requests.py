import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.canonical_relations import work_dependencies
from switchstand.canonical_work import (
    CanonicalWorkRepository,
    CurrentWork,
    canonical_revision,
    canonical_work,
)
from switchstand.contracts import LaunchAuthority
from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext, WorkGrant
from switchstand.human_reviews import HumanReviewConsequence, HumanReviewState
from switchstand.human_trajectory import HumanTrajectoryData, HumanTrajectoryStore, SourceKind
from switchstand.implementation_requests import ImplementationRequestState
from switchstand.priority_claims import NewPriorityClaim, PriorityClaimRepository
from switchstand.reviews import BaselineReviewProof
from switchstand.state import PostgresState
from switchstand.task_runs import task_run_requests

PACKAGE = UUID("45000000-0000-4000-8000-000000000001")
PRINCIPAL = PrincipalContext(issuer="fixture", subject="implementation-owner", client_id="codex", assurance="test")

@pytest.fixture
async def subject(database_prerequisite):
    url = os.getenv("TEST_DATABASE_URL") or pytest.skip("TEST_DATABASE_URL is required for implementation-request tests")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    state, works = PostgresState(engine), CanonicalWorkRepository(engine)
    await state.bind_reserved(PACKAGE, "local", str(PACKAGE))
    await works.create(CurrentWork(PACKAGE, "Approved package", False, "exact package", lifecycle_state="CURRENT"))
    grant = WorkGrant(id=uuid4(), version=1, principal=PRINCIPAL, authority=LaunchAuthority(active_work_id=PACKAGE), operations=frozenset({"implementation_request"}), issuer="fixture", provenance="direct assignment fixture", expires_at=datetime.now(UTC) + timedelta(hours=1))
    await GrantState(engine).issue(grant, None)
    occurrences = AsyncMock()
    occurrences.baseline_pass_status_in_transaction.return_value = BaselineReviewProof(
        status="NOT_PASS", reason="no matching baseline review"
    )
    occurrences.pass_status_in_transaction.return_value = "PASS"
    yield ImplementationRequestState(engine, works, occurrences), HumanReviewState(engine, works), engine, grant, occurrences
    await engine.dispose()

def consequence() -> HumanReviewConsequence:
    return HumanReviewConsequence(package_work_id=PACKAGE, package_revision=canonical_revision(PACKAGE, 1), implementation_scope=("Implement the exact reviewed package",), implementation_target="the package WorkId in an owned writer", excluded_effects=("deployment", "activation", "provider production writes"))


def v2_consequence(human_source_refs: tuple[str, ...] = ("Marco 2026-10-16",)) -> HumanReviewConsequence:
    return HumanReviewConsequence.model_validate({**consequence().model_dump(),
        "approval_effect": "CLEAR_HUMAN_REVIEW_FOR_NAMED_CONTINUATION",
        "baseline": {
            "human_source_refs": human_source_refs,
            "opening": {"what_you_asked": "Approved redesign", "what_this_does": "Binds admission", "what_this_does_not_or_changes_for_you": "No activation", "size": "600-1000 lines"},
            "approved_meaning": ("Preserve the approved baseline",),
            "protected_requirements": ("Only preserving review dispatches",),
            "approved_non_goals": ("No workflow engine",),
            "decision_context": {"size_or_burden": ("600-1000 lines",), "scale_assumptions": (), "material_risks_and_boundaries": ("Separate activation",)},
            "continuation_effects": ("IMPLEMENT", "LAND"),
        },
    })


async def approve(reviews: HumanReviewState) -> HumanReviewConsequence:
    value, proposed = consequence(), await reviews.propose(consequence())
    assert proposed.record is not None
    approved = await reviews.submit(proposed.record.consequence_id, PACKAGE, value.package_revision, "APPROVED")
    assert approved.status == "RECORDED"
    return value


async def request_count(engine) -> int:
    async with engine.connect() as connection:
        return await connection.scalar(select(func.count()).select_from(task_run_requests)) or 0


async def test_approved_current_send_authority_creates_one_inert_request(subject):
    facade, reviews, engine, grant, occurrences = subject
    approved, operation_id = await approve(reviews), uuid4()
    applied, recovered = await asyncio.gather(facade.request(PRINCIPAL, operation_id, PACKAGE, approved.package_revision), facade.request(PRINCIPAL, uuid4(), PACKAGE, approved.package_revision))
    assert (applied.status, recovered.status, applied.task_run_request_id) == ("APPLIED", "REPLAYED", recovered.task_run_request_id)
    assert occurrences.pass_status_in_transaction.call_args.args[1:] == (
        PACKAGE, approved.package_revision, "IMPLEMENTATION", None,
    )
    assert (applied.delivery_state, applied.next_action, await request_count(engine)) == ("PENDING", "await managed worker pickup", 1)
    async with engine.connect() as connection:
        row = (await connection.execute(select(task_run_requests))).mappings().one()
    assert (row["task_kind"], row["authorization_ref"], row["send_authority_ref"], row["execution_work_id"]) == ("IMPLEMENTATION", f"human-review/{approved.consequence_id}/{approved.digest}", f"grant/{grant.id}/1/{PRINCIPAL.key}", PACKAGE)
    contract = row["result_contract"]
    assert (contract["return_to_work_id"], contract["implementation_scope"], contract["excluded_effects"], contract["milestones"]) == (str(PACKAGE), list(approved.implementation_scope), list(approved.excluded_effects), ["IMPLEMENTED"])
    async with engine.begin() as connection:
        await connection.execute(update(task_run_requests).values(objective="corrupt"))
    corrupt = await facade.request(PRINCIPAL, operation_id, PACKAGE, approved.package_revision)
    assert (corrupt.status, corrupt.reason) == ("UNKNOWN", "state_unavailable")


async def test_invalid_currentness_and_authority_create_zero_requests(subject):
    facade, reviews, engine, grant, occurrences = subject
    revision = canonical_revision(PACKAGE, 1)
    denied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, revision)
    assert (denied.status, denied.reason) == ("DENIED", "human_review_not_approved")
    approved = await approve(reviews)
    denied = await facade.request(PRINCIPAL.model_copy(update={"subject": "other-agent"}), uuid4(), PACKAGE, revision)
    assert (denied.status, denied.reason) == ("DENIED", "no_send_authority")
    occurrences.baseline_pass_status_in_transaction.return_value = BaselineReviewProof(status="UNKNOWN", reason="unreadable")
    unavailable = await facade.request(PRINCIPAL, uuid4(), PACKAGE, revision)
    assert (unavailable.status, unavailable.reason, await request_count(engine)) == ("UNKNOWN", "baseline_review_unavailable", 0)
    occurrences.baseline_pass_status_in_transaction.return_value = BaselineReviewProof(status="NOT_PASS", reason="no matching baseline review")
    for review_status, expected in (("NOT_PASS", ("DENIED", "independent_review_not_passed")), ("UNKNOWN", ("UNKNOWN", "independent_review_unavailable"))):
        occurrences.pass_status_in_transaction.return_value = review_status
        denied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, revision)
        assert (denied.status, denied.reason) == expected
    occurrences.pass_status_in_transaction.return_value = "PASS"
    stale = await facade.request(PRINCIPAL, uuid4(), PACKAGE, "pg_stale")
    assert (stale.status, stale.reason) == ("STALE", "package_revision_changed")
    async with engine.begin() as connection:
        await connection.execute(update(canonical_work).values(completed=True))
    denied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, approved.package_revision)
    assert (denied.status, denied.reason) == ("DENIED", "package_not_active")
    async with engine.begin() as connection:
        await connection.execute(update(canonical_work).values(completed=False, lifecycle_state="TERMINAL"))
    denied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, approved.package_revision)
    assert (denied.status, denied.reason) == ("DENIED", "package_not_active")
    async with engine.begin() as connection:
        await connection.execute(update(canonical_work).values(lifecycle_state="CURRENT"))
    await GrantState(engine).issue(grant.model_copy(update={"id": uuid4(), "version": 2, "state": "revoked"}), 1)
    denied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, approved.package_revision)
    assert (denied.status, denied.reason, await request_count(engine)) == ("STALE", "send_authority_not_current", 0)


async def test_exact_replay_survives_revoke_but_changed_replay_conflicts(subject):
    facade, reviews, engine, grant, _ = subject
    approved, operation_id = await approve(reviews), uuid4()
    first = await facade.request(PRINCIPAL, operation_id, PACKAGE, approved.package_revision)
    await GrantState(engine).issue(grant.model_copy(update={"id": uuid4(), "version": 2, "state": "revoked"}), 1)
    replay = await facade.request(PRINCIPAL, operation_id, PACKAGE, approved.package_revision)
    changed = await facade.request(PRINCIPAL, operation_id, uuid4(), approved.package_revision)
    assert (replay.status, replay.task_run_request_id, changed.status, changed.reason, await request_count(engine)) == ("REPLAYED", first.task_run_request_id, "CONFLICT", "operation_identity_conflict", 1)


async def test_hold_review_creates_zero_requests(subject):
    facade, reviews, engine, _, _ = subject
    value, proposed = consequence(), await reviews.propose(consequence())
    assert proposed.record is not None
    held = await reviews.submit(proposed.record.consequence_id, PACKAGE, value.package_revision, "HOLD")
    denied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, value.package_revision)
    assert (held.status, denied.status, denied.reason, await request_count(engine)) == ("RECORDED", "DENIED", "human_review_not_approved", 0)


async def test_v2_baseline_and_exact_preimplementation_pass_bind_full_authorization(subject):
    facade, reviews, engine, grant, occurrences = subject
    approved = v2_consequence()
    proposed = await reviews.propose(approved)
    assert proposed.record is not None
    await reviews.submit(approved.consequence_id, PACKAGE, approved.package_revision, "APPROVED")
    async with engine.begin() as connection:
        await connection.execute(update(canonical_work).where(
            canonical_work.c.work_id == PACKAGE
        ).values(row_version=2))
    revision = canonical_revision(PACKAGE, 2)
    denied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, revision)
    assert (denied.status, denied.reason, await request_count(engine)) == ("DENIED", "baseline_review_not_passed", 0)
    review_id, verdict_digest = uuid4(), "a" * 64
    occurrences.baseline_pass_status_in_transaction.return_value = BaselineReviewProof(
        status="PASS", baseline_id=approved.consequence_id, phase="PRE_IMPLEMENTATION",
        review_id=review_id, verdict_digest=verdict_digest, reason="exact preserving review",
    )
    applied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, revision)
    assert (applied.status, await request_count(engine)) == ("APPLIED", 1)
    occurrences.pass_status_in_transaction.assert_not_called()
    async with engine.connect() as connection:
        row = (await connection.execute(select(task_run_requests))).mappings().one()
    expected_ref = f"human-review-v2/{approved.consequence_id}/{approved.digest}/review/{review_id}/{verdict_digest}"
    assert row["authorization_ref"] == expected_ref
    assert row["send_authority_ref"] == f"grant/{grant.id}/1/{PRINCIPAL.key}"
    contract = row["result_contract"]
    assert (contract["baseline_id"], contract["baseline_digest"], contract["preimplementation_review_id"], contract["preimplementation_verdict_digest"]) == (str(approved.consequence_id), approved.digest, str(review_id), verdict_digest)
    assert (contract["continuation_effects"], contract["milestones"], contract["excluded_effects"]) == (["IMPLEMENT", "LAND"], ["IMPLEMENTED", "MERGED"], list(approved.excluded_effects))


async def _trajectory(engine, predecessor=None, generation=1):
    return await HumanTrajectoryStore(engine).record(
        work_id=PACKAGE, append_request_id=uuid4(), expected_generation=generation,
        expected_predecessor_id=predecessor, source_kind=SourceKind.HUMAN_INPUT,
        source_ref="agent-captured human direction", source_revision=None,
        data=HumanTrajectoryData(
            outcome="Preserve the actual outcome without Marco attestation",
            current_slice="Implementation V1", remaining_outcome="Recheck when corrected",
        ),
    )


async def _approved_v2(reviews, occurrences, source_refs):
    approved = v2_consequence(source_refs)
    prepared = await reviews.propose(approved)
    assert prepared.status == "PREPARED"
    result = await reviews.submit(
        approved.consequence_id, PACKAGE, approved.package_revision, "APPROVED"
    )
    assert result.status == "RECORDED"
    review_id = uuid4()
    occurrences.baseline_pass_status_in_transaction.return_value = BaselineReviewProof(
        status="PASS", baseline_id=approved.consequence_id, phase="PRE_IMPLEMENTATION",
        review_id=review_id, verdict_digest="a" * 64,
        reason="exact preserving review",
    )
    return approved


async def test_opt_in_hihr_current_trajectory_allows_regular_approval(subject):
    facade, reviews, engine, _, occurrences = subject
    recorded = await _trajectory(engine)
    assert recorded.status == "APPLIED" and recorded.revision is not None
    marker = f"hihr/1.0/trajectory/{PACKAGE}/{recorded.revision.trajectory_id}"
    approved = await _approved_v2(reviews, occurrences, ("agent working source", marker))
    applied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, approved.package_revision)
    assert (applied.status, await request_count(engine)) == ("APPLIED", 1)
    async with engine.connect() as connection:
        row = (await connection.execute(select(task_run_requests))).mappings().one()
    assert row["result_contract"]["hihr_trajectory_ref"] == marker


async def test_opt_in_hihr_stale_trajectory_denies_without_request(subject):
    facade, reviews, engine, _, occurrences = subject
    first = await _trajectory(engine)
    assert first.revision is not None
    marker = f"hihr/1.0/trajectory/{PACKAGE}/{first.revision.trajectory_id}"
    approved = await _approved_v2(reviews, occurrences, ("agent working source", marker))
    second = await _trajectory(engine, first.revision.trajectory_id, 2)
    assert second.status == "APPLIED"
    denied = await facade.request(PRINCIPAL, uuid4(), PACKAGE, approved.package_revision)
    assert (denied.status, denied.reason, await request_count(engine)) == (
        "DENIED", "hihr_trajectory_stale", 0,
    )


@pytest.mark.parametrize("marker,expected_status,expected_reason", [
    ("hihr/2.0/trajectory/not-supported", "DENIED", "hihr_binding_invalid"),
    (f"hihr/1.0/trajectory/{PACKAGE}/{UUID('11111111-1111-4111-8111-111111111111')}",
     "UNKNOWN", "hihr_trajectory_unavailable"),
])
async def test_opt_in_hihr_invalid_or_missing_binding_never_dispatches(
    subject, marker, expected_status, expected_reason,
):
    facade, reviews, engine, _, occurrences = subject
    approved = await _approved_v2(reviews, occurrences, ("agent working source", marker))
    result = await facade.request(PRINCIPAL, uuid4(), PACKAGE, approved.package_revision)
    assert (result.status, result.reason, await request_count(engine)) == (
        expected_status, expected_reason, 0,
    )


@pytest.mark.parametrize("gate", ["revision", "completed", "lifecycle", "grant", "grant_version", "hold", "review", "unknown", "baseline", "trajectory", "human_hold", "dependency"])
async def test_bind_rechecks_admitted_implementation(subject, monkeypatch, gate):
    from test_task_runs import receipt

    from switchstand.human_reviews import human_review_consequences
    from switchstand.task_runs import task_run_executions

    facade, reviews, engine, grant, occurrences = subject
    if gate in {"baseline", "trajectory"}:
        first = await _trajectory(engine)
        marker = f"hihr/1.0/trajectory/{PACKAGE}/{first.revision.trajectory_id}"
        approved = await _approved_v2(reviews, occurrences, (marker,))
    else:
        approved = await approve(reviews)
    if gate == "dependency":
        dependency = uuid4()
        await PostgresState(engine).bind_reserved(dependency, "local", str(dependency))
        await facade.works.create(CurrentWork(dependency, "Dependency", False, "fixture", lifecycle_state="CURRENT"))
        async with engine.begin() as connection:
            await connection.execute(insert(work_dependencies).values(work_id=PACKAGE, depends_on_work_id=dependency))
    admitted = await facade.request(PRINCIPAL, uuid4(), PACKAGE, approved.package_revision)
    assert admitted.status == "APPLIED"
    monkeypatch.setattr("switchstand.reviews.ReviewOccurrenceState", lambda *args: occurrences)
    if gate in {"revision", "completed", "lifecycle", "hold"}:
        table = human_review_consequences if gate == "hold" else canonical_work
        changes = {"revision": {"row_version": 2}, "completed": {"completed": True},
                   "lifecycle": {"lifecycle_state": "UNKNOWN"},
                   "hold": {"decision": "HOLD", "state": "HOLD"}}
        async with engine.begin() as connection:
            await connection.execute(update(table).values(**changes[gate]))
    elif gate in {"grant", "grant_version"}:
        await GrantState(engine).issue(grant.model_copy(update={
            "id": uuid4(), "version": 2,
            "state": "revoked" if gate == "grant" else "active",
        }), 1)
    elif gate == "human_hold":
        await PriorityClaimRepository(engine).record(NewPriorityClaim(uuid4(), "HUMAN_PRIORITY", "WORK", PACKAGE, "HOLD", "Pause", "Marco fixture", "fixture:hold"))
    elif gate == "dependency":
        async with engine.begin() as connection:
            await connection.execute(update(canonical_work).where(canonical_work.c.work_id == dependency).values(lifecycle_state="TERMINAL"))
    elif gate == "trajectory":
        await _trajectory(engine, first.revision.trajectory_id, 2)
    elif gate == "baseline":
        occurrences.baseline_pass_status_in_transaction.return_value = BaselineReviewProof(
            status="NOT_PASS", reason="preservation evidence changed"
        )
    else:
        occurrences.pass_status_in_transaction.return_value = "UNKNOWN" if gate == "unknown" else "NOT_PASS"
    bound = await facade.tasks.bind_start(admitted.task_run_request_id, receipt(PACKAGE))
    assert bound.status == ("unknown" if gate in {"unknown", "dependency"} else "stale" if gate in {"revision", "grant", "grant_version"} else "denied")
    async with engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(task_run_executions)) == 0


@pytest.mark.skipif(os.getenv("SWITCHSTAND_NATIVE_IMPLEMENTATION_E2E") != "1", reason="explicit native E2E opt-in")
@pytest.mark.parametrize("blocked", [False, True])
async def test_native_implementation_launch(subject, tmp_path, monkeypatch, blocked):
    import sys
    import time
    from pathlib import Path

    import test_coordinator_workers as host
    from test_reviews import test_baseline_review_round_trip_binds_assessment_and_projects_proof

    from switchstand.agent_mailboxes import AgentMailboxState
    from switchstand.messages import MessageState
    from switchstand.reviews import ReviewOccurrenceState, ReviewPolicy
    from switchstand.task_runs import TaskRunState, task_run_executions

    facade, _, engine, _, _ = subject
    occurrences = ReviewOccurrenceState(facade.works, AgentMailboxState(engine), ReviewPolicy(version="review-policy-v1"))
    await test_baseline_review_round_trip_binds_assessment_and_projects_proof((
        occurrences, MessageState(engine, GrantState(engine)), occurrences.mailboxes, PACKAGE, engine,
    ))
    async with engine.connect() as connection:
        row = (await connection.execute(select(task_run_requests))).mappings().one()
        assert await connection.scalar(select(func.count()).select_from(task_run_executions)) == 0
    monkeypatch.setattr(host, "WORK", PACKAGE)
    home, primary, writer, base = host.repository(tmp_path, tmp_path / "unused-gate")
    control = home / ".local/state/switchstand/control/controls" / base
    host.git(control, "config", "user.name", "Worker Test")
    host.git(control, "config", "user.email", "worker@example.invalid")
    if blocked:
        async with engine.begin() as connection:
            await connection.execute(update(canonical_work).where(
                canonical_work.c.work_id == PACKAGE
            ).values(completed=True))
    # Only provisioning/development transport is adapted; reservation, bind,
    # supervision, native Codex, durable worker state and terminal result are real.
    code = r"""
import asyncio, os, subprocess, sys
from pathlib import Path
from uuid import UUID, uuid4
from switchstand import launch, provision
from switchstand.development import DevelopmentBoundary
from switchstand.mcp import server_from_env
from switchstand.task_run_bind import _bind
url, control, native_home = sys.argv[1:4]
args = launch.parser().parse_args(sys.argv[4:])
control = Path(control)
package = UUID(args.active)
writer = Path(os.environ["HOME"]) / ".local/state/switchstand/worktrees" / f"switchstand-task-{package}"
branch = f"v2-task-{package}"
subprocess.run(["git", "-C", str(control), "worktree", "add", "-q", "-b", branch, str(writer), args.commit], check=True)
git_dir = Path(subprocess.check_output(["git", "-C", str(writer), "rev-parse", "--absolute-git-dir"], text=True).strip())
def local_bind(*values):
    result = asyncio.run(_bind(args.task_request_id, values[3]))
    if result.status == "ok":
        assert asyncio.run(_bind(args.task_request_id, values[3])) == result  # Exact-ID lost-reply reconciliation.
    if result.status != "ok":
        raise RuntimeError(result.model_dump_json())
os.environ.update(DATABASE_URL=url, SWITCHSTAND_MANAGED="1", ACTIVE_WORK_ID=str(package))
def local_provision(*a, agent_task=False):
    asyncio.run(provision.run(args.active, (), managed_agent=True, agent_task=agent_task))
    return launch.Authority(package, ())
launch.provision = local_provision
launch.bind_task_start = local_bind
launch.prepare_development = lambda *a: DevelopmentBoundary("fixture", "fixture", "fixture", "fixture")
launch.cleanup_development = lambda *a: None
prepared = launch.prepare_managed_run(control, writer, branch, args.active, (), dict(os.environ), git_dir, package, args.task_request_id, args.agent_task)
os.environ.update(SWITCHSTAND_RUN_ID=str(prepared.receipt.run_id), SWITCHSTAND_WORKTREE=str(writer), SWITCHSTAND_BRANCH=branch, SWITCHSTAND_GIT_DIR=str(git_dir))
assert "agent_task_result" in " ".join(launch.codex_command(control, writer, [], agent_task=args.agent_task))
(writer / "AGENTS.md").write_text("This file is the sole project instruction source. Do not read global CLAUDE.md. No fanout. Only create native.txt. The fixture supervisor commits the output.\n")
env = dict(os.environ, HOME=native_home, SWITCHSTAND_WORKTREE=str(writer))
command = ["/usr/bin/timeout", "--kill-after=2", "90", native_home + "/.local/state/switchstand/codex/updater-bin/bin/codex", "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "workspace-write", "-C", str(writer), "-c", "mcp_servers={}", "Create native.txt containing native implementation proof. Do not commit; the fixture supervisor commits your output. Do no other work. Do not use subagents or MCP."]
(git_dir / "native-started").write_text(str(prepared.receipt.run_id))
status = launch.supervise_codex(command, env, prepared.development, prepared.receipt.run_id)
if status:
    raise SystemExit(status)
subprocess.run(["git", "-C", str(writer), "add", "native.txt", "AGENTS.md"], check=True)
subprocess.run(["git", "-C", str(writer), "commit", "-q", "-m", "Native implementation fixture"], check=True)
head = subprocess.check_output(["git", "-C", str(writer), "rev-parse", "HEAD"], text=True).strip()
async def finish():
    result = await server_from_env().call_tool("agent_task_result", dict(api_version="1", request_id=str(args.task_request_id), result_id=str(uuid4()), outcome="PASS", summary="Native implementer candidate", evidence_refs=["git:" + head]))
    assert result.structured_content["status"] == "ok", result
asyncio.run(finish())
"""
    subject = host.CoordinatorWorkers(home, state_root=tmp_path / "worker-state",
        runtime_source=host.SOURCE / "src", launcher=("/usr/bin/env", "PYTHONPATH=" + str(host.SOURCE / "src"), sys.executable, "-c", code,
        engine.url.render_as_string(hide_password=False), str(control), os.environ["HOME"]))
    operation = uuid4()
    starts = await asyncio.gather(*(asyncio.to_thread(
        subject.spawn, operation, PACKAGE, "native implementation fixture", row["request_id"],
    ) for _ in range(2)))
    assert sorted(value.status for value in starts) == ["replayed", "started"]
    assert subject.spawn(operation, PACKAGE, "native implementation fixture", uuid4()).reason == "operation_identity_conflict"
    deadline = time.monotonic() + 180
    while (observed := subject.status(operation)).status == "running" and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
    record = subject.store.load(operation)
    git_dir = Path(host.git(writer, "rev-parse", "--absolute-git-dir"))
    assert (git_dir / "native-started").exists() is not blocked
    assert observed.status == ("failed" if blocked else "completed"), Path(record.log).read_text()[-4000:]
    stored = await TaskRunState(engine, occurrences.works).get(row["request_id"])
    assert (stored.request.terminal_result_id is None) is blocked
    if not blocked:
        evidence = await TaskRunState(engine, occurrences.works).terminal_evidence(stored.request.terminal_result_id)
        assert evidence.result.evidence_refs == ("git:" + observed.candidate_sha,)
        assert (writer / "native.txt").read_text().strip() == "native implementation proof"
    for revision in ("44d59bd1f6de96c6a05b39402a5d763694fd774e", "5dc19e230fa03619325595c8c6ad6d692d365299"):
        legacy: dict = {"__name__": "switchstand.coordinator_worker_state", "__package__": "switchstand"}
        exec(host.git(host.SOURCE, "show", revision + ":src/switchstand/coordinator_worker_state.py"), legacy)  # noqa: S102 - exact pinned local source
        assert legacy["WorkerStore"](home, subject.store.root).load(operation) is not None
        exec(host.git(host.SOURCE, "show", revision + ":src/switchstand/coordinator_workers.py"), legacy)  # noqa: S102 - exact pinned local source
        previous = legacy["CoordinatorWorkers"](home, state_root=subject.store.root)
        assert previous.spawn(operation, PACKAGE, "native implementation fixture").status == "replayed"
        previous.store.write(previous.store.load(operation).model_copy(update={"phase": "PREPARED", "cancelled": False}), replace=True)
        assert previous.cancel(operation).status == "cancelled"
        assert subject.store.load(operation).command == record.command
        assert subject.spawn(operation, PACKAGE, "native implementation fixture", row["request_id"]).status == "replayed"
    assert host.git(primary, "rev-parse", "HEAD") == base
