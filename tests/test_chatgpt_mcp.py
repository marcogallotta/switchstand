import sys
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from chatgpt_fixture import (
    ACTIVE,
    PRINCIPAL,
    REFERENCE,
    assert_public,
    grant,
    read_chain,
    service,
)
from mcp import Client, StdioServerParameters
from pydantic import ValidationError

from switchstand import chatgpt_mcp
from switchstand.agent_mailboxes import AgentMailbox, AgentMailboxResult
from switchstand.chatgpt_mcp import (
    ORDINARY_EFFECT_TOOLS,
    ORDINARY_GENUINE_READ_TOOLS,
    ORDINARY_NON_IDEMPOTENT_TOOLS,
    OrdinaryRelationPatch,
    OrdinaryWorkResult,
    build_chatgpt_server,
    build_ordinary_tools,
)
from switchstand.contracts import WorkResolveReferenceRequest
from switchstand.grants import (
    GrantResult,
    GuardOutcome,
    PrincipalContext,
    ProtectedAppend,
    ProtectedCreate,
)
from switchstand.implementation_requests import ImplementationRequestResult
from switchstand.outcome_state import ActionSummary, OutcomeAction, OutcomeWrite
from switchstand.priority_claim_service import HumanPriorityClear, HumanPrioritySet
from switchstand.repository_bundle import RepositoryBundleResolution
from switchstand.repository_candidate import QualificationGate, RepositoryCandidateQualification
from switchstand.reviews import ReviewBasis, ReviewBundleAccess


def test_ordinary_annotation_policy_is_exhaustive():
    tool_names = {name for name, _ in build_ordinary_tools(service())}
    assert ORDINARY_GENUINE_READ_TOOLS.isdisjoint(ORDINARY_EFFECT_TOOLS)
    assert ORDINARY_GENUINE_READ_TOOLS | ORDINARY_EFFECT_TOOLS == tool_names
    assert ORDINARY_NON_IDEMPOTENT_TOOLS == set()
    assert ORDINARY_NON_IDEMPOTENT_TOOLS <= ORDINARY_EFFECT_TOOLS


def test_review_bundle_tool_is_default_off():
    subject = service()
    subject.reviews = object()  # type: ignore[assignment]
    assert "review_bundle_get" not in dict(build_ordinary_tools(subject))
    assert "review_bundle_get" in dict(build_ordinary_tools(
        subject, review_bundle_enabled=True,
    ))


async def test_review_bundle_rejects_unknown_gates_and_candidate_race(monkeypatch):
    review_id, subject_id, endpoint_id = uuid4(), uuid4(), uuid4()
    mailbox = AgentMailbox(name="Reviewer", name_key="reviewer", endpoint_id=endpoint_id,
                           principal_key=PRINCIPAL.key, session_key="session", generation=1)
    state = SimpleNamespace(for_actor=AsyncMock(return_value=AgentMailboxResult(
        status="ok", mailbox=mailbox,
    )))
    monkeypatch.setattr(chatgpt_mcp, "AgentMailboxState", lambda _engine: state)
    basis = ReviewBasis(review_id=review_id, subject_work_id=subject_id, subject_revision="r1",
                        review_kind="CODE", candidate_ref="github:marcogallotta/switchstand:pr/7",
                        mode="FULL", requester_endpoint_id=uuid4(), requester_generation=1,
                        policy_version="p1", guidelines_version="g1", guidelines_digest="0" * 64)
    access = ReviewBundleAccess("AUTHORIZED", basis=basis)
    subject = service()
    subject.messages = SimpleNamespace(engine=object())
    subject.reviews = SimpleNamespace(bundle_access=AsyncMock(return_value=access))
    gate = QualificationGate(name="Exact-head Quality", subject_kind="exact_head",
                             subject_sha="a" * 40, state="completed", conclusion="failure",
                             reason="failed", detail_reason="provider-unavailable")
    unknown = RepositoryCandidateQualification(status="NOT_READY", pull_request=7,
        base_sha="b" * 40, head_sha="a" * 40, composition_sha="c" * 40,
        composition_parents=["b" * 40, "a" * 40], gates=[gate], reason="gates_not_ready")
    qualify = AsyncMock(return_value=unknown)
    monkeypatch.setattr(chatgpt_mcp.repository_candidate, "qualify_repository_candidate", qualify)
    resolve = AsyncMock(return_value=RepositoryBundleResolution(
        status="current", bundle_url="https://example.invalid/bundle", required_sha="a" * 40,
    ))
    monkeypatch.setattr(chatgpt_mcp.repository_bundle, "resolve_repository_bundle", resolve)
    tool = dict(build_ordinary_tools(subject, agent_identity=lambda: "session",
                                     review_bundle_enabled=True))["review_bundle_get"]
    result = await tool("1", review_id)
    assert result.structured_content["status"] == "UNKNOWN" and len(result.content) == 1
    resolve.assert_not_awaited()

    gates = [QualificationGate(name=name, subject_kind=kind,
        subject_sha="a" * 40 if kind == "exact_head" else "c" * 40,
        state="in_progress", reason="running") for name, kind in (
            ("Exact-head Quality", "exact_head"), ("PR composition Quality", "composition"))]
    current = unknown.model_copy(update={"gates": gates})
    duplicate = current.model_copy(update={"gates": [gates[0], gates[0]]})
    contradictory = current.model_copy(update={"status": "READY", "reason": None})
    stale_gate = current.model_copy(update={"gates": [
        gates[0].model_copy(update={"reason": "wrong-head"}), gates[1],
    ]})
    ambiguous_stale = stale_gate.model_copy(update={"gates": [
        stale_gate.gates[0].model_copy(update={"detail_reason": "provider-unavailable"}), gates[1],
    ]})
    changed = current.model_copy(update={"head_sha": "d" * 40})
    qualify.side_effect = [duplicate, contradictory, ambiguous_stale, stale_gate, current, changed]
    for _ in range(3):
        result = await tool("1", review_id)
        assert result.structured_content["status"] == "UNKNOWN" and len(result.content) == 1
    result = await tool("1", review_id)
    assert result.structured_content["status"] == "STALE" and len(result.content) == 1
    result = await tool("1", review_id)
    assert result.structured_content["status"] == "STALE" and len(result.content) == 1


