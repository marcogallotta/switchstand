from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from chatgpt_fixture import PRINCIPAL, MemoryGrants, grant
from pydantic import ValidationError

from switchstand.chatgpt import ChatGPTService
from switchstand.contracts import (
    GroupedCandidate,
    GroupedLookup,
    LaunchAuthority,
    RelatedCandidate,
    RelatedLookup,
    Routing,
    SourceStoriesRequest,
    SourceStoryRequest,
    SourceTaskRequest,
    WorkAppendRequest,
    WorkContext,
    WorkEventRequest,
    WorkGetRequest,
    WorkHistoryRequest,
)
from switchstand.core import (
    Controller,
    EventBinding,
    Handle,
    ProviderError,
    ProviderSourceStory,
    ProviderSourceTask,
    ProviderStoriesPage,
    ProviderWork,
)
from switchstand.grants import CreateReceipt, GuardOutcome

WORK_ID = UUID("00000000-0000-0000-0000-000000000001")
REFERENCE_ID = UUID("00000000-0000-0000-0000-000000000002")
TASK_GID = "1218431511675555"
STORY_GID = "1218431592688855"


class FakeState:
    def __init__(self):
        self.handles = {
            WORK_ID: Handle(WORK_ID, "asana", TASK_GID),
            REFERENCE_ID: Handle(REFERENCE_ID, "asana", "222"),
        }
        self.events = {}

    async def get(self, work_id):
        return self.handles.get(work_id)

    async def bound_provider_ids(self, provider):
        return frozenset(
            handle.provider_work_id for handle in self.handles.values()
            if handle.provider == provider
        )

    @asynccontextmanager
    async def locked(self, work_id):
        yield self.handles.get(work_id)

    async def bind(self, provider, provider_work_id):
        return next(handle for handle in self.handles.values()
                    if (handle.provider, handle.provider_work_id) == (provider, provider_work_id))

    async def bind_event(self, work_id, provider, provider_work_id, provider_event_id):
        key = (work_id, provider, provider_work_id, provider_event_id)
        binding = self.events.get(key)
        if binding is None:
            binding = EventBinding(uuid4(), work_id, provider, provider_work_id, provider_event_id)
            self.events[key] = binding
        return binding

    async def get_event(self, work_id, event_id):
        return next(
            (binding for binding in self.events.values()
             if binding.work_id == work_id and binding.id == event_id),
            None,
        )


class FakeProvider:
    def __init__(self):
        self.revision = "r1"
        self.related_revision = "r1"
        self.related_reason = None
        self.related_calls = []
        self.grouped_calls = []
        self.grouped_revision = "r1"
        self.canonical = True
        self.story_task_gid = None
        self.story_gid = STORY_GID
        self.story_text = "feedback"
        self.append_count = 0
        self.bump_revision_on_story = False
        self.history_error = False
        self.history_empty = False
        self.history_next_offset = None

    async def get(self, provider_work_id):
        return ProviderWork(
            "Task", "Notes", False, self.revision, Routing(), WorkContext(), self.canonical
        )

    async def find_related(self, work_task_gid):
        self.related_calls.append(work_task_gid)
        return RelatedLookup(status="UH_OH" if self.related_reason else "CANDIDATES",
                             reason=self.related_reason, work_task_gid=work_task_gid,
                             observed_revision=self.related_revision,
                             candidates=(RelatedCandidate(task_gid="777", title="Review",
                                         revision="r1", parent_gid=work_task_gid),))

    async def find_grouped(self, root_task_gid):
        self.grouped_calls.append(root_task_gid)
        return GroupedLookup(status="CANDIDATES", root_task_gid=root_task_gid,
                             observed_revision=self.grouped_revision,
                             candidates=(GroupedCandidate(
                                 task_gid="888", title="Family member", revision="r1",
                                 root_work_gid=root_task_gid,
                                 source="asana_root_work_gid_search_exact_get",
                             ),))

    async def source_task(self, provider_task_id):
        return ProviderSourceTask("Task", "Notes", False, self.revision, self.canonical)

    async def source_stories(self, provider_task_id, observed_revision, offset, limit):
        if self.history_error:
            raise ProviderError("history unavailable")
        if observed_revision != self.revision:
            return ProviderStoriesPage(
                provider_task_id, self.revision, (), None, self.canonical, True
            )
        story = ProviderSourceStory(
            STORY_GID, provider_task_id, "comment_added", self.story_text,
            "2026-09-12T00:00:00Z", "Marco",
        )
        stories = () if self.history_empty else (story,)
        return ProviderStoriesPage(
            provider_task_id, self.revision, stories, self.history_next_offset, self.canonical
        )

    async def source_story(self, provider_task_id, provider_story_id):
        if self.bump_revision_on_story:
            self.revision = "r2"
        return ProviderSourceStory(
            self.story_gid, self.story_task_gid or provider_task_id,
            "comment_added", self.story_text,
            "2026-09-12T00:00:00Z", "Marco",
        )

    async def append(self, provider_work_id, text):
        self.append_count += 1
        self.story_text = text
        return STORY_GID

    async def update(self, provider_work_id, patch):
        raise AssertionError("not used")

    async def suggest_next(self, excluded):
        return None


