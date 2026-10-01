from uuid import uuid4

import pytest
from pydantic import ValidationError

from switchstand.contracts import Routing, WorkContext, WorkSearchRequest
from switchstand.core import Handle, ProviderError
from switchstand.discovery import (
    ProviderSearchItem,
    ProviderSearchPage,
    ProviderStructure,
    WorkDiscovery,
)


class MemoryState:
    def __init__(self):
        self.handles: dict[tuple[str, str], Handle] = {}

    async def bind(self, provider: str, provider_work_id: str) -> Handle:
        key = provider, provider_work_id
        if key not in self.handles:
            self.handles[key] = Handle(uuid4(), provider, provider_work_id)
        return self.handles[key]

    async def bind_many(
        self, provider: str, provider_work_ids: tuple[str, ...]
    ) -> tuple[Handle, ...]:
        return tuple([await self.bind(provider, provider_work_id)
                      for provider_work_id in provider_work_ids])


class FakeProvider:
    def __init__(self):
        self.calls: list[tuple[object, ...]] = []

    async def search_work(self, text, completed, cursor, limit):
        self.calls.append((text, completed, cursor, limit))
        return ProviderSearchPage(
            items=(
                ProviderSearchItem(
                    provider_work_id="101", title="Alpha", completed=False,
                    revision="r1", routing=Routing(priority="P0"),
                    context=WorkContext(assignee="Owner"),
                ),
                ProviderSearchItem(
                    provider_work_id="202", title="Beta", completed=True,
                    revision="r2", routing=Routing(priority="P1"),
                    context=WorkContext(),
                ),
            ),
            next_cursor="next",
        )

    async def structure_work(self, provider_work_id, observed_revision):
        self.calls.append((provider_work_id, observed_revision))
        def item(gid):
            return ProviderSearchItem(
                provider_work_id=gid, title=gid, completed=False, revision="r1",
                routing=Routing(), context=WorkContext(),
            )
        return ProviderStructure(
            status="ok", revision="r1", parent=item("parent"),
            children=(item("child-1"), item("child-2")),
        )


async def test_discovery_returns_only_stable_provider_neutral_work_ids():
    provider = FakeProvider()
    state = MemoryState()
    subject = WorkDiscovery("asana", provider, state)
    request = WorkSearchRequest(
        api_version="1", text="task", completed=None, cursor=None, limit=10
    )
    first = await subject.search(request)
    second = await subject.search(request)
    assert first.status == second.status == "ok"
    assert first.next_cursor == second.next_cursor == "next"
    assert [item.id for item in first.items] == [item.id for item in second.items]
    assert [item.title for item in first.items] == ["Alpha", "Beta"]
    assert [item.context.assignee for item in first.items] == ["Owner", None]
    assert provider.calls == [("task", None, None, 10), ("task", None, None, 10)]
    rendered = first.model_dump(mode="json")
    assert all("provider" not in item and "task_gid" not in item and "gid" not in item
               for item in rendered["items"])


async def test_structure_binds_complete_provider_snapshot_to_stable_ids():
    provider = FakeProvider()
    state = MemoryState()
    subject = WorkDiscovery("asana", provider, state)

    first = await subject.structure(uuid4(), "target", "r1")
    second = await subject.structure(uuid4(), "target", "r1")

    assert first is not None and second is not None
    assert first.status == second.status == "ok"
    assert first.parent is not None and second.parent is not None
    assert first.parent.id == second.parent.id
    assert [child.id for child in first.children] == [
        child.id for child in second.children
    ]
    assert set(state.handles) == {
        ("asana", "parent"), ("asana", "child-1"), ("asana", "child-2"),
    }


async def test_structure_stale_snapshot_does_not_bind_relations():
    class StaleProvider(FakeProvider):
        async def structure_work(self, *_args):
            return ProviderStructure(status="stale", revision="r2")

    state = MemoryState()
    result = await WorkDiscovery("asana", StaleProvider(), state).structure(
        uuid4(), "target", "r1"
    )

    assert result is not None and result.status == "stale" and result.revision == "r2"
    assert result.parent is None and result.children == () and state.handles == {}


@pytest.mark.parametrize("parent_id, child_id", [
    ("target", "child"),
    ("same", "same"),
])
async def test_structure_invalid_relation_identities_do_not_bind(parent_id, child_id):
    class InvalidProvider(FakeProvider):
        async def structure_work(self, *_args):
            def item(provider_work_id):
                return ProviderSearchItem(
                    provider_work_id, provider_work_id, False, "r1",
                    Routing(), WorkContext(),
                )

            return ProviderStructure(
                "ok", "r1", item(parent_id), (item(child_id),)
            )

    class NoBindingState(MemoryState):
        async def bind_many(self, *_args):
            raise AssertionError("invalid identities must be rejected before binding")

    state = NoBindingState()
    result = await WorkDiscovery("asana", InvalidProvider(), state).structure(
        uuid4(), "target", "r1"
    )

    assert result is None
    assert state.handles == {}


async def test_structure_later_batch_failure_leaves_no_bindings():
    class FailingState(MemoryState):
        async def bind_many(self, provider, provider_work_ids):
            staged = dict(self.handles)
            for index, provider_work_id in enumerate(provider_work_ids):
                if index == 1:
                    raise ValueError("injected later binding failure")
                staged[(provider, provider_work_id)] = Handle(
                    uuid4(), provider, provider_work_id
                )
            self.handles = staged
            return tuple(staged[(provider, item)] for item in provider_work_ids)

    state = FailingState()
    result = await WorkDiscovery("asana", FakeProvider(), state).structure(
        uuid4(), "target", "r1"
    )

    assert result is None
    assert state.handles == {}


class BrokenProvider:
    async def search_work(self, *_args):
        raise ProviderError("hidden provider detail")


async def test_discovery_provider_failure_returns_closed_error_without_partial_data():
    result = await WorkDiscovery("asana", BrokenProvider(), MemoryState()).search(
        WorkSearchRequest(api_version="1")
    )
    assert result.status == "provider_error"
    assert result.items == () and result.next_cursor is None


@pytest.mark.parametrize("values", [
    {"text": ""},
    {"cursor": ""},
    {"cursor": "x" * 1025},
    {"limit": 0},
    {"limit": 101},
    {"unexpected": True},
])
def test_discovery_request_contract_is_closed_and_bounded(values):
    with pytest.raises(ValidationError):
        WorkSearchRequest.model_validate({"api_version": "1"} | values)
