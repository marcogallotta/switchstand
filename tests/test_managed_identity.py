from uuid import uuid4

from switchstand.contracts import LaunchAuthority
from switchstand.managed_identity import rotate_managed_grant


async def test_priority_claim_authority_is_explicit_launch_opt_in():
    issued = []

    class Grants:
        async def current(self, _principal_key):
            return None

        async def issue(self, grant, expected_version):
            issued.append((grant, expected_version))

    authority = LaunchAuthority(active_work_id=uuid4())
    ordinary = await rotate_managed_grant(Grants(), authority)  # type: ignore[arg-type]
    enabled = await rotate_managed_grant(
        Grants(), authority, priority_claims=True,  # type: ignore[arg-type]
    )
    activation = await rotate_managed_grant(
        Grants(), authority, activation_continuity=True,  # type: ignore[arg-type]
    )

    assert "priority_claim" not in ordinary.operations
    assert ordinary.priority_claim_qualification is None
    assert "priority_claim" in enabled.operations
    assert enabled.priority_claim_qualification == "managed:task-bound"
    assert "activation_continuity" in activation.operations
    assert [expected for _, expected in issued] == [None, None, None]