def test_priority_claim_tools_are_default_off_and_human_only():
    subject = service()
    assert not {name for name, _ in build_ordinary_tools(subject)} & {
        "priority_claim_get", "priority_claim_record",
    }
    subject.priority_claims_enabled = True
    subject.priority_claims = object()  # type: ignore[assignment]
    tools = dict(build_ordinary_tools(subject))
    assert {"priority_claim_get", "priority_claim_record"} <= tools.keys()
    parameters = signature(tools["priority_claim_record"]).parameters
    assert {"action", "subject_kind", "subject_id", "claim_id"} <= parameters.keys()
    assert "work_id" not in parameters
    assert "grant_version" not in parameters
    assert "claim_kind" not in parameters
    assert "source_label" not in parameters


async def test_ordinary_priority_claim_record_derives_set_and_clear(monkeypatch):
    subject = service()
    subject.priority_claims_enabled = True
    subject.priority_claims = object()  # type: ignore[assignment]
    recorded = AsyncMock(return_value=subject.denied("priority_claim_record", "probe"))
    monkeypatch.setattr(subject, "priority_claim_record", recorded)
    tool = dict(build_ordinary_tools(subject))["priority_claim_record"]
    operation, claim = uuid4(), uuid4()

    await tool(
        api_version="1", operation_id=operation, action="SET",
        subject_kind="WORK", subject_id=ACTIVE, observed_revision="r1",
        relation_kind="BAND", rationale="Marco says high", band="HIGH",
    )
    request = recorded.await_args.args[0]
    assert isinstance(request, HumanPrioritySet)
    assert request.subject_id == ACTIVE and request.band == "HIGH"

    recorded.reset_mock()
    await tool(
        api_version="1", operation_id=operation, action="CLEAR",
        subject_kind="WORK", subject_id=ACTIVE, observed_revision="r1",
        claim_id=claim,
    )
    request = recorded.await_args.args[0]
    assert isinstance(request, HumanPriorityClear)
    assert request.claim_id == claim

    with pytest.raises(ValueError, match="CLEAR requires only"):
        await tool(
            api_version="1", operation_id=uuid4(), action="CLEAR",
            subject_kind="WORK", subject_id=ACTIVE, observed_revision="r1",
            claim_id=claim, rationale="caller must not restate relation",
        )


def test_priority_context_tool_is_default_off_and_explicitly_bounded():
    subject = service()
    assert "priority_context_get" not in dict(build_ordinary_tools(subject))
    subject.priority_context_enabled = True
    subject.priority_context = object()  # type: ignore[assignment]
    tool = dict(build_ordinary_tools(subject))["priority_context_get"]
    assert set(signature(tool).parameters) == {"api_version", "work_ids"}


async def test_implementation_request_is_default_off_and_delegates_exact_identity():
    subject = service()
    assert "implementation_request" not in dict(build_ordinary_tools(subject))
    subject.implementation_requests = AsyncMock()
    expected = ImplementationRequestResult(status="DENIED", reason="probe")
    subject.implementation_requests.request.return_value = expected
    operation_id = uuid4()
    tool = dict(build_ordinary_tools(subject))["implementation_request"]
    result = await tool("1", operation_id, ACTIVE, "pg_exact")
    assert result == expected
    subject.implementation_requests.request.assert_awaited_once_with(
        PRINCIPAL, operation_id, ACTIVE, "pg_exact"
    )


