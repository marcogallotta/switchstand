from typing import Any
from uuid import uuid4

import pytest
from chatgpt_fixture import ACTIVE, PRINCIPAL, grant, service
from mcp.server.mcpserver.exceptions import ToolError

from switchstand.activation_continuity import (
    ActivationContract,
    TechnicalBasis,
    open_obligation,
)
from switchstand.chatgpt_mcp import build_chatgpt_server
from switchstand.product_currentness import ProductCurrentness


def contract() -> ActivationContract:
    return ActivationContract(
        product_work_id=ACTIVE, outcome_key="release", target_revision="git:abc",
        target_phase="ACTIVATED", return_owner_work_id=ACTIVE,
        acceptance_contract_id="acceptance", contract_revision="v1",
        adoption_requirement="NOT_REQUIRED", lifecycle_authority_work_id=ACTIVE,
    )


def technical(bound: ActivationContract) -> TechnicalBasis:
    current = ProductCurrentness(
        status="ok", product_work_id=ACTIVE, current="TRUE", contract_revision="v1",
        reconciliation_id=uuid4(), basis_id="basis", conditions=(), blockers=(),
    )
    return TechnicalBasis(
        target_revision=bound.target_revision, target_phase=bound.target_phase,
        currentness="CURRENT", result=current,
    )


class Continuity:
    def __init__(self, bound: ActivationContract):
        self.bound, self.seen = bound, None

    async def transition(self, principal, selected, intent, basis):
        self.seen = principal, selected, intent, basis
        return open_obligation(self.bound, intent, "private-actor-ref", basis)


async def test_tool_is_absent_without_server_owned_continuity() -> None:
    subject = service()
    names = {tool.name for tool in await build_chatgpt_server(subject).list_tools()}
    assert "activation_obligation_transition" not in names


async def test_authenticated_tool_derives_actor_and_sanitizes_internal_evidence() -> None:
    subject, bound = service(), contract()
    continuity = Continuity(bound)
    subject.activation_continuity = continuity

    async def resolve_technical(_obligation_id):
        return technical(bound)

    subject.activation_technical = resolve_technical
    subject.admission_grants.grant = grant(operations=frozenset({"activation_continuity"}))
    server, operation_id = build_chatgpt_server(subject), uuid4()
    result = await server.call_tool("activation_obligation_transition", {
        "api_version": "1", "operation_id": str(operation_id),
        "obligation_id": str(bound.obligation_id), "observed_revision": "MISSING",
        "transition": "ACTIVATED",
    })
    payload: dict[str, Any] = result.structured_content
    assert (payload["status"], payload["state"]) == ("APPLIED", "VERIFY_NOW")
    assert continuity.seen[0] == PRINCIPAL and continuity.seen[2].operation_id == operation_id
    assert "private-actor-ref" not in str(payload) and "technical_basis_ref" not in str(payload)


async def test_missing_grant_denies_and_internal_fields_are_closed() -> None:
    subject, bound = service(), contract()
    continuity = Continuity(bound)
    subject.activation_continuity = continuity
    subject.admission_grants.grant = grant(operations=frozenset({"work_get"}))
    server = build_chatgpt_server(subject)
    arguments = {
        "api_version": "1", "operation_id": str(uuid4()),
        "obligation_id": str(bound.obligation_id), "observed_revision": "MISSING",
        "transition": "ACTIVATED",
    }
    denied = await server.call_tool("activation_obligation_transition", arguments)
    assert (denied.structured_content["status"], continuity.seen) == ("DENIED", None)
    with pytest.raises(ToolError, match="Extra inputs are not permitted"):
        await server.call_tool("activation_obligation_transition", {
            **arguments, "actor_work_id": str(ACTIVE),
        })