@pytest.fixture
def setup_controller():
    provider = FakeProvider()
    controller = Controller(
        LaunchAuthority(active_work_id=WORK_ID, reference_work_ids=(REFERENCE_ID,)),
        FakeState(), {"asana": provider}
    )
    return provider, controller


def test_asana_source_identity_is_not_a_work_id():
    with pytest.raises(ValidationError):
        SourceTaskRequest(api_version="1", task_gid=str(WORK_ID))


async def test_exact_task_and_revision_checked_history(setup_controller):
    provider, controller = setup_controller
    active = await controller.get(WorkGetRequest(api_version="1", work_id=WORK_ID))
    assert active.status == "ok" and active.item and active.item.source
    assert active.item.source.provider == "asana" and active.item.source.task_gid == TASK_GID
    assert active.item.id == WORK_ID
    task = await controller.source_task(SourceTaskRequest(api_version="1", task_gid=TASK_GID))
    assert task.status == "ok" and task.item and task.item.task_gid == TASK_GID

    page = await controller.source_stories(
        SourceStoriesRequest(
            api_version="1", task_gid=TASK_GID, observed_revision="r1"
        )
    )
    assert page.status == "ok" and page.revision == "r1"
    assert page.stories[0].story_gid == STORY_GID

    provider.revision = "r2"
    stale = await controller.source_stories(
        SourceStoriesRequest(
            api_version="1", task_gid=TASK_GID, observed_revision="r1"
        )
    )
    assert stale.status == "stale" and stale.revision == "r2" and not stale.stories


async def test_related_read_uses_only_granted_work_id_and_rejects_mixed_revisions(setup_controller):
    provider, controller = setup_controller
    foreign = UUID("00000000-0000-0000-0000-000000000003")
    denied = await controller.get(WorkGetRequest(api_version="1", work_id=foreign,
                                                 include_related=True))
    assert denied.status == "denied" and provider.related_calls == [] and provider.grouped_calls == []

    current = await controller.get(WorkGetRequest(api_version="1", work_id=WORK_ID,
                                                  include_related=True))
    assert current.status == "ok" and current.related is not None
    assert current.related.status == "CANDIDATES"
    assert current.related.candidates[0].parent_gid == TASK_GID
    assert provider.related_calls == [TASK_GID]
    assert current.grouped is not None and current.grouped.complete is False
    assert current.grouped.candidates[0].root_work_gid == TASK_GID
    assert provider.grouped_calls == [TASK_GID]

    provider.grouped_revision = None
    unbound = await controller.get(WorkGetRequest(api_version="1", work_id=WORK_ID,
                                                 include_related=True))
    assert unbound.status == "ok" and unbound.grouped is not None
    assert (unbound.grouped.status, unbound.grouped.reason, unbound.grouped.candidates) == (
        "UH_OH", "work_revision_mismatch", ()
    )
    provider.grouped_revision = "r1"

    provider.related_revision = "r2"
    stale = await controller.get(WorkGetRequest(api_version="1", work_id=WORK_ID,
                                                include_related=True))
    assert stale.status == "ok" and stale.related is not None
    assert (stale.related.status, stale.related.reason, stale.related.candidates) == (
        "UH_OH", "work_revision_mismatch", ())

    provider.related_revision = "r1"
    provider.related_reason = "work_not_canonical"
    removed = await controller.get(WorkGetRequest(api_version="1", work_id=WORK_ID,
                                                  include_related=True))
    assert removed.status == "denied" and removed.item is None and removed.related is None

    provider.related_reason = "work_revision_unavailable"
    provider.related_revision = None
    uncertain = await controller.get(WorkGetRequest(api_version="1", work_id=WORK_ID,
                                                    include_related=True))
    assert uncertain.status == "ok" and uncertain.item is not None
    assert uncertain.related is not None and uncertain.related.status == "UH_OH"