async def test_product_currentness_tool_is_default_off_and_server_owned():
    subject = service()
    assert "product_currentness_get" not in dict(build_ordinary_tools(subject))
    expected = object()
    callback = AsyncMock(return_value=expected)
    subject.product_currentness_enabled = True
    subject.product_currentness = callback  # type: ignore[assignment]
    tool = dict(build_ordinary_tools(subject))["product_currentness_get"]

    assert set(signature(tool).parameters) == {"api_version"}
    assert await tool(api_version="1") is expected
    callback.assert_awaited_once_with(PRINCIPAL)


async def test_product_currentness_tool_requires_authenticated_principal():
    subject = service()
    callback = AsyncMock()
    subject.principal = AsyncMock(return_value=None)
    subject.product_currentness_enabled = True
    subject.product_currentness = callback  # type: ignore[assignment]

    with pytest.raises(PermissionError, match="authenticated principal"):
        await dict(build_ordinary_tools(subject))["product_currentness_get"](api_version="1")
    callback.assert_not_awaited()


async def test_chatgpt_update_rejects_empty_patch_before_handler(monkeypatch):
    subject = service()
    update = AsyncMock(side_effect=AssertionError("invalid patch reached update handler"))
    monkeypatch.setattr(subject, "update", update)

    async with Client(build_chatgpt_server(subject)) as client:
        result = await client.call_tool("work_update", {
            "api_version": "1", "operation_id": str(uuid4()), "work_id": str(ACTIVE),
            "observed_revision": "r1", "patch": {},
        })

    assert result.is_error is True
    assert result.structured_content is None
    assert any("patch must not be empty" in getattr(item, "text", "")
               for item in result.content)
    update.assert_not_awaited()
    assert subject.providers["asana"].sends == 0


async def test_work_create_routes_provider_neutral_project_target(monkeypatch):
    subject = service()
    subject.admission_grants.grant = grant(operations=frozenset({"work_create"}))
    create = AsyncMock(return_value=subject.denied("work_create", "probe"))
    monkeypatch.setattr(subject, "create", create)
    operation_id, project_id = uuid4(), uuid4()

    result = await dict(build_ordinary_tools(subject))["work_create"](
        api_version="1", operation_id=operation_id, title="Independent",
        project_id=project_id, canonical_root=str(ACTIVE),
    )

    assert result.reason == "probe"
    request = create.await_args.args[0]
    assert request.parent_work_id is None and request.project_id == project_id
    assert request.project_gid is None and request.canonical_root == str(ACTIVE)


async def test_canonical_create_derives_self_owner_from_registered_mailbox(monkeypatch):
    subject = service()
    subject.canonical_work_active = True
    subject.admission_grants.grant = grant(operations=frozenset({"work_create"}))
    subject.messages = SimpleNamespace(engine=object())
    mailbox = AgentMailbox(
        name="Root", name_key="root", endpoint_id=uuid4(),
        principal_key=PRINCIPAL.key, session_key="session", generation=1,
    )
    lookup = SimpleNamespace(for_actor=AsyncMock(return_value=AgentMailboxResult(
        status="ok", mailbox=mailbox,
    )))
    monkeypatch.setattr(chatgpt_mcp, "AgentMailboxState", lambda _engine: lookup)
    create = AsyncMock(return_value=subject.denied("work_create", "probe"))
    monkeypatch.setattr(subject, "create", create)
    tool = dict(build_ordinary_tools(subject, agent_identity=lambda: "session"))["work_create"]

    await tool(
        api_version="1", operation_id=uuid4(), title="Child", parent_work_id=ACTIVE,
        work_type="Task", lifecycle_state="CURRENT", owner_key="SELF",
        wait_kind="NONE", unblock_condition="NONE", next_due="NONE",
        next_action_class="OWNER_CAN_DO", next_action_ref="implement",
    )
    request = create.await_args.args[0]
    assert request.owner_key == "agent:root"
    assert request.work_type == "Task" and request.lifecycle_state == "CURRENT"
    assert (request.wait_kind, request.unblock_condition, request.next_due) == (
        "NONE", "NONE", "NONE",
    )
    assert (request.next_action_class, request.next_action_ref) == (
        "OWNER_CAN_DO", "implement",
    )

    denied = await tool(
        api_version="1", operation_id=uuid4(), title="Wrong owner", parent_work_id=ACTIVE,
        work_type="Task", lifecycle_state="CURRENT", owner_key="agent:root",
        wait_kind="NONE", unblock_condition="NONE", next_due="NONE",
        next_action_class="OWNER_CAN_DO", next_action_ref="implement",
    )
    assert denied.reason == "canonical_owner_must_be_self"

