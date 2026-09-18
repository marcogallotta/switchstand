from contextlib import asynccontextmanager
from uuid import uuid4

from chatgpt_fixture import ACTIVE, PRINCIPAL, MemoryGrants, grant

from switchstand.contracts import Routing, WorkPatch
from switchstand.core import Handle, ProviderError, ProviderWork, UnknownEffect
from switchstand.grants import ProtectedUpdate
from switchstand.updates import UpdateGateway


class State:
    def __init__(self):
        self.handle = Handle(ACTIVE, "asana", "123")

    async def get(self, work_id):
        return self.handle if work_id == ACTIVE else None


class Provider:
    def __init__(self):
        self.title = "Before"
        self.notes = "notes"
        self.completed = False
        self.priority = "P0"
        self.work_type = "Research"
        self.revision = "r1"
        self.sends = 0
        self.lose_response = False
        self.apply_before_loss = True
        self.reject = False

    async def get(self, _task_gid):
        return ProviderWork(
            self.title,
            self.notes,
            self.completed,
            self.revision,
            Routing(priority=self.priority, work_type=self.work_type),
            True,
        )

    async def update(self, _task_gid, patch):
        self.sends += 1
        if self.reject:
            raise ProviderError("definite rejection")
        if not self.lose_response or self.apply_before_loss:
            for field in patch.model_fields_set:
                value = getattr(patch, field)
                if field == "title":
                    self.title = value
                elif field == "notes":
                    self.notes = value
                elif field == "completed":
                    self.completed = value
                elif field == "priority":
                    self.priority = value
                elif field == "work_type":
                    self.work_type = value
            self.revision = f"r{self.sends + 1}"
        if self.lose_response:
            raise UnknownEffect("lost provider response")


def subject(**grant_changes):
    selected = grant(
        operations=frozenset({"work_get", "work_update"}),
        append_qualification=None,
        update_qualification="test:update",
        **grant_changes,
    )
    state, provider = State(), Provider()
    grants = MemoryGrants(selected)
    return UpdateGateway(state, grants, {"asana": provider}), selected, grants, provider


def request(selected, *, operation_id=None, revision="r1", patch=None, **changes):
    values = {
        "api_version": "1",
        "operation_id": operation_id or uuid4(),
        "work_id": ACTIVE,
        "grant_version": selected.version,
        "observed_revision": revision,
        "patch": patch or WorkPatch(title="After", priority="P-CRITICAL"),
    }
    return ProtectedUpdate(**(values | changes))


async def test_verified_update_replays_exact_operation_without_second_send():
    gateway, selected, _grants, provider = subject()
    req = request(selected)
    first = await gateway.update(PRINCIPAL, req)
    assert first.status == "ok" and first.effect == "applied"
    assert first.receipt is not None
    assert first.receipt.work_id == ACTIVE
    assert first.receipt.observed_revision == "r1"
    assert first.receipt.result_revision == "r2"
    assert first.receipt.patch.title == "After"
    assert first.receipt.patch.priority == "P-CRITICAL"
    assert provider.sends == 1

    replay = await gateway.update(PRINCIPAL, req)
    assert replay == first
    assert provider.sends == 1


async def test_lost_response_after_provider_commit_reconciles_same_update():
    gateway, selected, _grants, provider = subject()
    provider.lose_response = True
    req = request(selected)

    result = await gateway.update(PRINCIPAL, req)
    assert result.status == "ok" and result.effect == "applied"
    assert result.reason == "exact_update_verified"
    assert provider.sends == 1
    assert provider.title == "After" and provider.priority == "P-CRITICAL"

    assert await gateway.update(PRINCIPAL, req) == result
    assert provider.sends == 1


async def test_unresolved_update_blocks_new_effect_then_recovers_without_resend():
    gateway, selected, grants, provider = subject()
    provider.lose_response = True
    provider.apply_before_loss = False
    req = request(selected)

    first = await gateway.update(PRINCIPAL, req)
    assert first.status == "unknown" and first.effect == "unknown"
    assert first.retry == "reconcile" and provider.sends == 1

    changed = request(
        selected,
        operation_id=uuid4(),
        patch=WorkPatch(title="Different"),
    )
    blocked = await gateway.update(PRINCIPAL, changed)
    assert blocked.status == "unknown"
    assert blocked.reason == "target_has_unresolved_effect"
    assert provider.sends == 1

    renewed = selected.model_copy(update={"id": uuid4(), "version": 2})
    grants.grant = renewed
    provider.title = "After"
    provider.priority = "P-CRITICAL"
    provider.revision = "r2"
    recovered = await gateway.update(PRINCIPAL, req)
    assert recovered.status == "ok" and recovered.effect == "applied"
    assert recovered.receipt is not None
    assert recovered.receipt.grant_id == selected.id
    assert recovered.receipt.grant_version == selected.version
    assert provider.sends == 1


async def test_same_operation_changed_payload_conflicts_without_send():
    gateway, selected, _grants, provider = subject()
    req = request(selected)
    assert (await gateway.update(PRINCIPAL, req)).status == "ok"
    conflict = await gateway.update(
        PRINCIPAL, req.model_copy(update={"patch": WorkPatch(title="Different")})
    )
    assert conflict.status == "denied"
    assert conflict.reason == "operation_identity_conflict"
    assert provider.sends == 1


async def test_revision_scope_version_and_qualification_deny_before_send():
    gateway, selected, grants, provider = subject()

    stale = await gateway.update(PRINCIPAL, request(selected, revision="old"))
    assert stale.status == "stale"

    wrong_work = await gateway.update(
        PRINCIPAL, request(selected, work_id=uuid4())
    )
    assert wrong_work.status == "denied"

    wrong_version = await gateway.update(
        PRINCIPAL, request(selected, grant_version=2)
    )
    assert wrong_version.status == "stale"

    unqualified = selected.model_copy(update={
        "id": uuid4(), "version": 2, "update_qualification": None,
    })
    grants.grant = unqualified
    denied = await gateway.update(PRINCIPAL, request(unqualified))
    assert denied.status == "denied"
    assert denied.reason == "update_not_qualified_for_this_surface"
    assert provider.sends == 0


async def test_definite_provider_rejection_is_not_applied_and_new_operation_can_follow():
    gateway, selected, _grants, provider = subject()
    provider.reject = True
    first = await gateway.update(PRINCIPAL, request(selected))
    assert first.status == "not_applied" and first.effect == "not_sent"
    assert provider.sends == 1

    provider.reject = False
    recovered = await gateway.update(
        PRINCIPAL, request(selected, operation_id=uuid4())
    )
    assert recovered.status == "ok"
    assert provider.sends == 2


async def test_completed_work_can_reopen_through_same_scalar_gateway():
    gateway, selected, _grants, provider = subject()
    provider.completed = True
    result = await gateway.update(
        PRINCIPAL,
        request(selected, patch=WorkPatch(completed=False)),
    )
    assert result.status == "ok" and result.effect == "applied"
    assert provider.completed is False
    assert provider.sends == 1
