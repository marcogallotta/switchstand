from unittest.mock import AsyncMock
from uuid import uuid4

from chatgpt_fixture import ACTIVE, PRINCIPAL, REFERENCE, Handles, MemoryGrants, Provider, grant

from switchstand.core import ProviderRelation, UnknownEffect
from switchstand.grants import ProtectedRelation, RelationPatch
from switchstand.relations import RelationGateway


class RelationProvider(Provider):
    def __init__(self):
        super().__init__()
        self.relation = None
        self.relation_sends = 0
        self.lose_without_apply = False
        self.partial_move = False
        self.move_target_present = False
        self.move_old_present = True
        self.move_adds = 0
        self.move_removals = 0

    async def update_relation(self, task_gid, patch):
        assert task_gid == "123"
        self.relation_sends += 1
        if patch.kind == "placement" and patch.action == "move" and self.partial_move:
            if not self.move_target_present:
                self.move_target_present = True
                self.move_adds += 1
                raise UnknownEffect("lost after target add")
            if self.move_old_present:
                self.move_old_present = False
                self.move_removals += 1
            return
        if self.lose_without_apply:
            raise UnknownEffect("lost")
        self.relation = patch

    async def relation_matches(self, task_gid, patch):
        assert task_gid == "123"
        if patch.kind == "placement" and patch.action == "move" and self.partial_move:
            return self.move_target_present and not self.move_old_present
        return self.relation == patch


def request(selected, patch):
    return ProtectedRelation(
        api_version="1", operation_id=uuid4(), work_id=ACTIVE,
        grant_version=selected.version, observed_revision="r1", patch=patch,
    )


async def test_relation_gateway_resolves_workid_and_replays_without_resend():
    selected = grant(
        scope="workspace", operations=frozenset({"work_relate"}),
        relation_qualification="test:relations",
    )
    grants = MemoryGrants(selected)
    provider = RelationProvider()
    gateway = RelationGateway(Handles(), grants, {"asana": provider})
    change = request(
        selected, RelationPatch(kind="parent", action="set", target_work_id=REFERENCE)
    )

    applied = await gateway.update(PRINCIPAL, change)
    assert applied.effect == "applied"
    assert applied.receipt.patch == change.patch
    assert provider.relation == ProviderRelation(
        kind="parent", action="set", target_gid="456"
    )
    assert (await gateway.update(PRINCIPAL, change)) == applied
    assert provider.relation_sends == 1


async def test_relation_gateway_keeps_ambiguous_send_unknown_and_blocks_new_effect():
    selected = grant(
        scope="workspace", operations=frozenset({"work_relate"}),
        relation_qualification="test:relations",
    )
    grants = MemoryGrants(selected)
    provider = RelationProvider()
    provider.lose_without_apply = True
    gateway = RelationGateway(Handles(), grants, {"asana": provider})
    first = request(
        selected, RelationPatch(kind="dependency", action="add", target_work_id=REFERENCE)
    )

    unknown = await gateway.update(PRINCIPAL, first)
    assert (unknown.status, unknown.effect, provider.relation_sends) == ("unknown", "unknown", 1)
    assert (await gateway.update(PRINCIPAL, first)).effect == "unknown"
    assert provider.relation_sends == 1

    blocked = await gateway.update(
        PRINCIPAL,
        request(selected, RelationPatch(kind="assignee", action="set", assignee_gid="42")),
    )
    assert (blocked.reason, blocked.effect, blocked.retry) == (
        "target_has_unresolved_effect", "not_sent", "none",
    )
    assert blocked.blocked_by is not None
    assert blocked.blocked_by.operation_id == first.operation_id
    assert provider.relation_sends == 1


async def test_relation_gateway_resumes_partial_move_under_same_operation_id():
    selected = grant(
        scope="workspace", operations=frozenset({"work_relate"}),
        relation_qualification="test:relations",
    )
    grants = MemoryGrants(selected)
    provider = RelationProvider()
    provider.partial_move = True
    gateway = RelationGateway(Handles(), grants, {"asana": provider})
    change = request(
        selected,
        RelationPatch(
            kind="placement", action="move",
            project_gid="1218210259719507", section_gid="42",
        ),
    )

    unknown = await gateway.update(PRINCIPAL, change)
    assert (unknown.status, unknown.effect) == ("unknown", "unknown")
    assert (provider.move_adds, provider.move_removals) == (1, 0)

    applied = await gateway.update(PRINCIPAL, change)

    assert (applied.status, applied.effect) == ("ok", "applied")
    assert applied.operation_id == change.operation_id
    assert (provider.move_adds, provider.move_removals) == (1, 1)
    assert provider.relation_sends == 2
    assert await gateway.update(PRINCIPAL, change) == applied
    assert provider.relation_sends == 2


async def test_update_only_authority_denies_before_state_or_provider_access(monkeypatch):
    selected = grant(
        scope="workspace", operations=frozenset({"work_update"}),
        update_qualification="test:updates",
    )
    state = Handles()
    provider = RelationProvider()
    forbidden = [AsyncMock(side_effect=AssertionError("unauthorized access")) for _ in range(4)]
    monkeypatch.setattr(state, "get", forbidden[0])
    for method, replacement in zip(
        ("get", "update_relation", "relation_matches"), forbidden[1:], strict=True,
    ):
        monkeypatch.setattr(provider, method, replacement)
    gateway = RelationGateway(state, MemoryGrants(selected), {"asana": provider})

    denied = await gateway.update(
        PRINCIPAL,
        request(selected, RelationPatch(kind="assignee", action="set", assignee_gid="42")),
    )

    assert denied.status == "denied"
    assert denied.reason == "operation_or_work_not_granted"
    assert denied.effect == "not_sent"
    assert all(access.await_count == 0 for access in forbidden)


async def test_relation_authority_requires_its_own_qualification():
    selected = grant(
        scope="workspace", operations=frozenset({"work_relate"}),
        update_qualification="test:updates",
    )
    provider = RelationProvider()
    gateway = RelationGateway(Handles(), MemoryGrants(selected), {"asana": provider})

    denied = await gateway.update(
        PRINCIPAL,
        request(selected, RelationPatch(kind="assignee", action="clear")),
    )

    assert denied.status == "denied"
    assert denied.reason == "relation_not_qualified_for_this_surface"
    assert denied.effect == "not_sent"
    assert provider.relation_sends == 0
