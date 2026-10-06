import os
from typing import cast

from .grant_state import GrantState
from .managed_identity import managed_principal
from .mcp import build_context_server, controller_from_env, protect_provider_logs
from .priority_claim_service import PriorityClaimService
from .priority_claims import PriorityClaimRepository
from .priority_context import PriorityContextProjection
from .provision import require_current_schema
from .state import PostgresState


def main() -> None:
    protect_provider_logs()
    require_current_schema()
    service = controller_from_env()
    active = service.authority.active_work_id
    priority_claims = None
    priority_context = None
    grants = None
    principal = None
    if os.getenv("SWITCHSTAND_PRIORITY_CLAIMS") == "1":
        engine = cast(PostgresState, service.state).engine
        grants = GrantState(engine)
        principal = managed_principal(active)
        priority_claims = PriorityClaimService(
            PriorityClaimRepository(engine), service.work.works,
        )
        priority_context = PriorityContextProjection(
            works=service.work.works,
            relations=service.work.relations,
            claims=priority_claims,
        )
    build_context_server(
        service, active, service.authority.reference_work_ids,
        priority_claims=priority_claims,
        priority_context=priority_context,
        grants=grants,
        principal=principal,
    ).run()


if __name__ == "__main__":
    main()