async def test_material_story_reread_checks_revision_and_target(setup_controller):
    provider, controller = setup_controller
    result = await controller.source_story(
        SourceStoryRequest(
            api_version="1", task_gid=TASK_GID,
            story_gid=STORY_GID, observed_revision="r1",
        )
    )
    assert result.status == "ok" and result.item
    assert result.item.task_gid == TASK_GID

    provider.story_task_gid = "999"
    denied = await controller.source_story(
        SourceStoryRequest(
            api_version="1", task_gid=TASK_GID,
            story_gid=STORY_GID, observed_revision="r1",
        )
    )
    assert denied.status == "denied"

    provider.story_task_gid = TASK_GID
    provider.bump_revision_on_story = True
    stale = await controller.source_story(
        SourceStoryRequest(
            api_version="1", task_gid=TASK_GID,
            story_gid=STORY_GID, observed_revision="r1",
        )
    )
    assert stale.status == "stale" and stale.revision == "r2"


async def test_append_returns_exact_created_story_and_target(setup_controller):
    provider, controller = setup_controller
    result = await controller.append(
        WorkAppendRequest(api_version="1", work_id=WORK_ID, text="feedback")
    )
    assert result.status == "ok"
    assert result.task_gid == TASK_GID and result.story_gid == STORY_GID
    assert provider.append_count == 1

    provider.story_task_gid = "999"
    unknown = await controller.append(
        WorkAppendRequest(api_version="1", work_id=WORK_ID, text="second")
    )
    assert unknown.status == "unknown" and provider.append_count == 2


async def test_exact_story_identity_is_required_for_read_and_append(setup_controller):
    provider, controller = setup_controller
    provider.story_gid = "999"
    reread = await controller.source_story(SourceStoryRequest(
        api_version="1", task_gid=TASK_GID, story_gid=STORY_GID, observed_revision="r1",
    ))
    assert reread.status == "denied" and reread.item is None
    appended = await controller.append(
        WorkAppendRequest(api_version="1", work_id=WORK_ID, text="feedback")
    )
    assert appended.status == "unknown" and appended.story_gid is None
    assert provider.append_count == 1


async def test_work_history_uses_provider_neutral_event_identity(setup_controller):
    _provider, controller = setup_controller
    page = await controller.history(WorkHistoryRequest(
        api_version="1", work_id=WORK_ID, observed_revision="r1"
    ))
    assert page.status == "ok" and page.work_id == WORK_ID and page.revision == "r1"
    assert len(page.events) == 1
    event = page.events[0]
    assert event.work_id == WORK_ID and event.id.version == 4
    assert event.text == "feedback" and event.actor == "Marco"

    exact = await controller.event(WorkEventRequest(
        api_version="1", work_id=WORK_ID, event_id=event.id, observed_revision="r1"
    ))
    assert exact.status == "ok" and exact.item == event

    denied = await controller.history(WorkHistoryRequest(
        api_version="1", work_id=UUID("00000000-0000-0000-0000-000000000003"),
        observed_revision="r1",
    ))
    assert denied.status == "denied"


