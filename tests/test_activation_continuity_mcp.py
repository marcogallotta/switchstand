from uuid import uuid4

from chatgpt_fixture import ACTIVE, PRINCIPAL, grant, service

from switchstand.activation_continuity import (
    ActivationContract,
    RuntimeBinding,
    TechnicalBasis,
    open_obligation,
)
from switchstand.chatgpt_mcp import build_chatgpt_server
from switchstand.mcp import build_context_server
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
        self.bound, self.seen, self.current = bound, None, None
        self.contracts = {bound.obligation_id: bound}

    async def transition(self, principal, selected, runtime, intent, basis, proof):
        self.seen = principal, selected, runtime, intent, basis, proof
        result = open_obligation(
            self.bound, intent, "private-actor-ref", runtime.binding_token, basis
        )
        self.current = result.obligation
        return result

    async def for_owner(self, owner_work_id):
        return () if self.current is None or owner_work_id != ACTIVE else (self.current,)


async def test_ordinary_surface_exposes_preflight_but_never_mutation() -> None:
    subject, bound = service(), contract()
    subject.activation_continuity = Continuity(bound)
    server = build_chatgpt_server(subject)
    assert "activation_obligation_transition" not in {
        tool.name for tool in await server.list_tools()
    }
    result = await server.call_tool("capability_preflight_get", {"api_version": "1"})
    assert result.structured_content["reasons"] == ["WORK_BOUND_ACTOR_REQUIRED"]


async def test_managed_opt_in_is_ready_and_grant_rotation_fails_closed() -> None:
    bound, selected = contract(), grant()
    continuity = Continuity(bound)
    grants = service().admission_grants
    grants.grant = selected

    async def resolve_technical(_principal, _obligation_id):
        return technical(bound)

    async def resolve_proof(*_args):
        return None

    plain = build_context_server(object(), ACTIVE, grants=grants, principal=PRINCIPAL)
    assert "activation_obligation_transition" not in {
        tool.name for tool in await plain.list_tools()
    }
    unavailable = await plain.call_tool("capability_preflight_get", {"api_version": "1"})
    reasons = unavailable.structured_content["reasons"]
    assert reasons == ["TOOL_NOT_EXPOSED", "OPERATION_NOT_GRANTED"]
    selected = grant(operations=frozenset({"activation_continuity"}))
    grants.grant = selected
    activation = (continuity, resolve_technical, resolve_proof, (selected.id, selected.version))
    server = build_context_server(object(), ACTIVE, grants=grants, principal=PRINCIPAL,
                                  activation=activation)
    names = {tool.name for tool in await server.list_tools()}
    assert "activation_obligation_transition" in names
    ready = await server.call_tool("capability_preflight_get", {"api_version": "1"})
    assert ready.structured_content["status"] == "READY"
    applied = await server.call_tool("activation_obligation_transition", {
        "api_version": "1", "operation_id": str(uuid4()),
        "obligation_id": str(bound.obligation_id), "observed_revision": "MISSING",
        "transition": "ACTIVATED",
    })
    assert applied.structured_content["status"] == "APPLIED"
    stale = build_context_server(object(), ACTIVE, grants=grants, principal=PRINCIPAL,
                                 activation=(*activation[:3], (uuid4(), selected.version)))
    result = await stale.call_tool("capability_preflight_get", {"api_version": "1"})
    assert result.structured_content["reasons"] == ["ACTOR_GRANT_STALE", "RUNTIME_STALE"]
    selected = selected.model_copy(update={"state": "revoked"})
    grants.grant = selected
    result = await build_context_server(object(), ACTIVE, grants=grants, principal=PRINCIPAL,
        activation=(*activation[:3], (selected.id, selected.version))).call_tool(
            "capability_preflight_get", {"api_version": "1"})
    assert result.structured_content["reasons"] == ["ACTOR_GRANT_STALE", "RUNTIME_STALE"]


async def test_managed_preflight_closes_missing_stale_mismatched_and_failed_inputs() -> None:
    bound, selected = contract(), grant(operations=frozenset({"activation_continuity"}))
    continuity = Continuity(bound)
    grants = service().admission_grants
    grants.grant = selected
    async def project(contracts, basis, proof_route=True):
        async def resolve(_principal, _obligation_id):
            if isinstance(basis, Exception):
                raise basis
            return basis
        expected_grant = selected.id, selected.version
        server = build_context_server(object(), ACTIVE, grants=grants, principal=PRINCIPAL,
            activation=(contracts, resolve, resolve if proof_route else None, expected_grant))
        result = await server.call_tool("capability_preflight_get", {"api_version": "1"})
        return result.structured_content
    empty = Continuity(bound)
    empty.contracts = {}
    assert "CONTRACT_NOT_INSTALLED" in (await project(empty, None))["reasons"]
    assert "TECHNICAL_PROOF_MISSING" in (await project(continuity, None))["reasons"]
    stale = technical(bound).model_copy(update={"currentness": "STALE"})
    assert "TECHNICAL_PROOF_STALE" in (await project(continuity, stale))["reasons"]
    result = technical(bound).result.model_copy(update={"product_work_id": uuid4()})
    mismatched = technical(bound).model_copy(update={"result": result})
    assert "TECHNICAL_PROOF_MISSING" in (await project(continuity, mismatched))["reasons"]
    assert "PROOF_ROUTE_MISSING" in (await project(continuity, technical(bound), False))["reasons"]
    failed = await project(continuity, RuntimeError("currentness unavailable"))
    states = failed["status"], failed["actor_binding"], failed["technical_proof"]
    assert states == ("UNKNOWN", "UNKNOWN", "UNKNOWN")


async def test_unresolved_runtime_and_proof_fail_closed_before_transition() -> None:
    subject, bound = service(), contract()
    continuity = Continuity(bound)
    subject.activation_continuity = continuity
    subject.admission_grants.grant = grant(
        operations=frozenset({"activation_continuity"})
    )
    missing_runtime = await subject.activation_continuity_transition(
        uuid4(), bound.obligation_id, "MISSING", "ACTIVATED", (), None, None
    )
    assert (missing_runtime.status, continuity.seen) == ("UNKNOWN", None)

    async def resolve_runtime(_principal, selected):
        return RuntimeBinding(
            actor_work_id=selected.authority.active_work_id,
            binding_token="runtime/current",
            currentness="CURRENT",
        )

    subject.activation_runtime = resolve_runtime
    missing_proof = await subject.activation_continuity_transition(
        uuid4(), bound.obligation_id, "revision", "ACCEPTANCE_PASS",
        ("opaque-ref",), None, None,
    )
    assert (missing_proof.status, continuity.seen) == ("UNKNOWN", None)