async def test_stateful_switch_serializes_exact_owner_actions_and_preserves_unknown(monkeypatch):
    subject = service()
    subject.outcome_state_enabled = True
    subject.grants.grant = grant(
        operations=frozenset({"work_get", "work_update"}),
        update_qualification="test:ordinary-workspace",
    )

    class Outcomes:
        def __init__(self):
            self.owner = None
            self.items = ()

        async def record(self, **values):
            self.owner, self.items = values["owner_work_id"], values["items"]
            if any(item.source_label == "MARCO" for item in self.items):
                return OutcomeWrite("DENIED")
            assert values["active_work_id"] == self.owner
            assert values["owner_currentness_token"] == "r1"
            return OutcomeWrite("APPLIED", uuid4())

        async def summary(self, owner, token):
            if owner != self.owner:
                return None
            return ActionSummary(
                currentness="CURRENT" if token == "r1" else "STALE",
                open_action_count=1,
                actions=(OutcomeAction(
                    action_class="NEEDS_MARCO", item_key="rollout",
                    description="Choose rollout", what_yes_causes="Dispatch implementation",
                ),),
            )

    subject.state.outcomes = Outcomes()
    server = build_chatgpt_server(subject)
    listed = {tool.name: tool for tool in await server.list_tools()}
    assert "outcome_state_update" in listed
    assert next(iter(listed["work_get"].output_schema["properties"])) == "action_summary"
    operation = uuid4()
    item = {
        "item_key": "rollout", "description": "Choose rollout", "kind": "DECISION",
        "status": "READY", "who_acts": "MARCO",
        "what_yes_causes": "Dispatch implementation", "source_label": "AGENT",
    }
    written = await server.call_tool("outcome_state_update", {
        "api_version": "1", "operation_id": str(operation),
        "owner_work_id": str(ACTIVE), "expected_state_id": None,
        "owner_observed_revision": "r1", "items": [item],
    })
    assert written.structured_content["status"] == "APPLIED"
    got = await server.call_tool("work_get", {
        "api_version": "1", "work_id": str(ACTIVE),
    })
    assert got.structured_content["action_summary"] == {
        "currentness": "CURRENT", "open_action_count": 1,
        "actions": [{
            "action_class": "NEEDS_MARCO", "item_key": "rollout",
            "description": "Choose rollout", "dispatch_work_id": None,
            "what_yes_causes": "Dispatch implementation",
        }],
    }
    unrelated = await server.call_tool("work_get", {
        "api_version": "1", "work_id": str(REFERENCE),
    })
    assert "action_summary" not in unrelated.structured_content

    denied = await server.call_tool("outcome_state_update", {
        "api_version": "1", "operation_id": str(uuid4()),
        "owner_work_id": str(ACTIVE), "expected_state_id": None,
        "owner_observed_revision": "r1",
        "items": [item | {"source_label": "MARCO"}],
    })
    assert denied.structured_content["status"] == "DENIED"
    unadmitted = await server.call_tool("outcome_state_update", {
        "api_version": "1", "operation_id": str(uuid4()),
        "owner_work_id": str(uuid4()), "expected_state_id": None,
        "owner_observed_revision": "r1", "items": [item],
    })
    assert unadmitted.structured_content["status"] == "DENIED"

    updated = await server.call_tool("work_update", {
        "api_version": "1", "operation_id": str(uuid4()), "work_id": str(ACTIVE),
        "observed_revision": "r1", "patch": {"completed": True},
    })
    assert updated.structured_content["status"] == "ok"
    assert updated.structured_content["action_summary"]["currentness"] == "STALE"
    assert updated.structured_content["action_summary"]["actions"][0][
        "action_class"
    ] == "NEEDS_MARCO"

    async def unknown(_request):
        return GuardOutcome(
            status="unknown", operation="work_update", work_id=ACTIVE,
            operation_id=uuid4(), reason="lost_response", effect="unknown",
            retry="reconcile", next_action="Reconcile the effect.",
        )

    monkeypatch.setattr(subject, "update", unknown)
    update = await server.call_tool("work_update", {
        "api_version": "1", "operation_id": str(uuid4()), "work_id": str(ACTIVE),
        "observed_revision": "r1", "patch": {"completed": True},
    })
    assert update.structured_content["status"] == "unknown"
    assert "action_summary" not in update.structured_content


async def test_candidate_qualification_adapter_is_read_only_and_audited(monkeypatch):
    observed = []
    expected = RepositoryCandidateQualification(
        status="NOT_READY", pull_request=7, gates=[], reason="gates_not_ready",
    )

    async def qualify(pull_request, include_failure_detail):
        assert (pull_request, include_failure_detail) == (7, True)
        return expected

    monkeypatch.setattr(
        "switchstand.chatgpt_mcp.repository_candidate.qualify_repository_candidate", qualify,
    )
    tool = dict(build_ordinary_tools(
        service(), audit=lambda name, target, status: observed.append((name, target, status)),
    ))["repository_candidate_qualification_get"]
    assert await tool("1", 7, True) == expected
    assert observed == [("repository_candidate_qualification_get", "7", "NOT_READY")]
    assert "repository_candidate_qualification_get" in ORDINARY_GENUINE_READ_TOOLS


