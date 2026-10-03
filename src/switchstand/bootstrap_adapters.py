"""Read-only production adapters for bounded pre-migration bootstrap admission."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast
from uuid import UUID

from .bootstrap_identity import MigrationReceipt, ProviderTaskStatus
from .core import Handle
from .grant_state import GrantState
from .grants import WorkGrant
from .provider import AsanaProvider
from .state import PostgresState


class PostgresIdentityMapping:
    def __init__(self, state: PostgresState):
        self.state = state

    async def get(self, work_id: UUID) -> Handle | None:
        return await self.state.get(work_id)


class ExistingGrantAdapter:
    def __init__(self, grants: GrantState):
        self.grants = grants

    async def for_active_work(self, work_id: UUID) -> tuple[WorkGrant, ...]:
        return await self.grants.for_active_work(work_id)


class ExactAsanaTaskProbe:
    def __init__(self, provider: AsanaProvider):
        self.provider = provider

    async def status(self, provider_work_id: str) -> ProviderTaskStatus:
        task = await self.provider.source_task(provider_work_id)
        return ProviderTaskStatus(
            provider_work_id,
            exists=task is not None,
            current=task is not None and task.canonical and not task.completed,
        )


class AuthenticatedMigrationReceiptFile:
    """Accept a completion receipt only when a trusted exact digest authenticates it."""

    def __init__(self, path: Path | None, expected_sha256: str | None):
        self.path = path
        self.expected_sha256 = expected_sha256

    async def read(self, name: str) -> MigrationReceipt | None:
        if self.path is None or self.expected_sha256 is None:
            return None
        payload = self.path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != self.expected_sha256:
            return None
        value = cast(dict[str, object], json.loads(payload))
        return MigrationReceipt(
            name=str(value.get("name", "")),
            authenticated=True,
            complete=value.get("complete") is True,
        )
