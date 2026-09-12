"""Explicit fake caller/provider for local protocol tests; never a ChatGPT identity claim."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from switchstand.chatgpt import ChatGPTService
from switchstand.contracts import LaunchAuthority, Routing
from switchstand.core import (
    Handle,
    ProviderSourceStory,
    ProviderSourceTask,
    ProviderStoriesPage,
    ProviderWork,
    UnknownEffect,
)
from switchstand.grants import PrincipalContext, WorkGrant

ACTIVE = UUID("00000000-0000-0000-0000-000000000001")
REFERENCE = UUID("00000000-0000-0000-0000-000000000002")
PRINCIPAL = PrincipalContext(issuer="fixture", subject="owner", client_id="local-test",
                             assurance="test")


def grant(principal=PRINCIPAL, active=ACTIVE, reference=REFERENCE, **changes):
    values = {'id': uuid4(), 'version': 1, 'principal': principal, 'authority': LaunchAuthority(active_work_id=active, reference_work_ids=(reference,)), 'operations': frozenset({"work_get", "work_append"}), 'issuer': "fixture-operator", 'provenance': "local fixture only", 'append_qualification': "test:disposable", 'expires_at': datetime.now(UTC) + timedelta(hours=1)}
    return WorkGrant(**(values | changes))


class Handles:
    def __init__(self, active=ACTIVE, reference=REFERENCE):
        self.handles = {active: Handle(active, "asana", "123"),
                        reference: Handle(reference, "asana", "456")}

    async def get(self, work_id):
        return self.handles.get(work_id)


class Provider:
    def __init__(self):
        self.notes, self.revision, self.stories = "initial notes", "r1", []
        self.sends = 0
        self.unknown = self.mismatch = self.cancel = False
        self.before_send = None

    async def get(self, task_gid):
        return ProviderWork("Task", self.notes, False, self.revision, Routing(priority="P0"),
                            task_gid in {"123", "456"})

    async def source_task(self, task_gid):
        return ProviderSourceTask("Task", self.notes, False, self.revision,
                                  task_gid in {"123", "456"})

    async def append(self, task_gid, text):
        if self.before_send:
            await self.before_send()
        self.sends += 1
        story = ProviderSourceStory(str(self.sends), task_gid, "comment_added", text,
                                    "2026-09-12T00:00:00Z", "shared-provider-author")
        self.stories.append(story)
        self.revision = f"r{self.sends + 1}"
        if self.cancel:
            import asyncio
            raise asyncio.CancelledError
        if self.unknown:
            raise UnknownEffect("injected lost response")
        return story.story_gid

    async def source_story(self, task_gid, story_gid):
        story = next((s for s in self.stories if s.story_gid == story_gid), None)
        if story and self.mismatch:
            return ProviderSourceStory(story_gid, "999", story.subtype, story.text,
                                        story.created_at, story.created_by)
        return story

    async def source_stories(self, task_gid, revision, offset, limit):
        if revision != self.revision:
            return ProviderStoriesPage(task_gid, self.revision, (), None, True, stale=True)
        start = int(offset or 0)
        stories = tuple(s for s in self.stories if s.task_gid == task_gid)
        end = start + limit
        return ProviderStoriesPage(task_gid, revision, stories[start:end],
                                    str(end) if end < len(stories) else None, True)


class MemoryGrants:
    def __init__(self, initial):
        self.grant = initial
        self.effects = {}

    async def current(self, key):
        return self.grant if self.grant and self.grant.principal.key == key else None

    @asynccontextmanager
    async def locked(self, key, work_id=None):
        yield await self.current(key)

    async def previous(self, operation_id, fingerprint, work_id):
        candidates = [v for k, v in self.effects.items()
                      if k == operation_id or v[1] == fingerprint
                      or v[2].work_id == work_id and v[2].effect == "unknown"]
        return candidates[0] if candidates else None

    async def prepare(self, request, selected, fingerprint, unknown):
        self.effects[unknown.operation_id] = (selected.principal.key, fingerprint, unknown)

    async def finish(self, outcome):
        key, fingerprint, _ = self.effects[outcome.operation_id]
        self.effects[outcome.operation_id] = key, fingerprint, outcome


def service():
    async def principal():
        return PRINCIPAL
    return ChatGPTService(principal, Handles(), MemoryGrants(grant()), {"asana": Provider()})