def test_ordinary_relation_patch_converts_provider_neutral_targets():
    patch = OrdinaryRelationPatch(kind="dependency", action="add", target_work_id=ACTIVE)
    converted = patch.internal()
    assert (converted.kind, converted.action, converted.target_work_id) == (
        "dependency", "add", ACTIVE,
    )
    with pytest.raises(ValidationError):
        OrdinaryRelationPatch.model_validate({"kind": "workset", "action": "move"})
    with pytest.raises(ValidationError):
        OrdinaryRelationPatch.model_validate({
            "kind": "parent", "action": "set", "target_work_id": ACTIVE,
            "project_gid": "123",
        })
    project_id = uuid4()
    placement = OrdinaryRelationPatch(
        kind="placement", action="add", project_id=project_id,
    ).internal()
    assert placement.project_id == project_id
    assert placement.project_gid is None and placement.section_gid is None
    for invalid in (
        {"kind": "placement", "action": "move", "project_id": project_id},
        {"kind": "placement", "action": "add"},
        {"kind": "placement", "action": "add", "project_id": project_id,
         "target_work_id": ACTIVE},
        {"kind": "dependency", "action": "add", "target_work_id": ACTIVE,
         "project_id": project_id},
    ):
        with pytest.raises(ValidationError):
            OrdinaryRelationPatch.model_validate(invalid)


@pytest.mark.parametrize("field", ["principal", "role", "grant", "allowed_operations"])
def test_append_cannot_accept_authority_arguments(field):
    values = {'api_version': "1", 'operation_id': uuid4(), 'work_id': ACTIVE, 'grant_version': 1, 'observed_revision': "r1", 'text': "feedback"}
    with pytest.raises(ValidationError):
        ProtectedAppend.model_validate(values | {field: "owner"})
    create = {'api_version': "1", 'operation_id': uuid4(), 'parent_work_id': ACTIVE,
              'grant_version': 1, 'title': "child"}
    with pytest.raises(ValidationError):
        ProtectedCreate.model_validate(create | {field: "owner"})


async def test_ordinary_facade_preserves_unknown_admission_without_sending(monkeypatch):
    subject = service()
    tools = dict(build_ordinary_tools(subject))

    async def unavailable():
        return GrantResult(status="unknown", principal=PRINCIPAL)

    monkeypatch.setattr(subject, "admission_get", unavailable)
    monkeypatch.setattr(subject, "grant_get", unavailable)
    operation_id = uuid4()
    append = await tools["work_append"](
        "1", operation_id, ACTIVE, "r1", "feedback", "investigation"
    )
    assert (append.status, append.effect, append.reason) == (
        "unknown", "not_sent", "admission_state_unavailable"
    )
    assert subject.providers["asana"].sends == 0

async def test_exceptional_work_purpose_is_preserved_in_audit() -> None:
    records: list[tuple[str, str | None, str]] = []
    tools = dict(build_ordinary_tools(service(), lambda *record: records.append(record)))

    await tools["work_history"]("1", ACTIVE, "r1", "recovery")
    await tools["work_event"]("1", uuid4(), "r1", ACTIVE, "legacy_reconciliation")
    await tools["work_append"](
        "1", uuid4(), ACTIVE, "r1", "provenance evidence", "provenance"
    )

    assert any(
        tool == "work_history" and target == f"{ACTIVE}:purpose=recovery"
        for tool, target, _ in records
    )
    assert any(
        tool == "work_event" and target == f"{ACTIVE}:purpose=legacy_reconciliation"
        for tool, target, _ in records
    )
    assert any(
        tool == "work_append" and target == f"{ACTIVE}:purpose=provenance"
        for tool, target, _ in records
    )


async def test_timing_correlation_uses_typed_work_targets_at_tool_entry(monkeypatch) -> None:
    subject = service()
    targets = []
    tools = dict(build_ordinary_tools(subject, correlate_work=targets.append))

    await tools["work_history"]("1", ACTIVE, "r1", "recovery")
    await tools["work_event"]("1", uuid4(), "r1", ACTIVE, "legacy_reconciliation")
    await tools["work_append"]("1", uuid4(), ACTIVE, "r1", "evidence", "provenance")
    await tools["work_create"](
        api_version="1", operation_id=uuid4(), title="Project child", project_id=uuid4(),
    )
    assert targets == [ACTIVE, ACTIVE, ACTIVE, None]

    for status in ("unknown", "denied"):
        monkeypatch.setattr(
            subject, "admission_get",
            AsyncMock(return_value=GrantResult(status=status, principal=PRINCIPAL)),
        )
        result = await tools["work_append"](
            "1", uuid4(), ACTIVE, "r1", "early return", "investigation",
        )
        assert result.status == status
        assert targets[-1] == ACTIVE


