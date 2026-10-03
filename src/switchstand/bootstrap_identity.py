"""Read-only compatibility admission for pre-migration managed Workers.

This module deliberately separates identity recovery from authority admission.  The
legacy mapping and provider can establish which work item a WorkId names; only an
already-issued, current launch grant can authorize a managed launch.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from .core import Handle
from .grants import WorkGrant

MIGRATION_COMPLETE_RECEIPT = "work-identity-migration-complete-v1"


class BootstrapIdentityError(ValueError):
    """Closed failure at the compatibility identity/admission boundary."""

    def __init__(self, reason: Literal[
        "invalid_work_id",
        "migration_complete",
        "unknown_work_id",
        "unsupported_mapping",
        "task_not_current",
        "missing_grant",
        "stale_grant",
        "ambiguous_grant",
        "wrong_scope",
    ]):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class MigrationReceipt:
    name: str
    authenticated: bool
    complete: bool


@dataclass(frozen=True)
class ProviderTaskStatus:
    provider_work_id: str
    exists: bool
    current: bool


@dataclass(frozen=True)
class ResolvedBootstrapIdentity:
    work_id: UUID
    provider: Literal["asana"]
    provider_work_id: str
    source: Literal["legacy_work_handles"] = "legacy_work_handles"


@dataclass(frozen=True)
class AdmittedBootstrap:
    identity: ResolvedBootstrapIdentity
    grant: WorkGrant


class WorkIdentityMapping(Protocol):
    async def get(self, work_id: UUID) -> Handle | None: ...


class ExactTaskProbe(Protocol):
    async def status(self, provider_work_id: str) -> ProviderTaskStatus: ...


class MigrationReceiptReader(Protocol):
    async def read(self, name: str) -> MigrationReceipt | None: ...


class ExistingGrantReader(Protocol):
    async def for_active_work(self, work_id: UUID) -> Sequence[WorkGrant]: ...


def exact_work_id(value: str) -> UUID:
    """Parse only the canonical textual UUID form, never a GID or provider URL."""
    try:
        work_id = UUID(value)
    except (ValueError, AttributeError):
        raise BootstrapIdentityError("invalid_work_id") from None
    if str(work_id) != value:
        raise BootstrapIdentityError("invalid_work_id")
    return work_id


async def resolve_pre_migration_identity(
    value: str,
    mapping: WorkIdentityMapping,
    tasks: ExactTaskProbe,
    receipts: MigrationReceiptReader,
) -> ResolvedBootstrapIdentity:
    """Resolve an exact existing identity without creating or changing any binding."""
    work_id = exact_work_id(value)
    receipt = await receipts.read(MIGRATION_COMPLETE_RECEIPT)
    if (
        receipt is not None
        and receipt.name == MIGRATION_COMPLETE_RECEIPT
        and receipt.authenticated
        and receipt.complete
    ):
        raise BootstrapIdentityError("migration_complete")

    handle = await mapping.get(work_id)
    if handle is None:
        raise BootstrapIdentityError("unknown_work_id")
    if (
        handle.id != work_id
        or handle.provider != "asana"
        or not handle.provider_work_id.isdecimal()
    ):
        raise BootstrapIdentityError("unsupported_mapping")

    status = await tasks.status(handle.provider_work_id)
    if (
        status.provider_work_id != handle.provider_work_id
        or not status.exists
        or not status.current
    ):
        raise BootstrapIdentityError("task_not_current")
    return ResolvedBootstrapIdentity(work_id, "asana", handle.provider_work_id)


async def admit_existing_launch_grant(
    identity: ResolvedBootstrapIdentity,
    grants: ExistingGrantReader,
) -> AdmittedBootstrap:
    """Admit exactly one already-current launch grant; never issue or rotate one."""
    candidates = tuple(await grants.for_active_work(identity.work_id))
    matching = tuple(
        grant for grant in candidates
        if grant.authority.active_work_id == identity.work_id
    )
    if not matching:
        raise BootstrapIdentityError("missing_grant")
    current = tuple(grant for grant in matching if grant.current())
    if not current:
        raise BootstrapIdentityError("stale_grant")
    if len(current) != 1:
        raise BootstrapIdentityError("ambiguous_grant")
    grant = current[0]
    if grant.scope != "launch":
        raise BootstrapIdentityError("wrong_scope")
    return AdmittedBootstrap(identity, grant)


async def resolve_and_admit_pre_migration_launch(
    value: str,
    mapping: WorkIdentityMapping,
    tasks: ExactTaskProbe,
    receipts: MigrationReceiptReader,
    grants: ExistingGrantReader,
) -> AdmittedBootstrap:
    """Small integration seam for a launcher compatibility fallback."""
    identity = await resolve_pre_migration_identity(value, mapping, tasks, receipts)
    return await admit_existing_launch_grant(identity, grants)
