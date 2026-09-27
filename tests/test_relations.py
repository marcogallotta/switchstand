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

    async def update_relation(self, task_gid, patch):
        assert task_gid == "123"
        self.relation_sends += 1
        if self.lose_without_apply:
            raise UnknownEffect("lost")
        self.relation = patch

    async def relation_matches(self, task_gid, patch):
        assert task_gid == "123"
        return self.relation == patch


def request(selected, patch):
    return ProtectedRelation(
        api_version="1", operation_id=uuid4(), work_id=ACTIVE,
        grant_version=selected.version, observed_revision="r1", patch=patch,
    )


async def test_relation_gateway_resolves_workid_and_replays_without_resend():
    selected = grant(
        scope="workspace", operations=frozenset({"work_update"}),
        update_qualification="test:relations",
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
        scope="workspace", operations=frozenset({"work_update"}),
        update_qualification="test:relations",
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
    assert blocked.reason == "target_has_unresolved_effect"
    assert provider.relation_sends == 1