async def test_each_call_resolves_the_caller_again_and_does_not_self_take():
    subject = service()
    assert (await subject.get()).status == "ok"

    async def another():
        return PrincipalContext(issuer="fixture", subject="reviewer", client_id="local-test",
                                assurance="test")
    subject.principal = another
    assert (await subject.get(ACTIVE)).status == "denied"
    assert (await subject.grant_get()).status == "denied"

    async def absent():
        return None
    subject.principal = absent
    assert (await subject.get(ACTIVE)).status == "denied"


async def test_workspace_search_requires_explicit_operation_and_returns_only_work_ids():
    subject = service()
    subject.grants.grant = grant(
        scope="launch",
        operations=frozenset({"work_get", "work_search"}),
        append_qualification=None,
    )
    denied = await build_chatgpt_server(subject).call_tool(
        "work_search", {"api_version": "1"}
    )
    assert denied.structured_content["status"] == "denied"
    assert subject.providers["asana"].search_calls == []

    subject.grants.grant = grant(
        scope="workspace",
        operations=frozenset({"work_get", "work_search"}),
        append_qualification=None,
    )
    result = await build_chatgpt_server(subject).call_tool(
        "work_search", {"api_version": "1", "text": "Task", "limit": 10}
    )
    value = result.structured_content
    assert value["status"] == "ok" and len(value["items"]) == 1
    assert value["items"][0]["title"] == "Task"
    assert value["items"][0]["context"] == {
        "assignee": "Ada", "placements": [{"area": "Engineering", "stage": "Doing"}],
    }
    assert "provider" not in value["items"][0] and "task_gid" not in value["items"][0]
    assert subject.providers["asana"].search_calls == [("Task", None, None, 10)]

    subject.grants.grant = grant(
        scope="workspace", operations=frozenset({"work_get"}), append_qualification=None,
    )
    denied = await build_chatgpt_server(subject).call_tool(
        "work_search", {"api_version": "1"}
    )
    assert denied.structured_content["status"] == "denied"
    assert subject.providers["asana"].search_calls == [("Task", None, None, 10)]


async def test_launch_reference_denies_unbound_canonical_task_without_binding(monkeypatch):
    from unittest.mock import AsyncMock

    subject = service()
    provider = subject.providers["asana"]
    provider.canonical_ids.add("789")
    before = dict(subject.state.handles)
    with monkeypatch.context() as patch:
        get = AsyncMock(side_effect=AssertionError("launch denial must precede provider read"))
        patch.setattr(provider, "get", get)
        result = await subject.resolve_reference(
            WorkResolveReferenceRequest(api_version="1", reference="789")
        )
    assert result.status == "denied"
    assert result.guard is not None
    assert result.guard.operation == "work_resolve_reference"
    assert result.guard.reason == "reference_not_granted"
    assert result.guard.next_action == "Use a reference admitted by the current work grant."
    assert subject.state.handles == before
    get.assert_not_awaited()


async def test_workspace_reference_binds_once_revalidates_and_stays_provider_neutral():
    subject = service()
    subject.grants.grant = grant(
        scope="workspace", operations=frozenset({"work_get"}), append_qualification=None,
    )
    provider = subject.providers["asana"]
    server = build_chatgpt_server(subject)

    initially_denied = await server.call_tool(
        "work_resolve_reference", {"api_version": "1", "reference": "789"}
    )
    assert initially_denied.structured_content["guard"] == {
        "status": "denied",
        "operation": "work_get",
        "reason": "reference_not_admitted",
        "next_action": "Use a reference admitted by the authenticated workspace.",
        "effect": "not_sent",
        "retry": "none",
    }
    assert len(subject.state.handles) == 2

    provider.canonical_ids.add("789")

    first = await server.call_tool("work_resolve_reference", {
        "api_version": "1", "reference": "https://app.asana.com/0/42/789/f",
    })
    assert_public(first.model_dump(mode="json"))
    first_item = first.structured_content["item"]
    assert first.structured_content["status"] == "ok" and first_item["title"] == "Task"
    assert len(subject.state.handles) == 3

    repeat = await server.call_tool(
        "work_resolve_reference", {"api_version": "1", "reference": "789"}
    )
    assert repeat.structured_content["item"]["id"] == first_item["id"]
    assert len(subject.state.handles) == 3

    provider.canonical_ids.remove("789")
    denied = await server.call_tool(
        "work_resolve_reference", {"api_version": "1", "reference": "789"}
    )
    assert denied.structured_content == {
        "status": "denied", "item": None, "guard": {
            "status": "denied",
            "operation": "work_get",
            "reason": "reference_not_admitted",
            "effect": "not_sent",
            "retry": "none",
            "next_action": "Use a reference admitted by the authenticated workspace.",
        },
    }


