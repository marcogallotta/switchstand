from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from switchstand.bootstrap_identity import (
    MIGRATION_COMPLETE_RECEIPT,
    BootstrapIdentityError,
    MigrationReceipt,
    ProviderTaskStatus,
    admit_existing_launch_grant,
    resolve_and_admit_pre_migration_launch,
    resolve_pre_migration_identity,
)
from switchstand.contracts import LaunchAuthority
from switchstand.core import Handle
from switchstand.grants import PrincipalContext, WorkGrant

WORK_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_WORK_ID = UUID("22222222-2222-4222-8222-222222222222")
GID = "1218999999999999"
PRINCIPAL = PrincipalContext(
    issuer="test", subject="managed-worker", client_id="test", assurance="authenticated",
)
OTHER_PRINCIPAL = PrincipalContext(
    issuer="test", subject="other-worker", client_id="test", assurance="authenticated",
)


class Mapping:
    def __init__(self, handle: Handle | None):
        self.handle = handle
        self.reads = 0

    async def get(self, work_id: UUID) -> Handle | None:
        self.reads += 1
        return self.handle if self.handle is not None and self.handle.id == work_id else None


class Tasks:
    def __init__(self, status: ProviderTaskStatus | None = None):
        self.result = status or ProviderTaskStatus(GID, True, True)
        self.reads = 0

    async def status(self, provider_work_id: str) -> ProviderTaskStatus:
        self.reads += 1
        return self.result


class Receipts:
    def __init__(self, receipt: MigrationReceipt | None = None):
        self.receipt = receipt
        self.names: list[str] = []

    async def read(self, name: str) -> MigrationReceipt | None:
        self.names.append(name)
        return self.receipt


class Grants:
    def __init__(self, *values: WorkGrant):
        self.values = values
        self.reads = 0

    async def for_active_work(self, work_id: UUID) -> tuple[WorkGrant, ...]:
        self.reads += 1
        return self.values


def grant(
    *,
    work_id: UUID = WORK_ID,
    scope: str = "launch",
    expires_at: datetime | None = None,
    principal: PrincipalContext = PRINCIPAL,
) -> WorkGrant:
    return WorkGrant(
        id=uuid4(),
        version=4,
        principal=principal,
        authority=LaunchAuthority(active_work_id=work_id),
        scope=scope,
        operations=frozenset({"work_get"}),
        issuer="test-control",
        provenance="existing test grant",
        expires_at=expires_at or datetime.now(UTC) + timedelta(hours=1),
    )


def boundaries() -> tuple[Mapping, Tasks, Receipts]:
    return Mapping(Handle(WORK_ID, "asana", GID)), Tasks(), Receipts()


async def test_known_exact_work_id_resolves_and_admits_existing_grant():
    mapping, tasks, receipts = boundaries()
    existing = grant()

    admitted = await resolve_and_admit_pre_migration_launch(
        str(WORK_ID), mapping, tasks, receipts, PRINCIPAL, Grants(existing),
    )

    assert admitted.identity.work_id == WORK_ID
    assert admitted.identity.provider_work_id == GID
    assert admitted.identity.source == "legacy_work_handles"
    assert admitted.grant is existing
    assert receipts.names == [MIGRATION_COMPLETE_RECEIPT]


@pytest.mark.parametrize("value", [GID, f"https://app.asana.com/0/0/{GID}", WORK_ID.hex])
async def test_rejects_gid_url_and_noncanonical_uuid_without_boundary_reads(value: str):
    mapping, tasks, receipts = boundaries()

    with pytest.raises(BootstrapIdentityError, match="invalid_work_id"):
        await resolve_pre_migration_identity(value, mapping, tasks, receipts)

    assert mapping.reads == tasks.reads == 0
    assert receipts.names == []


async def test_unknown_work_id_is_not_bound_or_probed():
    mapping, tasks, receipts = Mapping(None), Tasks(), Receipts()

    with pytest.raises(BootstrapIdentityError, match="unknown_work_id"):
        await resolve_pre_migration_identity(str(WORK_ID), mapping, tasks, receipts)

    assert mapping.reads == 1
    assert tasks.reads == 0


@pytest.mark.parametrize(
    "status",
    [
        ProviderTaskStatus(GID, False, False),
        ProviderTaskStatus(GID, True, False),
        ProviderTaskStatus("999", True, True),
    ],
)
async def test_mapped_task_must_exist_be_current_and_match_exact_gid(status: ProviderTaskStatus):
    mapping, _, receipts = boundaries()

    with pytest.raises(BootstrapIdentityError, match="task_not_current"):
        await resolve_pre_migration_identity(str(WORK_ID), mapping, Tasks(status), receipts)


