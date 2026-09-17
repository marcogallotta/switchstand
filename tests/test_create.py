from uuid import uuid4

from chatgpt_fixture import ACTIVE, PRINCIPAL, MemoryGrants, grant

from switchstand.chatgpt import ChatGPTService
from switchstand.contracts import SourceTaskRequest
from switchstand.core import Handle, ProviderSourceTask, UnknownEffect
from switchstand.creates import CreateGateway
from switchstand.grants import ProtectedCreate


class State:
    def __init__(self):
        self.handles = {ACTIVE: Handle(ACTIVE, "asana", "123")}

    async def get(self, work_id):
        return self.handles.get(work_id)

    async def bind_reserved(self, work_id, provider, provider_work_id):
        existing = self.handles.get(work_id)
        if existing is not None and (existing.provider, existing.provider_work_id) != (provider, provider_work_id):
            raise ValueError("binding conflict")
        handle = Handle(work_id, provider, provider_work_id)
        self.handles[work_id] = handle
        return handle


class Provider:
    def __init__(self):
        self.created = {}
        self.creates = 0
        self.lose_response = False
        self.visible = True

    async def source_task(self, task_gid):
        if task_gid == "123":
            return ProviderSourceTask("Parent", "", False, "r1", True)
        if task_gid in self.created.values():
            return ProviderSourceTask("Created", "notes", False, "r2", True)
        return None

    async def create_child(self, parent_task_gid, title, notes, operation_id):
        self.creates += 1
        task_gid = str(9000 + self.creates)
        self.created[operation_id] = task_gid
        if self.lose_response:
            raise UnknownEffect("lost response")
        return task_gid

    async def recover_created(self, parent_task_gid, operation_id):
        return self.created.get(operation_id) if self.visible else None


def subject():
    selected = grant(
        operations=frozenset({"work_get", "work_create"}),
        append_qualification=None, create_qualification="test:create",
    )
    state, provider = State(), Provider()
    grants = MemoryGrants(selected)

    async def principal():
        return PRINCIPAL

    return ChatGPTService(principal, state, grants, {"asana": provider}), selected, state, provider


def request(selected, operation_id=None, **changes):
    values = {
        "api_version": "1", "operation_id": operation_id or uuid4(),
        "parent_work_id": selected.authority.active_work_id,
        "grant_version": selected.version, "title": "Created", "notes": "notes",
    }
    return ProtectedCreate(**(values | changes))


async def test_create_binds_reserved_work_and_exact_readback():
    service, selected, state, provider = subject()
    req = request(selected)
    result = await service.create(req)
    assert result.status == "ok" and result.effect == "applied"
    assert result.receipt.task_gid == "9001" and result.receipt.parent_task_gid == "123"
    assert result.work_id == CreateGateway.work_id(req.operation_id)
    assert (await state.get(result.work_id)).provider_work_id == "9001"
    source = await service.source_task(SourceTaskRequest(api_version="1", task_gid="9001"))
    assert source.status == "ok" and source.item.title == "Created"
    assert provider.creates == 1


async def test_lost_create_response_recovers_after_restart_without_second_send():
    service, selected, state, provider = subject()
    req = request(selected)
    provider.lose_response, provider.visible = True, False
    first = await service.create(req)
    assert first.status == "unknown" and first.effect == "unknown"
    assert provider.creates == 1

    provider.visible = True
    restarted = ChatGPTService(service.principal, state, service.grants, service.providers)
    recovered = await restarted.create(req)
    assert recovered.status == "ok" and recovered.effect == "applied"
    assert recovered.receipt.task_gid == "9001"
    assert provider.creates == 1
    assert await restarted.create(req) == recovered
    assert provider.creates == 1


async def test_operation_identity_and_qualification_block_unsafe_create():
    service, selected, _state, provider = subject()
    req = request(selected)
    provider.lose_response, provider.visible = True, False
    assert (await service.create(req)).status == "unknown"
    conflict = await service.create(req.model_copy(update={"title": "Different"}))
    assert conflict.status == "denied" and conflict.reason == "operation_identity_conflict"

    unqualified = selected.model_copy(update={"version": 2, "create_qualification": None})
    service.grants.grant = unqualified
    denied = await service.create(request(unqualified))
    assert denied.status == "denied" and denied.effect == "not_sent"
    assert provider.creates == 1