async def test_reference_rejects_unrecognized_syntax_without_provider_read(monkeypatch):
    from unittest.mock import AsyncMock

    subject = service()
    get = AsyncMock(side_effect=AssertionError("invalid syntax must be closed before provider read"))
    monkeypatch.setattr(subject.providers["asana"], "get", get)
    result = await subject.resolve_reference(WorkResolveReferenceRequest(
        api_version="1", reference="https://example.com/task/123",
    ))
    assert result.status == "unknown" and result.item is None
    get.assert_not_awaited()


async def test_real_stdio_surface_has_no_issuer_or_identity_argument():
    parameters = StdioServerParameters(command=sys.executable,
        args=[str(Path(__file__)), "serve"], env={"PYTHONPATH": str(Path.cwd() / "src")})
    async with Client(parameters) as client:
        tools = (await client.list_tools()).tools
        assert {t.name for t in tools} == {
            "repository_bundle_get", "repository_candidate_qualification_get", "work_get", "work_search", "work_resolve_reference",
            "work_history", "work_event", "work_append", "capability_preflight_get",
            "work_create", "work_update", "work_relate",
            "task_control_get", "task_control_checkpoint", "work_hygiene_check",
            "agent_register", "agent_takeover", "agent_transfer_request",
            "agent_message_send", "agent_message_pending",
            "agent_message_receive", "agent_message_recover",
            "agent_message_result_send", "agent_message_disposition",
            "required_result_save",
        }
        for tool in tools:
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.idempotent_hint is True
            assert tool.annotations.open_world_hint is False
            if tool.name in {"work_get", "work_resolve_reference", "work_history", "work_event"}:
                assert_public(tool.model_dump(mode="json"))
            if tool.name == "repository_candidate_qualification_get":
                assert tool.input_schema["properties"]["pull_request"]["minimum"] == 1
                assert tool.input_schema["properties"]["include_failure_detail"]["default"] is False
            assert tool.input_schema.get("additionalProperties") is False
            forbidden = {"principal", "grant_id", "issuer", "allowed_operations"}
            forbidden.add("role")
            assert not forbidden.intersection(tool.input_schema.get("properties", {}))
        repository_bundle = next(
            tool for tool in tools if tool.name == "repository_bundle_get"
        )
        repository_bundle_description = " ".join(
            (repository_bundle.description or "").split()
        )
        assert (
            "Use this when substantive Switchstand repository content is needed and "
            "no verified local checkout is available."
            in repository_bundle_description
        )
        assert (
            "Do not use when a verified local checkout is already available."
            in repository_bundle_description
        )
        assert (
            "Pass ``required_sha`` only as the exact local checkout target; "
            "it never selects another bundle."
            in repository_bundle_description
        )
        assert (
            "On ``refresh_pending``, retry boundedly rather than reconstructing "
            "the repository through repeated remote file/tree reads."
            in repository_bundle_description
        )
        assert next(
            tool for tool in tools if tool.name == "agent_message_send"
        ).annotations.read_only_hint is True
        required_result = next(tool for tool in tools if tool.name == "required_result_save")
        assert "operation_id" not in required_result.input_schema["properties"]
        history = next(tool for tool in tools if tool.name == "work_history")
        event = next(tool for tool in tools if tool.name == "work_event")
        append = next(tool for tool in tools if tool.name == "work_append")
        assert history.input_schema["properties"]["purpose"]["enum"] == [
            "investigation", "recovery", "legacy_reconciliation",
        ]
        assert event.input_schema["properties"]["purpose"]["enum"] == [
            "investigation", "recovery", "legacy_reconciliation",
        ]
        assert append.input_schema["properties"]["purpose"]["enum"] == [
            "provenance", "investigation", "legacy_reconciliation",
        ]
        assert {"api_version", "work_id", "observed_revision", "purpose"} <= set(
            history.input_schema["required"]
        )
        assert {"api_version", "work_id", "event_id", "observed_revision", "purpose"} <= set(
            event.input_schema["required"]
        )
        assert "purpose" in append.input_schema["required"]
        assert "current notes" in next(
            tool for tool in tools if tool.name == "work_get"
        ).description
        assert "canonical current state" in next(
            tool for tool in tools if tool.name == "work_update"
        ).description
        ordinary_effects = {"work_append", "work_create", "work_update", "required_result_save"}
        for tool in tools:
            if tool.name in ordinary_effects:
                assert "grant_version" not in tool.input_schema["properties"]
        update = next(tool for tool in tools if tool.name == "work_update")
        patch = update.input_schema["$defs"]["ScalarPatch"]
        assert {"priority", "work_type", "review_next_action"} <= patch["properties"].keys() and "gid" not in str(patch).lower()
        create = next(tool for tool in tools if tool.name == "work_create")
        assert "parent_work_id" not in create.input_schema["required"]
        assert "project_id" in create.input_schema["properties"]
        assert "project_gid" not in create.input_schema["properties"]
        assert {"canonical_root", "owner_key", "next_action_class", "next_action_ref"} <= (
            create.input_schema["properties"].keys()
        )
        search = next(tool for tool in tools if tool.name == "work_search")
        assert {"lifecycle_state", "owner_key", "priority", "work_type", "canonical_root"} <= (
            search.input_schema["properties"].keys()
        )
        relate = next(tool for tool in tools if tool.name == "work_relate")
        relation = relate.input_schema["$defs"]["OrdinaryRelationPatch"]
        assert relation["properties"]["kind"]["enum"] == [
            "parent", "dependency", "placement",
        ]
        assert "project_id" in relation["properties"]
        assert "gid" not in str(relation).lower()
        got = (await client.call_tool("work_get", {"api_version": "1"})).structured_content
        assert got["item"]["id"] == str(ACTIVE)
        resolved = await client.call_tool(
            "work_resolve_reference", {"api_version": "1", "reference": "123"}
        )
        assert_public(resolved.model_dump(mode="json"))
        assert resolved.structured_content["item"]["id"] == str(ACTIVE)
        search = (await client.call_tool(
            "work_search", {"api_version": "1", "text": "Task"}
        )).structured_content
        assert search["status"] == "denied" and search["items"] == []
        get_tool = next(tool for tool in tools if tool.name == "work_get")
        assert get_tool.output_schema == OrdinaryWorkResult.model_json_schema()
        assert "include_related" not in get_tool.input_schema["properties"]
        assert "related" not in get_tool.output_schema["properties"]
        assert "grouped" not in get_tool.output_schema["properties"]
        assert "action_summary" not in get_tool.output_schema["properties"]
        bad = await client.call_tool("work_get", {"api_version": "1", "role": "owner"})
        assert bad.is_error
        missing_purpose = await client.call_tool("work_history", {
            "api_version": "1", "work_id": str(ACTIVE), "observed_revision": "r1",
        })
        assert missing_purpose.is_error
        invalid_purpose = await client.call_tool("work_event", {
            "api_version": "1", "work_id": str(ACTIVE), "observed_revision": "r1",
            "event_id": str(uuid4()), "purpose": "routine_polling",
        })
        assert invalid_purpose.is_error
        args = {
            "api_version": "1", "operation_id": str(uuid4()), "work_id": str(ACTIVE),
            "observed_revision": "r1", "text": "protocol feedback",
            "purpose": "provenance",
        }
        reference = await client.call_tool("work_append", args | {"work_id": str(REFERENCE)})
        assert reference.structured_content["status"] == "denied"
        first = (await client.call_tool("work_append", args)).structured_content
        assert first["status"] == "ok" and first["receipt"]["task_gid"] == "123"
        assert (await client.call_tool("work_append", args)).structured_content == first


