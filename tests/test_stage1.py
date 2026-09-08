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
from switchstand.core import (
    Controller,
    Handle,
    ProviderError,
    ProviderWork,
    UnknownEffect,
    provision_launch,
)


class FakeState:
    def __init__(self, handles): self.handles, self.fail_unlock = handles, False
    async def get(self, work_id): return self.handles.get(work_id)
    @asynccontextmanager
    async def locked(self, work_id):
        yield self.handles.get(work_id)
        if self.fail_unlock: raise RuntimeError("database commit failed")
    async def bind(self, provider, provider_work_id):
        existing = next((handle for handle in self.handles.values()
                         if (handle.provider, handle.provider_work_id) == (provider, provider_work_id)), None)
        if existing is not None: return existing
        handle = Handle(uuid4(), provider, provider_work_id); self.handles[handle.id] = handle; return handle

class FakeProvider:
    def __init__(self, work):
        self.work, self.updates, self.appends = work, [], []
        self.ignore_update = self.unknown_append = self.fail_get = False
        self.fail_after_append = self.deny_after_append = False
        self.reject_append = False
        self.confirm_append = True
    async def get(self, provider_work_id):
        if self.fail_get:
            raise ProviderError("secret provider detail")
        return self.work
    async def update(self, provider_work_id, patch):
        self.updates.append(patch)
        if self.ignore_update:
            return
        fields = patch.model_fields_set
        routing = self.work.routing.model_dump()
        for field in {"horizon", "review_next_action", "stage3_gate"} & fields:
            routing[field] = getattr(patch, field)
        self.work = ProviderWork(
            self.work.title,
            patch.notes if "notes" in fields else self.work.notes,
            patch.completed if "completed" in fields else self.work.completed,
            "r2", Routing(**routing), self.work.canonical,
        )
    async def append(self, provider_work_id, text):
        self.appends.append(text)
        if self.reject_append:
            raise ProviderError("definite rejection")
        if self.unknown_append:
            raise UnknownEffect("lost response")
        if self.fail_after_append:
            self.fail_get = True
        if self.deny_after_append:
            self.work = ProviderWork(
                self.work.title, self.work.notes, self.work.completed,
                self.work.revision, self.work.routing, False,
            )
        return self.confirm_append

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
    patch = WorkPatch(notes="new", horizon="Stage 3")
    result = await controller.update(WorkUpdateRequest(api_version="1", work_id=active, observed_revision="r1", patch=patch))
    assert result.status == "ok" and result.item and result.item.routing.horizon == "Stage 3"
    assert len(provider.updates) == 1

async def test_write_denial_and_readback_mismatch_are_not_success(setup_controller):
    active, _, provider, controller = setup_controller
    provider.work = ProviderWork("T", "N", False, "r1", Routing(), False)
    request = WorkUpdateRequest(api_version="1", work_id=active, observed_revision="r1", patch=WorkPatch(notes="new"))
    assert (await controller.update(request)).status == "denied" and provider.updates == []
    provider.work = ProviderWork("T", "N", False, "r1", Routing(), True)
    provider.ignore_update = True
    assert (await controller.update(request)).status == "unknown" and len(provider.updates) == 1

async def test_append_unknown_is_not_retried_and_errors_are_sanitized(setup_controller):
    active, _, provider, controller = setup_controller
    provider.unknown_append = True
    result = await controller.append(WorkAppendRequest(api_version="1", work_id=active, text="history"))
    assert result.status == "unknown" and provider.appends == ["history"]
    provider.unknown_append, provider.confirm_append = False, False
    result = await controller.append(WorkAppendRequest(api_version="1", work_id=active, text="again"))
    assert result.status == "unknown" and provider.appends == ["history", "again"]
    provider.fail_get = True
    result = await controller.get(WorkGetRequest(api_version="1", work_id=active))
    assert result.status == "provider_error" and "secret" not in str(result.model_dump())


@pytest.mark.parametrize("failure", ["fail_after_append", "deny_after_append", "fail_unlock"])
async def test_append_readback_failure_after_post_is_unknown(setup_controller, failure):
    active, _, provider, controller = setup_controller
    setattr(controller.state if failure == "fail_unlock" else provider, failure, True)
    result = await controller.append(
        WorkAppendRequest(api_version="1", work_id=active, text="sent once")
    )
    assert result.status == "unknown" and provider.appends == ["sent once"]


async def test_append_provider_rejection_is_provider_error(setup_controller):
    active, _, provider, controller = setup_controller
    provider.reject_append = True
    result = await controller.append(
        WorkAppendRequest(api_version="1", work_id=active, text="rejected")
    )
    assert result.status == "provider_error" and provider.appends == ["rejected"]


async def test_provision_launch_validates_all_work_before_stable_binding():
    state = FakeState({})
    provider = FakeProvider(ProviderWork("T", "N", False, "r1", Routing(), True))
    first = await provision_launch(state, "fake", provider, "active", ("reference",))
    again = await provision_launch(state, "fake", provider, "active", ("reference",))
    assert first == again
    assert await state.get(first.active_work_id) == Handle(first.active_work_id, "fake", "active")
    assert await state.get(first.reference_work_ids[0]) == Handle(
        first.reference_work_ids[0], "fake", "reference"
    )

    provider.work = ProviderWork("T", "N", False, "r1", Routing(), False)
    empty = FakeState({})
    with pytest.raises(PermissionError, match="all work must be canonical"):
        await provision_launch(empty, "fake", provider, "active", ("reference",))
    assert empty.handles == {}


async def test_provision_launch_rejects_invalid_bounds_before_provider_reads():
    state = FakeState({})
    provider = FakeProvider(ProviderWork("T", "N", False, "r1", Routing(), True))
    with pytest.raises(ValueError, match="distinct"):
        await provision_launch(state, "fake", provider, "same", ("same",))
    with pytest.raises(ValueError, match="eight"):
        await provision_launch(state, "fake", provider, "active", tuple(map(str, range(9))))
    assert state.handles == {}
