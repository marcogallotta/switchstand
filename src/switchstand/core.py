from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from .contracts import (
    AppendResult,
    LaunchAuthority,
    Routing,
    WorkAppendRequest,
    WorkGetRequest,
    WorkItem,
    WorkPatch,
    WorkResult,
    WorkUpdateRequest,
)


class ProviderError(Exception):
    """A provider failure whose details must not cross the controller boundary."""

class UnknownEffect(ProviderError):
    """A single external effect was sent but its outcome is unknown."""

@dataclass(frozen=True)
class Handle:
    id: UUID
    provider: str
    provider_work_id: str

@dataclass(frozen=True)
class ProviderWork:
    title: str
    notes: str
    completed: bool
    revision: str
    routing: Routing
    canonical: bool

class State(Protocol):
    async def get(self, work_id: UUID) -> Handle | None: ...
    def locked(self, work_id: UUID) -> AbstractAsyncContextManager[Handle | None]: ...
    async def bind(self, provider: str, provider_work_id: str) -> Handle: ...

class Provider(Protocol):
    async def get(self, provider_work_id: str) -> ProviderWork | None: ...
    async def update(self, provider_work_id: str, patch: WorkPatch) -> None: ...
    async def append(self, provider_work_id: str, text: str) -> None: ...

class Controller:
    def __init__(self, authority: LaunchAuthority, state: State, providers: dict[str, Provider]):
        self.authority, self.state, self.providers = authority, state, providers

    def _item(self, work_id: UUID, work: ProviderWork) -> WorkItem:
        return WorkItem(id=work_id, title=work.title, notes=work.notes, completed=work.completed, revision=work.revision, routing=work.routing)

    async def _read(self, work_id: UUID, handle: Handle) -> WorkResult:
        provider = self.providers.get(handle.provider)
        if provider is None:
            return WorkResult(status="provider_error")
        work = await provider.get(handle.provider_work_id)
        if work is None:
            return WorkResult(status="unknown")
        if not work.canonical:
            return WorkResult(status="denied")
        return WorkResult(status="ok", item=self._item(work_id, work))

    async def bind_work(self, provider_name: str, provider_work_id: str) -> Handle:
        provider = self.providers[provider_name]
        work = await provider.get(provider_work_id)
        if work is None or not work.canonical:
            raise PermissionError("work is not canonical")
        return await self.state.bind(provider_name, provider_work_id)

    async def get(self, request: WorkGetRequest) -> WorkResult:
        if not self.authority.can_read(request.work_id):
            return WorkResult(status="denied")
        try:
            handle = await self.state.get(request.work_id)
            return WorkResult(status="unknown") if handle is None else await self._read(request.work_id, handle)
        except UnknownEffect:
            return WorkResult(status="unknown")
        except ProviderError:
            return WorkResult(status="provider_error")

    async def update(self, request: WorkUpdateRequest) -> WorkResult:
        if request.work_id != self.authority.active_work_id:
            return WorkResult(status="denied")
        try:
            async with self.state.locked(request.work_id) as handle:
                if handle is None:
                    return WorkResult(status="unknown")
                current = await self._read(request.work_id, handle)
                if current.status != "ok" or current.item is None:
                    return current
                if current.item.revision != request.observed_revision:
                    return WorkResult(status="stale", item=current.item)
                await self.providers[handle.provider].update(handle.provider_work_id, request.patch)
                return await self._read(request.work_id, handle)
        except UnknownEffect:
            return WorkResult(status="unknown")
        except ProviderError:
            return WorkResult(status="provider_error")

    async def append(self, request: WorkAppendRequest) -> AppendResult:
        if request.work_id != self.authority.active_work_id:
            return AppendResult(status="denied")
        try:
            async with self.state.locked(request.work_id) as handle:
                if handle is None:
                    return AppendResult(status="unknown")
                current = await self._read(request.work_id, handle)
                if current.status != "ok":
                    if current.status == "stale":
                        return AppendResult(status="provider_error")
                    return AppendResult(status=current.status)
                await self.providers[handle.provider].append(handle.provider_work_id, request.text)
                readback = await self._read(request.work_id, handle)
                if readback.status == "ok":
                    return AppendResult(status="ok")
                if readback.status == "denied":
                    return AppendResult(status="denied")
                if readback.status == "unknown":
                    return AppendResult(status="unknown")
                return AppendResult(status="provider_error")
        except UnknownEffect:
            return AppendResult(status="unknown")
        except ProviderError:
            return AppendResult(status="provider_error")