async def test_workspace_read_chain_and_causal_denials(monkeypatch):
    from unittest.mock import AsyncMock

    from switchstand.core import ProviderSourceStory

    subject = service()
    subject.grants.grant = grant(scope="workspace", operations=frozenset({"work_get", "work_search"}))
    provider = subject.providers["asana"]
    provider.stories = [ProviderSourceStory("raw-event", "123", "comment_added", "history", "now", "Marco")]
    server = build_chatgpt_server(subject)
    found = await server.call_tool("work_search", {"api_version": "1"})
    assert_public(found.model_dump(mode="json"))
    assert found.structured_content["items"][0]["context"]["assignee"] == "Ada"
    await read_chain(
        server, found.structured_content["items"][0]["id"], exceptional_purpose=True
    )
    for selected, target in ((grant(scope="workspace", operations=frozenset({"work_search"})), ACTIVE),
                             (grant(scope="launch"), uuid4())):
        subject.grants.grant = selected
        with monkeypatch.context() as patch:
            for method in ("get", "source_task", "source_stories", "source_story"):
                patch.setattr(provider, method, AsyncMock(side_effect=AssertionError("unauthorized access")))
            for tool, extra in (
                ("work_get", {}),
                ("work_history", {
                    "observed_revision": "r1", "purpose": "investigation",
                }),
                ("work_event", {
                    "observed_revision": "r1", "event_id": str(uuid4()),
                    "purpose": "investigation",
                }),
            ):
                denied = await server.call_tool(tool, {"api_version": "1", "work_id": str(target), **extra})
                assert_public(denied.model_dump(mode="json"))
                assert denied.structured_content["status"] == "denied"


if __name__ == "__main__":
    build_chatgpt_server(service()).run()
