from contextlib import asynccontextmanager
from uuid import UUID

from pydantic import ValidationError
import pytest

from switchstand.contracts import (
    LaunchAuthority,
    Routing,
    SourceStoriesRequest,
    SourceStoryRequest,
    SourceTaskRequest,
    WorkAppendRequest,
)
from switchstand.core import (
    Controller,
    Handle,
    ProviderSourceStory,
    ProviderSourceTask,
    ProviderStoriesPage,
    ProviderWork,
)

WORK_ID = UUID("00000000-0000-0000-0000-000000000001")
TASK_GID = "1218431511675555"
STORY_GID = "1218431592688855"


class FakeState:
    def __init__(self):
        self.handle = Handle(WORK_ID, "asana", TASK_GID)

    async def get(self, work_id):
        return self.handle if work_id == WORK_ID else None

    async def bound_provider_ids(self, provider):
        return frozenset({TASK_GID}) if provider == "asana" else frozenset()

    @asynccontextmanager
    async def locked(self, work_id):
        yield self.handle if work_id == WORK_ID else None

    async def bind(self, provider, provider_work_id):
        return self.handle


class FakeProvider:
    def __init__(self):
        self.revision = "r1"
        self.canonical = True
        self.story_task_gid = TASK_GID
        self.story_text = "feedback"
        self.append_count = 0

    async def get(self, provider_work_id):
        return ProviderWork("Task", "Notes", False, self.revision, Routing(), self.canonical)

    async def source_task(self, provider_task_id):
        return ProviderSourceTask("Task", "Notes", False, self.revision, self.canonical)

    async def source_stories(self, provider_task_id, observed_revision, offset, limit):
        if observed_revision != self.revision:
            return ProviderStoriesPage(
                provider_task_id, self.revision, (), None, self.canonical, True
            )
        story = ProviderSourceStory(
            STORY_GID, provider_task_id, "comment_added", self.story_text,
            "2026-09-12T00:00:00Z", "Marco",
        )
        return ProviderStoriesPage(
            provider_task_id, self.revision, (story,), None, self.canonical
        )

    async def source_story(self, provider_task_id, provider_story_id):
        return ProviderSourceStory(
            provider_story_id, self.story_task_gid, "comment_added", self.story_text,
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
        LaunchAuthority(active_work_id=WORK_ID), FakeState(), {"asana": provider}
    )
    return provider, controller


def test_asana_source_identity_is_not_a_work_id():
    with pytest.raises(ValidationError):
        SourceTaskRequest(api_version="1", task_gid=str(WORK_ID))


async def test_exact_task_and_revision_checked_history(setup_controller):
    provider, controller = setup_controller
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
