from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from pydantic import ValidationError

from switchstand.contracts import (
    LaunchAuthority,
    Routing,
    WorkAppendRequest,
    WorkGetRequest,
    WorkPatch,
    WorkUpdateRequest,
)
from switchstand.core import Controller, Handle, ProviderWork


class FakeState:
    def __init__(self, handles): self.handles = handles
    async def get(self, work_id): return self.handles.get(work_id)
    @asynccontextmanager
    async def locked(self, work_id): yield self.handles.get(work_id)
    async def bind(self, provider, provider_work_id):
        handle = Handle(uuid4(), provider, provider_work_id); self.handles[handle.id] = handle; return handle

class FakeProvider:
    def __init__(self, work): self.work, self.updates, self.appends = work, [], []
    async def get(self, provider_work_id): return self.work
    async def update(self, provider_work_id, patch):
        self.updates.append(patch)
        self.work = ProviderWork(self.work.title, patch.notes or self.work.notes, patch.completed if patch.completed is not None else self.work.completed, "r2", self.work.routing, self.work.canonical)
    async def append(self, provider_work_id, text): self.appends.append(text)

@pytest.fixture
def setup_controller():
    active, reference = uuid4(), uuid4()
    state = FakeState({active: Handle(active, "fake", "a"), reference: Handle(reference, "fake", "r")})
    provider = FakeProvider(ProviderWork("Title", "Notes", False, "r1", Routing(priority="P0"), True))
    return active, reference, provider, Controller(LaunchAuthority(active_work_id=active, reference_work_ids=(reference,)), state, {"fake": provider})

def test_contracts_are_closed_and_patch_is_coherent():
    with pytest.raises(ValidationError):
        WorkGetRequest.model_validate({"api_version": "1", "work_id": uuid4(), "extra": True})
    with pytest.raises(ValidationError): WorkPatch()
    with pytest.raises(ValidationError): WorkPatch(notes=None)
    with pytest.raises(ValidationError): WorkPatch(horizon="Stage 3")

async def test_reads_bound_handles_and_denies_unbound(setup_controller):
    active, reference, _, controller = setup_controller
    assert (await controller.get(WorkGetRequest(api_version="1", work_id=active))).status == "ok"
    assert (await controller.get(WorkGetRequest(api_version="1", work_id=reference))).status == "ok"
    assert (await controller.get(WorkGetRequest(api_version="1", work_id=uuid4()))).status == "denied"

async def test_reference_write_denied_and_stale_update_has_no_effect(setup_controller):
    active, reference, provider, controller = setup_controller
    denied = await controller.update(WorkUpdateRequest(api_version="1", work_id=reference, observed_revision="r1", patch=WorkPatch(notes="x")))
    stale = await controller.update(WorkUpdateRequest(api_version="1", work_id=active, observed_revision="old", patch=WorkPatch(notes="x")))
    assert denied.status == "denied" and stale.status == "stale" and provider.updates == []

async def test_update_returns_authoritative_readback(setup_controller):
    active, _, provider, controller = setup_controller
    result = await controller.update(WorkUpdateRequest(api_version="1", work_id=active, observed_revision="r1", patch=WorkPatch(notes="new")))
    assert result.status == "ok" and result.item and result.item.notes == "new" and result.item.revision == "r2"
    assert len(provider.updates) == 1

async def test_noncanonical_work_denies_and_append_is_once(setup_controller):
    active, _, provider, controller = setup_controller
    provider.work = ProviderWork("T", "N", False, "r1", Routing(), False)
    assert (await controller.get(WorkGetRequest(api_version="1", work_id=active))).status == "denied"
    provider.work = ProviderWork("T", "N", False, "r1", Routing(), True)
    result = await controller.append(WorkAppendRequest(api_version="1", work_id=active, text="history"))
    assert result.status == "ok" and provider.appends == ["history"]