async def test_authenticated_migration_complete_receipt_disables_fallback_before_mapping_read():
    mapping, tasks, _ = boundaries()
    receipts = Receipts(MigrationReceipt(MIGRATION_COMPLETE_RECEIPT, True, True))

    with pytest.raises(BootstrapIdentityError, match="migration_complete"):
        await resolve_pre_migration_identity(str(WORK_ID), mapping, tasks, receipts)

    assert mapping.reads == tasks.reads == 0


@pytest.mark.parametrize(
    "receipt",
    [
        MigrationReceipt(MIGRATION_COMPLETE_RECEIPT, False, True),
        MigrationReceipt(MIGRATION_COMPLETE_RECEIPT, True, False),
        MigrationReceipt("some-other-receipt", True, True),
    ],
)
async def test_only_named_authenticated_complete_receipt_disables_fallback(
    receipt: MigrationReceipt,
):
    mapping, tasks, _ = boundaries()

    with pytest.raises(BootstrapIdentityError, match="migration_receipt_invalid"):
        await resolve_pre_migration_identity(str(WORK_ID), mapping, tasks, Receipts(receipt))


async def test_missing_grant_is_rejected():
    mapping, tasks, receipts = boundaries()
    identity = await resolve_pre_migration_identity(str(WORK_ID), mapping, tasks, receipts)

    with pytest.raises(BootstrapIdentityError, match="missing_grant"):
        await admit_existing_launch_grant(identity, PRINCIPAL, Grants())


async def test_stale_grant_is_rejected():
    mapping, tasks, receipts = boundaries()
    identity = await resolve_pre_migration_identity(str(WORK_ID), mapping, tasks, receipts)
    stale = grant(expires_at=datetime.now(UTC) - timedelta(seconds=1))

    with pytest.raises(BootstrapIdentityError, match="stale_grant"):
        await admit_existing_launch_grant(identity, PRINCIPAL, Grants(stale))


async def test_wrong_scope_grant_is_rejected():
    mapping, tasks, receipts = boundaries()
    identity = await resolve_pre_migration_identity(str(WORK_ID), mapping, tasks, receipts)

    with pytest.raises(BootstrapIdentityError, match="wrong_scope"):
        await admit_existing_launch_grant(
            identity, PRINCIPAL, Grants(grant(scope="workspace")),
        )


async def test_ambiguous_current_grants_are_rejected():
    mapping, tasks, receipts = boundaries()
    identity = await resolve_pre_migration_identity(str(WORK_ID), mapping, tasks, receipts)

    with pytest.raises(BootstrapIdentityError, match="ambiguous_grant"):
        await admit_existing_launch_grant(identity, PRINCIPAL, Grants(grant(), grant()))


async def test_grant_for_different_work_does_not_authorize_launch():
    mapping, tasks, receipts = boundaries()
    identity = await resolve_pre_migration_identity(str(WORK_ID), mapping, tasks, receipts)

    with pytest.raises(BootstrapIdentityError, match="missing_grant"):
        await admit_existing_launch_grant(
            identity, PRINCIPAL, Grants(grant(work_id=OTHER_WORK_ID)),
        )


async def test_grant_for_different_principal_does_not_authorize_launch():
    mapping, tasks, receipts = boundaries()
    identity = await resolve_pre_migration_identity(str(WORK_ID), mapping, tasks, receipts)

    with pytest.raises(BootstrapIdentityError, match="principal_mismatch"):
        await admit_existing_launch_grant(
            identity, PRINCIPAL, Grants(grant(principal=OTHER_PRINCIPAL)),
        )


async def test_other_principals_grant_does_not_make_exact_principal_ambiguous():
    mapping, tasks, receipts = boundaries()
    identity = await resolve_pre_migration_identity(str(WORK_ID), mapping, tasks, receipts)
    expected = grant()

    admitted = await admit_existing_launch_grant(
        identity, PRINCIPAL, Grants(grant(principal=OTHER_PRINCIPAL), expected),
    )

    assert admitted.grant is expected


async def test_requesting_principal_must_itself_be_authenticated():
    mapping, tasks, receipts = boundaries()
    identity = await resolve_pre_migration_identity(str(WORK_ID), mapping, tasks, receipts)
    unauthenticated = PrincipalContext(
        issuer="test", subject="managed-worker", client_id="test", assurance="test",
    )
    grants = Grants(grant())

    with pytest.raises(BootstrapIdentityError, match="unauthenticated_principal"):
        await admit_existing_launch_grant(identity, unauthenticated, grants)

    assert grants.reads == 0


async def test_boundaries_are_read_only_and_existing_grant_identity_is_preserved():
    mapping, tasks, receipts = boundaries()
    grants = Grants(grant())

    admitted = await resolve_and_admit_pre_migration_launch(
        str(WORK_ID), mapping, tasks, receipts, PRINCIPAL, grants,
    )

    assert admitted.grant is grants.values[0]
    assert admitted.grant.version == 4
    assert mapping.reads == tasks.reads == grants.reads == 1
