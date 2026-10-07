import json
import os
from typing import cast
from uuid import UUID

from . import chatgpt_edge
from .activation_continuity import TechnicalBasis
from .activation_continuity_store import ActivationContinuity
from .activation_contract_loader import load_activation_contracts_from_environment
from .grant_state import GrantState
from .managed_identity import managed_principal
from .mcp import build_context_server, controller_from_env, protect_provider_logs
from .priority_claim_service import PriorityClaimService
from .priority_claims import PriorityClaimRepository
from .priority_context import PriorityContextProjection
from .product_currentness import STATEFUL_PRODUCT_WORK_ID, evaluate_stateful_currentness
from .product_currentness_stateful import LiveStatefulEvidenceReader, StatefulServerSnapshot
from .provision import require_current_schema
from .state import PostgresState
from .task_runs import TaskRunState


def main() -> None:
    protect_provider_logs()
    require_current_schema()
    service = controller_from_env()
    active = service.authority.active_work_id
    engine = cast(PostgresState, service.state).engine
    priority_claims = None
    priority_context = None
    grants = GrantState(engine)
    principal = managed_principal(active)
    if os.getenv("SWITCHSTAND_PRIORITY_CLAIMS") == "1":
        priority_claims = PriorityClaimService(
            PriorityClaimRepository(engine), service.work.works,
        )
        priority_context = PriorityContextProjection(
            works=service.work.works,
            relations=service.work.relations,
            claims=priority_claims,
        )
    activation = None
    if os.getenv("SWITCHSTAND_ACTIVATION_CONTINUITY") == "1":
        contracts = load_activation_contracts_from_environment()
        config = chatgpt_edge._ProductCurrentnessConfig.from_environment()  # pyright: ignore
        names = cast(list[object], json.loads(
            os.environ["SWITCHSTAND_PRODUCT_CURRENTNESS_TOOL_NAMES"]))
        valid = isinstance(names, list) and 0 < len(names) <= 128  # pyright: ignore
        valid = valid and len(set(names)) == len(names)  # pyright: ignore
        valid = valid and all(isinstance(name, str) and 0 < len(name) <= 120 for name in names)
        if contracts is None or config is None or not valid:
            raise ValueError("activation continuity configuration is invalid")
        async def read_principal():
            return principal
        async def read_snapshot():
            return StatefulServerSnapshot(
                runtime_sha=config.runtime_sha, selected_runtime_sha=config.selected_runtime_sha,
                run_id=config.run_id, principal_key=principal.key,
                outcome_actions_enabled=True, tool_names=tuple(cast(list[str], names)),
                tools_schema_sha256=config.expected_tools_schema_sha256,
            )
        reader = LiveStatefulEvidenceReader(
            engine, read_principal, read_snapshot,
            expected_migration_revision=chatgpt_edge.STATEFUL_MIGRATION_REVISION,
            expected_tools_schema_sha256=config.expected_tools_schema_sha256,
            qualification_receipt=config.qualification_receipt,
            qualification_key=config.qualification_key,
        )
        async def technical(_principal: object, obligation_id: UUID) -> TechnicalBasis | None:
            contract = contracts.get(obligation_id)
            if contract is None:
                return None
            result = await evaluate_stateful_currentness(STATEFUL_PRODUCT_WORK_ID, reader)
            return TechnicalBasis(
                target_revision=contract.target_revision, target_phase=contract.target_phase,
                currentness="CURRENT", result=result,
            )
        runs = TaskRunState(engine, service.work.works)
        proof = chatgpt_edge.managed_activation_proof(runs, contracts)
        env = os.environ
        grant = UUID(env["SWITCHSTAND_GRANT_ID"]), int(env["SWITCHSTAND_GRANT_VERSION"])
        activation = ActivationContinuity(engine, contracts), technical, proof, grant
    build_context_server(
        service, active, service.authority.reference_work_ids,
        priority_claims=priority_claims,
        priority_context=priority_context,
        grants=grants,
        principal=principal,
        activation=activation,
    ).run()


if __name__ == "__main__":
    main()