async def test_work_event_preserves_currentness_and_target_checks(setup_controller):
    provider, controller = setup_controller
    page = await controller.history(WorkHistoryRequest(
        api_version="1", work_id=WORK_ID, observed_revision="r1"
    ))
    event_id = page.events[0].id

    provider.revision = "r2"
    stale = await controller.event(WorkEventRequest(
        api_version="1", work_id=WORK_ID, event_id=event_id, observed_revision="r1"
    ))
    assert stale.status == "stale" and stale.revision == "r2" and stale.item is None

    provider.revision = "r1"
    provider.story_task_gid = "999"
    wrong_target = await controller.event(WorkEventRequest(
        api_version="1", work_id=WORK_ID, event_id=event_id, observed_revision="r1"
    ))
    assert wrong_target.status == "denied" and wrong_target.item is None

    provider.story_task_gid = TASK_GID
    forged = await controller.event(WorkEventRequest(
        api_version="1", work_id=WORK_ID, event_id=uuid4(), observed_revision="r1"
    ))
    assert forged.status == "denied" and forged.item is None

    cross_work = await controller.event(WorkEventRequest(
        api_version="1", work_id=REFERENCE_ID, event_id=event_id, observed_revision="r1"
    ))
    assert cross_work.status == "denied" and cross_work.item is None

    provider.bump_revision_on_story = True
    changed_mid_read = await controller.event(WorkEventRequest(
        api_version="1", work_id=WORK_ID, event_id=event_id, observed_revision="r1"
    ))
    assert changed_mid_read.status == "stale"
    assert changed_mid_read.revision == "r2" and changed_mid_read.item is None


async def test_work_history_provider_error_empty_and_pagination_are_distinct(setup_controller):
    provider, controller = setup_controller

    provider.history_error = True
    unavailable = await controller.history(WorkHistoryRequest(
        api_version="1", work_id=WORK_ID, observed_revision="r1"
    ))
    assert unavailable.status == "provider_error" and not unavailable.events

    provider.history_error = False
    provider.history_next_offset = "more"
    paged = await controller.history(WorkHistoryRequest(
        api_version="1", work_id=WORK_ID, observed_revision="r1", limit=1
    ))
    assert paged.status == "ok" and len(paged.events) == 1 and paged.next_cursor == "more"

    provider.history_empty = True
    provider.history_next_offset = None
    empty = await controller.history(WorkHistoryRequest(
        api_version="1", work_id=WORK_ID, observed_revision="r1"
    ))
    assert empty.status == "ok" and empty.events == () and empty.next_cursor is None


async def test_chatgpt_created_work_history_requires_exact_applied_create_evidence():
    state, provider = FakeState(), FakeProvider()
    created, merely_bound = uuid4(), uuid4()
    state.handles[created] = Handle(created, "asana", "789")
    state.handles[merely_bound] = Handle(merely_bound, "asana", "790")
    selected = grant(active=WORK_ID, reference=REFERENCE_ID)
    grants = MemoryGrants(selected)

    async def principal():
        return PRINCIPAL

    service = ChatGPTService(principal, state, grants, {"asana": provider})
    operation_id = uuid4()
    receipt = CreateReceipt(
        operation_id=operation_id,
        principal=PRINCIPAL,
        grant_id=selected.id,
        grant_version=selected.version,
        work_id=created,
        provider="asana",
        task_gid="789",
        parent_task_gid=TASK_GID,
        title="Created",
        qualification="test:create",
    )
    unknown = GuardOutcome(
        status="unknown",
        operation="work_create",
        work_id=created,
        operation_id=operation_id,
        reason="prepared_or_unconfirmed_send",
        effect="unknown",
        retry="reconcile",
        next_action="Reconcile the recorded create.",
    )
    outcome = GuardOutcome(
        status="ok",
        operation="work_create",
        work_id=created,
        operation_id=operation_id,
        reason="exact_create_verified",
        effect="applied",
        retry="none",
        next_action="Use the recorded create receipt.",
        receipt=receipt,
    )
    await grants.prepare({}, selected, "fingerprint", unknown)
    await grants.finish(outcome)
    assert await grants.created_work_allowed(PRINCIPAL.key, created)
    assert (await service.get(created)).status == "ok"

    page = await service.history(WorkHistoryRequest(
        api_version="1", work_id=created, observed_revision="r1"
    ))
    assert page.status == "ok" and page.events
    exact = await service.event(WorkEventRequest(
        api_version="1", work_id=created,
        event_id=page.events[0].id, observed_revision="r1",
    ))
    assert exact.status == "ok" and exact.item == page.events[0]

    denied = await service.history(WorkHistoryRequest(
        api_version="1", work_id=merely_bound, observed_revision="r1"
    ))
    assert denied.status == "denied"
