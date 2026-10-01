import asyncio
import logging
from typing import Any, cast
from uuid import UUID

import httpx

from .contracts import (
    Routing,
    WorkContext,
    WorkPatch,
    WorkPlacement,
)
from .core import (
    AttachmentPage,
    ProviderAttachment,
    ProviderError,
    ProviderRelation,
    ProviderSourceStory,
    ProviderSourceTask,
    ProviderStoriesPage,
    ProviderWork,
    UnknownEffect,
)
from .discovery import ProviderSearchItem, ProviderSearchPage, ProviderStructure

PROJECTS = (
    "1218210259719507",
    "1218431616678499",
    "1218431557603624",
    "1218431557524230",
    "1218431557368054",
    "1218431592956026",
    "1218431584990145",
    "1218431586138793",
)
PROJECT = PROJECTS[0]
REVIEW_INTAKE_PROJECT = "1218915787182921"
REVIEW_INTAKE_SECTION = "1218916346671509"
WORKSPACE = "1200569426771227"
ANCESTRY_GETS = 9
FIELDS = {
    "priority": "1217653169990249", "horizon": "1218212397743203",
    "review_next_action": "1218212397743210", "stage3_gate": "1218212397743217"}
REQUIRED_ROUTING_TRUTH = frozenset(("priority", "review_next_action"))
WORK_TYPE = "1218431623135287"
FINDER_LIMIT = 20
FINDER_MAX_CHILDREN = 100
OPT_FIELDS = ("gid,name,notes,completed,modified_at,"
              "assignee.gid,assignee.name,"
              "memberships.project.gid,memberships.project.name,"
              "memberships.section.gid,memberships.section.name,parent.gid,"
              "custom_fields.gid,custom_fields.display_value,custom_fields.enum_value.gid,"
              "custom_fields.enabled,custom_fields.resource_subtype,custom_fields.text_value,"
              "custom_fields.enum_options.gid,custom_fields.enum_options.name,"
              "custom_fields.enum_options.enabled")
STORY_FIELDS = "gid,resource_subtype,text,created_at,created_by.name,target.gid"
ATTACHMENT_FIELDS = "name,parent.gid"
JSON = dict[str, Any]
LOG = logging.getLogger(__name__)
READ_RETRY_DELAYS = (0.1, 0.25)
READ_RETRY_STATUSES = frozenset((408, 425, 429, *range(500, 600)))
WRITE_TRANSIENT_STATUSES = frozenset((408, 425, 429))


class _TraversalFailure(Exception):
    def __init__(
        self, reason: str, children: tuple[tuple[str, JSON], ...] = (),
    ):
        super().__init__(reason)
        self.reason = reason
        self.children = children



class AsanaProvider:
    def __init__(self, client: httpx.AsyncClient, test_project_gid: str | None = None,
                 *, test_only: bool = False, create_notes_suffix: str | None = None):
        if test_only and not test_project_gid:
            raise ValueError("test-only admission requires a test project GID")
        if test_project_gid and (not all(digit in "0123456789" for digit in test_project_gid)
                                 or test_project_gid in PROJECTS
                                 or test_project_gid == REVIEW_INTAKE_PROJECT):
            raise ValueError("invalid test project GID")
        if create_notes_suffix is not None and (not test_only or not create_notes_suffix.strip()):
            raise ValueError("create notes suffix requires test-only admission")
        self.client = client
        self._test_project = test_project_gid if test_only else None
        self._create_notes_suffix = create_notes_suffix
        self._admission_projects: frozenset[str] = (
            frozenset((test_project_gid,)) if test_only and test_project_gid else
            frozenset((*PROJECTS, test_project_gid)) if test_project_gid else frozenset(PROJECTS)
        )

    async def _read(
        self, operation: str, path: str, *, params: dict[str, str | int],
    ) -> httpx.Response:
        """Perform one idempotent read with bounded, sanitized transient recovery."""
        for attempt in range(len(READ_RETRY_DELAYS) + 1):
            try:
                response = await self.client.get(path, params=params)
            except httpx.RequestError as error:
                if attempt < len(READ_RETRY_DELAYS):
                    LOG.warning(
                        "asana_provider_read_retry operation=%s failure=transport "
                        "error_type=%s attempt=%d",
                        operation, type(error).__name__, attempt + 1,
                    )
                    await asyncio.sleep(READ_RETRY_DELAYS[attempt])
                    continue
                LOG.error(
                    "asana_provider_read_failed operation=%s failure=transport "
                    "error_type=%s attempts=%d",
                    operation, type(error).__name__, attempt + 1,
                )
                raise
            if response.status_code not in READ_RETRY_STATUSES:
                if response.status_code >= 400 and response.status_code != 404:
                    LOG.error(
                        "asana_provider_read_failed operation=%s failure=http_status "
                        "status=%d attempts=%d",
                        operation, response.status_code, attempt + 1,
                    )
                return response
            if attempt < len(READ_RETRY_DELAYS):
                LOG.warning(
                    "asana_provider_read_retry operation=%s failure=http_status "
                    "status=%d attempt=%d",
                    operation, response.status_code, attempt + 1,
                )
                await asyncio.sleep(READ_RETRY_DELAYS[attempt])
                continue
            LOG.error(
                "asana_provider_read_failed operation=%s failure=http_status "
                "status=%d attempts=%d",
                operation, response.status_code, attempt + 1,
            )
            return response
        raise AssertionError("bounded provider read loop exhausted")

    @staticmethod
    def _invalid_read(operation: str, error: Exception) -> None:
        LOG.error(
            "asana_provider_read_failed operation=%s failure=response_invalid "
            "error_type=%s",
            operation, type(error).__name__,
        )

    async def _task(self, gid: str) -> JSON | None:
        try:
            response = await self._read(
                "task", f"/tasks/{gid}", params={"opt_fields": OPT_FIELDS},
            )
            if response.status_code == 404: return None
            response.raise_for_status()
            data = response.json()["data"]
            if not isinstance(data, dict): raise TypeError
            return cast(JSON, data)
        except (KeyError, TypeError, ValueError) as error:
            self._invalid_read("task", error)
            raise ProviderError("provider request failed") from None
        except httpx.HTTPError:
            raise ProviderError("provider request failed") from None

    async def _exact_task(self, gid: str) -> JSON:
        task = await self._task(gid)
        if task is None or self._gid(task) != gid:
            raise _TraversalFailure("not_returned")
        return task

    async def _direct_children(
        self, parent_gid: str, *, require_canonical: bool, offset_limit: int | None = None,
    ) -> tuple[tuple[str, JSON], ...]:
        children: list[tuple[str, JSON]] = []
        seen_gids: set[str] = set()
        seen_offsets: set[str] = set()
        offset: str | None = None

        def fail(reason: str) -> _TraversalFailure:
            return _TraversalFailure(reason, tuple(children))

        while True:
            try:
                params: dict[str, str | int] = {"limit": FINDER_LIMIT, "opt_fields": "gid"}
                if offset is not None:
                    params["offset"] = offset
                response = await self._read(
                    "subtasks", f"/tasks/{parent_gid}/subtasks", params=params,
                )
                response.raise_for_status()
                payload = response.json()
                rows, next_page = payload["data"], payload["next_page"]
                if not isinstance(rows, list):
                    raise fail("invalid_subtask_page")
                raw_rows = cast(list[object], rows)
                if len(raw_rows) > FINDER_LIMIT:
                    raise fail("invalid_subtask_page")
                if len(children) + len(raw_rows) > FINDER_MAX_CHILDREN:
                    raise fail("subtask_cap")
                for row in raw_rows:
                    child_gid = self._gid(row)
                    if child_gid is None:
                        raise fail("invalid_subtask_page")
                    if child_gid in seen_gids:
                        raise fail("duplicate_subtask")
                    seen_gids.add(child_gid)
                    child = await self._task(child_gid)
                    if child is None or self._gid(child) != child_gid:
                        raise fail("candidate_not_returned")
                    if self._parent_gid(child) != parent_gid:
                        raise fail("relationship_changed")
                    if require_canonical and not await self._canonical(child):
                        raise fail("candidate_not_canonical")
                    children.append((child_gid, child))
                if next_page is None:
                    return tuple(children)
                if len(children) >= FINDER_MAX_CHILDREN:
                    raise fail("subtask_cap")
                next_offset = (
                    cast(JSON, next_page).get("offset")
                    if isinstance(next_page, dict) else None
                )
                if (
                    not isinstance(next_offset, str)
                    or not next_offset
                    or next_offset in seen_offsets
                    or offset_limit is not None and len(next_offset) > offset_limit
                ):
                    raise fail("invalid_subtask_offset")
                seen_offsets.add(next_offset)
                offset = next_offset
            except _TraversalFailure:
                raise
            except (ProviderError, httpx.HTTPError, KeyError, TypeError, ValueError):
                raise fail("read_unavailable") from None

    async def _story(self, gid: str) -> JSON | None:
        try:
            response = await self._read(
                "story", f"/stories/{gid}", params={"opt_fields": STORY_FIELDS},
            )
            if response.status_code == 404: return None
            response.raise_for_status()
            data = response.json()["data"]
            if not isinstance(data, dict): raise TypeError
            return cast(JSON, data)
        except (KeyError, TypeError, ValueError) as error:
            self._invalid_read("story", error)
            raise ProviderError("provider request failed") from None
        except httpx.HTTPError:
            raise ProviderError("provider request failed") from None

    @staticmethod
    def _gid(value: object) -> str | None:
        gid = cast(JSON, value).get("gid") if isinstance(value, dict) else None
        return gid if isinstance(gid, str) else None

    async def _canonical(self, task: JSON) -> bool:
        if self._test_project is not None:
            admitted = False
            seen: set[str] = set()
            while True:
                memberships = task.get("memberships")
                if not isinstance(memberships, list):
                    return False
                projects = {
                    gid for item in cast(list[object], memberships)
                    if isinstance(item, dict)
                    and (gid := self._gid(cast(JSON, item).get("project"))) is not None
                }
                if len(projects) != len(cast(list[object], memberships)):
                    return False
                if projects - {self._test_project}:
                    return False
                admitted = admitted or self._test_project in projects
                if (parent := self._gid(task.get("parent"))) is None:
                    return admitted
                if parent in seen or len(seen) >= ANCESTRY_GETS - 1:
                    return False
                seen.add(parent)
                if (ancestor := await self._task(parent)) is None:
                    return False
                task = ancestor
        seen: set[str] = set()
        while True:
            if not isinstance(memberships := task.get("memberships"), list): return False
            if any(self._gid(cast(JSON, item).get("project")) in self._admission_projects
                   for item in cast(list[object], memberships) if isinstance(item, dict)):
                return True
            if (parent := self._gid(task.get("parent"))) is None or parent in seen: return False
            seen.add(parent)
            if len(seen) >= ANCESTRY_GETS: return False
            if (ancestor := await self._task(parent)) is None: return False
            task = ancestor

    @staticmethod
    def _custom_fields(task: JSON) -> list[JSON]:
        fields = task.get("custom_fields")
        return ([cast(JSON, value) for value in cast(list[object], fields) if isinstance(value, dict)]
                if isinstance(fields, list) else [])

    def _work_context(self, task: JSON) -> WorkContext:
        if "assignee" not in task:
            raise TypeError
        raw_assignee = task["assignee"]
        assignee: str | None = None
        if raw_assignee is not None:
            if not isinstance(raw_assignee, dict):
                raise TypeError
            assignee_gid = self._gid(cast(JSON, raw_assignee))
            assignee_name = cast(JSON, raw_assignee).get("name")
            if not assignee_gid or not isinstance(assignee_name, str) or not assignee_name:
                raise TypeError
            assignee = assignee_name

        memberships = task.get("memberships")
        if not isinstance(memberships, list):
            raise TypeError
        admitted: dict[str, tuple[str, str | None, str | None]] = {}
        for raw in cast(list[object], memberships):
            if not isinstance(raw, dict):
                raise TypeError
            membership = cast(JSON, raw)
            project = membership.get("project")
            project_gid = self._gid(project)
            if project_gid is None:
                raise TypeError
            if project_gid not in self._admission_projects:
                continue
            area = cast(JSON, project).get("name") if isinstance(project, dict) else None
            if not isinstance(area, str) or not area:
                raise TypeError
            raw_section = membership.get("section")
            section_gid: str | None = None
            stage: str | None = None
            if raw_section is not None:
                if not isinstance(raw_section, dict):
                    raise TypeError
                section_gid = self._gid(cast(JSON, raw_section))
                stage = cast(JSON, raw_section).get("name")
                if not section_gid or not isinstance(stage, str) or not stage:
                    raise TypeError
            value = (area, section_gid, stage)
            if project_gid in admitted and admitted[project_gid] != value:
                raise TypeError
            admitted[project_gid] = value
        placements = tuple(sorted(
            (WorkPlacement(area=area, stage=stage) for area, _, stage in admitted.values()),
            key=lambda placement: (placement.area, placement.stage or ""),
        ))
        return WorkContext(assignee=assignee, placements=placements)

    def _structure_item(self, gid: str, task: JSON) -> ProviderSearchItem:
        if self._gid(task) != gid:
            raise TypeError
        title, completed, revision = (
            task.get("name"), task.get("completed"), task.get("modified_at")
        )
        if (not isinstance(title, str) or not isinstance(completed, bool)
                or not isinstance(revision, str)):
            raise TypeError
        return ProviderSearchItem(
            provider_work_id=gid, title=title, completed=completed,
            revision=revision, routing=self._search_routing(task),
            context=self._work_context(task),
        )

    def _parent_gid(self, task: JSON) -> str | None:
        if "parent" not in task:
            raise TypeError
        parent = task["parent"]
        if parent is None:
            return None
        gid = self._gid(parent)
        if not gid:
            raise TypeError
        return gid

    @staticmethod
    def _story_value(payload: JSON, fallback_task_gid: str | None = None) -> ProviderSourceStory:
        story_gid = payload.get("gid")
        target_gid = (AsanaProvider._gid(payload["target"])
                      if "target" in payload else fallback_task_gid)
        created_at = payload.get("created_at")
        subtype, text = payload.get("resource_subtype"), payload.get("text")
        creator = payload.get("created_by")
        created_by = cast(JSON, creator).get("name") if isinstance(creator, dict) else None
        if (not isinstance(story_gid, str) or not story_gid
                or not isinstance(target_gid, str) or not target_gid
                or not isinstance(created_at, str)
                or subtype is not None and not isinstance(subtype, str)
                or text is not None and not isinstance(text, str)
                or created_by is not None and not isinstance(created_by, str)):
            raise ProviderError("provider response invalid")
        return ProviderSourceStory(story_gid, target_gid, subtype, text, created_at, created_by)

    async def get(self, provider_work_id: str) -> ProviderWork | None:
        task = await self._task(provider_work_id)
        if task is None: return None
        if self._gid(task) != provider_work_id:
            raise ProviderError("provider response identity mismatch")
        fields = self._custom_fields(task)
        values = {name: next((f.get("display_value") for f in fields
                  if f.get("gid") == gid), None) for name, gid in FIELDS.items()}
        try:
            for name, gid in (("priority", FIELDS["priority"]), ("work_type", WORK_TYPE),
                              ("review_next_action", FIELDS["review_next_action"])):
                strict = [field for field in fields if field.get("gid") == gid]
                required_truth = name in REQUIRED_ROUTING_TRUTH
                if len(strict) > 1 and required_truth: raise TypeError
                if len(strict) != 1:
                    values[name] = None
                    continue
                enum = strict[0]
                options = enum.get("enum_options")
                selected, display = enum.get("enum_value"), enum.get("display_value")
                current = self._gid(selected)
                if (enum.get("enabled") is not True or enum.get("resource_subtype") != "enum"
                        or not isinstance(options, list)
                        or any(not isinstance(option, dict)
                               or not isinstance(cast(JSON, option).get("gid"), str)
                               or not cast(JSON, option)["gid"]
                               or not isinstance(cast(JSON, option).get("name"), str)
                               or not isinstance(cast(JSON, option).get("enabled"), bool)
                               for option in cast(list[object], options))):
                    if required_truth: raise TypeError
                    values[name] = None
                    continue
                if selected is None and display is None:
                    values[name] = None
                    continue
                identities = [cast(JSON, option) for option in cast(list[object], options)
                           if self._gid(option) == current
                           and cast(JSON, option).get("enabled") is True]
                matches = [option for option in identities if option.get("name") == display]
                if (not isinstance(selected, dict) or not current
                        or not isinstance(display, str)
                        or len(identities) != 1 or len(matches) != 1):
                    if required_truth: raise TypeError
                    values[name] = None
                else: values[name] = display
            title, notes, completed, revision = (task[key] for key in ("name", "notes", "completed", "modified_at"))
            if not all(isinstance(value, str) for value in (title, notes, revision)) or not isinstance(completed, bool): raise TypeError
            routing = Routing(**{key: value if isinstance(value, str) else None
                               for key, value in values.items()})
            return ProviderWork(
                title, notes, completed, revision, routing, self._work_context(task),
                await self._canonical(task),
            )
        except (KeyError, TypeError, ValueError) as error:
            self._invalid_read("work", error)
            raise ProviderError("provider response invalid") from None

    async def list_attachments(
        self, provider_work_id: str, cursor: str | None, limit: int
    ) -> AttachmentPage:
        try:
            params: dict[str, str | int] = {
                "parent": provider_work_id, "limit": limit, "opt_fields": ATTACHMENT_FIELDS,
            }
            if cursor is not None:
                params["offset"] = cursor
            response = await self._read("attachments", "/attachments", params=params)
            response.raise_for_status()
            payload = response.json()
            data, next_page = payload["data"], payload["next_page"]
            if not isinstance(data, list):
                raise TypeError
            raw_attachments = cast(list[object], data)
            if len(raw_attachments) > limit:
                raise TypeError
            attachments: list[ProviderAttachment] = []
            for value in raw_attachments:
                if not isinstance(value, dict):
                    raise TypeError
                row = cast(JSON, value)
                name = row.get("name")
                if (not isinstance(name, str) or not name
                        or self._gid(row.get("parent")) != provider_work_id):
                    raise TypeError
                attachments.append(ProviderAttachment(name))
            next_cursor: str | None = None
            if next_page is not None:
                if not isinstance(next_page, dict):
                    raise TypeError
                next_cursor = cast(JSON, next_page).get("offset")
                if (not isinstance(next_cursor, str) or not next_cursor
                        or len(next_cursor) > 1024):
                    raise TypeError
            return AttachmentPage(tuple(attachments), next_cursor)
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise ProviderError("provider request failed") from None

    async def structure_work(
        self, provider_work_id: str, observed_revision: str,
    ) -> ProviderStructure:
        """Return one verified immediate-family snapshot or fail without partial data."""
        try:
            try:
                target = await self._exact_task(provider_work_id)
            except _TraversalFailure:
                raise ProviderError("provider structure unavailable") from None
            if not await self._canonical(target):
                raise ProviderError("provider structure unavailable")
            revision = target.get("modified_at")
            if not isinstance(revision, str):
                raise TypeError
            if revision != observed_revision:
                return ProviderStructure(status="stale", revision=revision)
            parent_gid = self._parent_gid(target)

            parent: ProviderSearchItem | None = None
            if parent_gid is not None:
                try:
                    parent_task = await self._exact_task(parent_gid)
                except _TraversalFailure:
                    raise ProviderError("provider structure unavailable") from None
                if not await self._canonical(parent_task):
                    raise ProviderError("provider structure unavailable")
                parent = self._structure_item(parent_gid, parent_task)

            try:
                children = await self._direct_children(
                    provider_work_id, require_canonical=True, offset_limit=1024
                )
            except _TraversalFailure:
                raise ProviderError("provider structure unavailable") from None
            child_items = tuple(
                self._structure_item(child_gid, child)
                for child_gid, child in children
            )

            try:
                readback = await self._exact_task(provider_work_id)
            except _TraversalFailure:
                raise ProviderError("provider structure unavailable") from None
            if not await self._canonical(readback):
                raise ProviderError("provider structure unavailable")
            if (
                readback.get("modified_at") != revision
                or self._parent_gid(readback) != parent_gid
            ):
                raise ProviderError("provider structure unavailable")
            return ProviderStructure(
                status="ok", revision=revision, parent=parent,
                children=child_items,
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise ProviderError("provider structure unavailable") from None

    async def source_task(self, provider_task_id: str) -> ProviderSourceTask | None:
        task = await self._task(provider_task_id)
        if task is None: return None
        try:
            if self._gid(task) != provider_task_id: raise TypeError
            title, notes, completed, revision = (
                task[key] for key in ("name", "notes", "completed", "modified_at")
            )
            if (not isinstance(title, str) or not isinstance(notes, str)
                    or not isinstance(completed, bool) or not isinstance(revision, str)):
                raise TypeError
            return ProviderSourceTask(
                title, notes, completed, revision, self._work_context(task),
                await self._canonical(task),
            )
        except (KeyError, TypeError, ValueError):
            raise ProviderError("provider response invalid") from None

    async def source_stories(
        self, provider_task_id: str, observed_revision: str,
        offset: str | None, limit: int,
    ) -> ProviderStoriesPage | None:
        before = await self.source_task(provider_task_id)
        if before is None: return None
        if not before.canonical:
            return ProviderStoriesPage(
                provider_task_id, before.revision, (), None, False
            )
        if before.revision != observed_revision:
            return ProviderStoriesPage(
                provider_task_id, before.revision, (), None, True, True
            )
        try:
            params: dict[str, str | int] = {"opt_fields": STORY_FIELDS, "limit": limit}
            if offset is not None: params["offset"] = offset
            response = await self._read(
                "stories", f"/tasks/{provider_task_id}/stories", params=params,
            )
            response.raise_for_status()
            payload = response.json()
            data = payload["data"]
            if not isinstance(data, list): raise TypeError
            raw_stories = cast(list[object], data)
            if len(raw_stories) > limit: raise TypeError
            stories = tuple(
                self._story_value(cast(JSON, value), provider_task_id)
                for value in raw_stories if isinstance(value, dict)
            )
            if len(stories) != len(raw_stories): raise TypeError
            if any(story.task_gid != provider_task_id for story in stories): raise TypeError
            next_page = payload["next_page"]
            next_offset: str | None = None
            if next_page is not None:
                if not isinstance(next_page, dict): raise TypeError
                value = cast(JSON, next_page).get("offset")
                if not isinstance(value, str) or not value or value == offset: raise TypeError
                next_offset = value
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise ProviderError("provider request failed") from None
        after = await self.source_task(provider_task_id)
        if after is None: return None
        if not after.canonical:
            return ProviderStoriesPage(
                provider_task_id, after.revision, (), None, False
            )
        if after.revision != before.revision:
            return ProviderStoriesPage(
                provider_task_id, after.revision, (), None, True, True
            )
        return ProviderStoriesPage(
            provider_task_id, after.revision, stories, next_offset, True
        )

    async def source_story(
        self, provider_task_id: str, provider_story_id: str
    ) -> ProviderSourceStory | None:
        story = await self._story(provider_story_id)
        if story is None: return None
        if self._gid(story) != provider_story_id:
            raise ProviderError("provider response invalid")
        return self._story_value(story)

    async def _write(
        self, method: str, path: str, data: JSON, *, unknown_on_server_error: bool = False
    ) -> httpx.Response:
        try:
            response = await self.client.request(method, path, json={"data": data})
        except httpx.RequestError:
            raise UnknownEffect("provider effect unknown") from None
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            if unknown_on_server_error and response.status_code >= 500:
                raise UnknownEffect("provider effect unknown") from None
            if response.status_code in WRITE_TRANSIENT_STATUSES:
                failure = "transient"
            elif response.status_code in {401, 403}:
                failure = "authority_denied"
            else:
                failure = "permanent"
            raise ProviderError("provider write failed", failure=failure) from None
        return response

    def recovery_identity(self) -> str:
        """Base Asana create relies on exact response/readback; UNKNOWN is manual-reconcile only."""
        return f"asana-exact-response-v1:{WORKSPACE}"

    async def create_work(
        self, title: str, notes: str, operation_id: UUID, *,
        parent_task_gid: str | None = None, project_gid: str | None = None,
    ) -> str:
        del operation_id
        if (parent_task_gid is None) == (project_gid is None):
            raise ProviderError("create target invalid", failure="invalid_request")
        if self._create_notes_suffix is not None:
            notes = f"{notes.rstrip()}\n\n{self._create_notes_suffix}"
        data: JSON = {"workspace": WORKSPACE, "name": title, "notes": notes}
        if parent_task_gid is not None:
            parent = await self._task(parent_task_gid)
            if parent is None or not await self._canonical(parent):
                raise ProviderError("create parent denied", failure="admission_denied")
            data["parent"] = parent_task_gid
        else:
            assert project_gid is not None
            if project_gid not in self._admission_projects:
                raise ProviderError("create project denied", failure="admission_denied")
            data["projects"] = [project_gid]
        response = await self._write("POST", "/tasks", data, unknown_on_server_error=True)
        try:
            payload = response.json()["data"]
            task_gid = self._gid(payload)
            if task_gid is None:
                raise UnknownEffect("created task response unknown")
            task = await self._task(task_gid)
            if task is None or self._gid(task) != task_gid or not await self._canonical(task):
                raise UnknownEffect("created task readback unknown")
            if task.get("name") != title or task.get("notes") != notes:
                raise UnknownEffect("created task readback mismatch")
            if parent_task_gid is not None:
                if self._parent_gid(task) != parent_task_gid:
                    raise UnknownEffect("created task parent mismatch")
            else:
                memberships = task.get("memberships")
                if not isinstance(memberships, list) or not any(
                    isinstance(raw, dict) and self._gid(cast(JSON, raw).get("project")) == project_gid
                    for raw in cast(list[object], memberships)
                ):
                    raise UnknownEffect("created task project mismatch")
            return task_gid
        except UnknownEffect:
            raise
        except (ProviderError, KeyError, TypeError, ValueError):
            raise UnknownEffect("created task readback unknown") from None

    async def recover_created(
        self, parent_task_gid: str | None, operation_id: UUID, *,
        project_gid: str | None = None,
    ) -> str | None:
        """No heuristic create search: an ambiguous base-Asana create remains durable UNKNOWN."""
        del parent_task_gid, project_gid, operation_id
        return None
    async def _dependency_gids(self, provider_work_id: str) -> frozenset[str]:
        try:
            gids: set[str] = set()
            offset: str | None = None
            while True:
                params: dict[str, str | int] = {"limit": 100, "opt_fields": "gid"}
                if offset is not None:
                    params["offset"] = offset
                response = await self._read(
                    "dependencies", f"/tasks/{provider_work_id}/dependencies", params=params,
                )
                response.raise_for_status()
                payload = response.json()
                rows = payload["data"]
                if not isinstance(rows, list):
                    raise TypeError
                raw_rows = cast(list[object], rows)
                if len(raw_rows) > 100:
                    raise TypeError
                for raw in raw_rows:
                    gid = self._gid(raw)
                    if gid is None or gid in gids:
                        raise TypeError
                    gids.add(gid)
                next_page = payload.get("next_page")
                if next_page is None:
                    return frozenset(gids)
                if not isinstance(next_page, dict):
                    raise TypeError
                next_offset = cast(JSON, next_page).get("offset")
                if (
                    not isinstance(next_offset, str)
                    or not next_offset
                    or next_offset == offset
                ):
                    raise TypeError
                offset = next_offset
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise ProviderError("dependency read failed") from None

    async def dependencies_for_import(self, provider_work_id: str) -> frozenset[str]:
        """Read exact dependency identities for the bounded offline migration."""
        return await self._dependency_gids(provider_work_id)

    def _placement_memberships(self, task: JSON) -> dict[str, str | None]:
        memberships = task.get("memberships")
        if not isinstance(memberships, list):
            raise ProviderError("placement read failed")
        result: dict[str, str | None] = {}
        for raw in cast(list[object], memberships):
            if not isinstance(raw, dict):
                raise ProviderError("placement read failed")
            membership = cast(JSON, raw)
            project_gid = self._gid(membership.get("project"))
            section = membership.get("section")
            section_gid = None if section is None else self._gid(section)
            if project_gid is None or section is not None and section_gid is None:
                raise ProviderError("placement read failed")
            if project_gid in result:
                raise ProviderError("placement read failed")
            result[project_gid] = section_gid
        return result

    async def relation_matches(self, provider_work_id: str, patch: ProviderRelation) -> bool:
        task = await self._task(provider_work_id)
        if task is None or not await self._canonical(task):
            raise ProviderError("relation read denied")
        if patch.kind == "assignee":
            assignee = task.get("assignee")
            actual = None if assignee is None else self._gid(assignee)
            return actual == patch.assignee_gid
        if patch.kind == "placement":
            memberships = self._placement_memberships(task)
            if patch.action == "remove":
                return patch.project_gid not in memberships
            target_matches = (
                patch.project_gid in memberships
                and memberships[patch.project_gid] == patch.section_gid
            )
            if patch.action == "add":
                return target_matches
            return target_matches and not any(
                project_gid != patch.project_gid
                and project_gid in self._admission_projects
                for project_gid in memberships
            )
        if patch.kind == "parent":
            return self._parent_gid(task) == patch.target_gid
        dependencies = await self._dependency_gids(provider_work_id)
        assert patch.target_gid is not None
        return (patch.target_gid in dependencies) == (patch.action == "add")

    async def update_relation(self, provider_work_id: str, patch: ProviderRelation) -> None:
        if patch.kind == "assignee":
            await self._write(
                "PUT", f"/tasks/{provider_work_id}", {"assignee": patch.assignee_gid},
                unknown_on_server_error=True,
            )
            return
        if patch.kind == "placement":
            assert patch.project_gid is not None
            if patch.project_gid == REVIEW_INTAKE_PROJECT:
                if (self._test_project is not None or patch.action != "add"
                        or patch.section_gid != REVIEW_INTAKE_SECTION):
                    raise ProviderError("placement project denied", failure="admission_denied")
                await self._write(
                    "POST", f"/tasks/{provider_work_id}/addProject",
                    {"project": patch.project_gid, "section": patch.section_gid},
                    unknown_on_server_error=True,
                )
                return
            if patch.project_gid not in self._admission_projects:
                raise ProviderError("placement project denied", failure="admission_denied")
            if self._test_project is not None and patch.action == "remove":
                raise ProviderError(
                    "test-only placement removal denied", failure="admission_denied"
                )
            if patch.action == "remove":
                await self._write(
                    "POST", f"/tasks/{provider_work_id}/removeProject",
                    {"project": patch.project_gid}, unknown_on_server_error=True,
                )
                return
            if patch.action == "add":
                data = {"project": patch.project_gid}
                if patch.section_gid is not None:
                    data["section"] = patch.section_gid
                await self._write(
                    "POST", f"/tasks/{provider_work_id}/addProject", data,
                    unknown_on_server_error=True,
                )
                return
            await self._move_placement(provider_work_id, patch)
            return
        if patch.kind == "parent":
            if self._create_notes_suffix is not None and patch.target_gid is None:
                raise ProviderError(
                    "test-only parent removal denied", failure="admission_denied"
                )
            if patch.target_gid is not None:
                parent = await self._task(patch.target_gid)
                if parent is None or not await self._canonical(parent):
                    raise ProviderError("parent target denied", failure="admission_denied")
            await self._write(
                "POST", f"/tasks/{provider_work_id}/setParent", {"parent": patch.target_gid},
                unknown_on_server_error=True,
            )
            return
        assert patch.target_gid is not None
        target = await self._task(patch.target_gid)
        if target is None or not await self._canonical(target):
            raise ProviderError("dependency target denied", failure="admission_denied")
        path = "addDependencies" if patch.action == "add" else "removeDependencies"
        await self._write(
            "POST", f"/tasks/{provider_work_id}/{path}",
            {"dependencies": [patch.target_gid]}, unknown_on_server_error=True,
        )

    async def _move_placement(
        self, provider_work_id: str, patch: ProviderRelation,
    ) -> None:
        """Converge a placement move while preserving non-admitted memberships."""
        assert patch.project_gid is not None
        sent = False
        try:
            task = await self._task(provider_work_id)
            if task is None or not await self._canonical(task):
                raise ProviderError("relation read denied")
            memberships = self._placement_memberships(task)
            if (
                patch.project_gid not in memberships
                or memberships[patch.project_gid] != patch.section_gid
            ):
                data = {"project": patch.project_gid}
                if patch.section_gid is not None:
                    data["section"] = patch.section_gid
                sent = True
                await self._write(
                    "POST", f"/tasks/{provider_work_id}/addProject", data,
                    unknown_on_server_error=True,
                )
                task = await self._task(provider_work_id)
                if task is None:
                    raise ProviderError("placement read failed")
                memberships = self._placement_memberships(task)
                if (
                    patch.project_gid not in memberships
                    or memberships[patch.project_gid] != patch.section_gid
                ):
                    raise ProviderError("placement target unconfirmed")
            superseded = sorted(
                project_gid for project_gid in memberships
                if project_gid != patch.project_gid
                and project_gid in self._admission_projects
            )
            for project_gid in superseded:
                sent = True
                await self._write(
                    "POST", f"/tasks/{provider_work_id}/removeProject",
                    {"project": project_gid}, unknown_on_server_error=True,
                )
        except UnknownEffect:
            raise
        except ProviderError:
            if sent:
                raise UnknownEffect("placement move effect unknown") from None
            raise
    async def update(self, provider_work_id: str, patch: WorkPatch) -> None:
        changed = patch.model_fields_set
        data = {("name" if name == "title" else name): getattr(patch, name)
                for name in {"title", "notes", "completed"} & changed}
        if self._create_notes_suffix is not None and "notes" in data:
            notes = cast(str, data["notes"])
            if self._create_notes_suffix not in notes.splitlines():
                data["notes"] = f"{notes.rstrip()}\n\n{self._create_notes_suffix}"
        routing = (set(FIELDS) | {"work_type"}) & changed
        if routing:
            task = await self._task(provider_work_id)
            raw_fields = None if task is None else task.get("custom_fields")
            if task is None:
                raise ProviderError("routing write denied", failure="admission_denied")
            if not isinstance(raw_fields, list):
                raise ProviderError("routing write denied", failure="permanent")
            fields = self._custom_fields(task)
            if (routing & {"priority", "work_type", "review_next_action"}
                    and len(fields) != len(cast(list[object], raw_fields))):
                raise ProviderError("routing write denied", failure="permanent")
            custom: dict[str, str] = {}
            for name in routing:
                gid = WORK_TYPE if name == "work_type" else FIELDS[name]
                matches = [field for field in fields if field.get("gid") == gid]
                enabled = (len(matches) == 1 and matches[0].get("enabled") is True
                           and (name not in {"priority", "work_type", "review_next_action"}
                                or matches[0].get("resource_subtype") == "enum"))
                raw_options: object = matches[0].get("enum_options", []) if enabled else []
                options = self._custom_fields({"custom_fields": raw_options})
                valid = (name not in {"priority", "work_type", "review_next_action"} or isinstance(raw_options, list)
                         and len(options) == len(cast(list[object], raw_options))
                         and all(isinstance(option.get("name"), str)
                                 and isinstance(option.get("enabled"), bool)
                                 and isinstance(option.get("gid"), str) and option["gid"]
                                 for option in options))
                choices = ([option for option in options
                            if option.get("name") == getattr(patch, name)
                            and option.get("enabled") is True] if valid else [])
                if (len(choices) != 1 or not isinstance(choices[0].get("gid"), str)
                        or name in {"priority", "work_type", "review_next_action"} and not choices[0]["gid"]):
                    failure = "invalid_request" if valid and not choices else "permanent"
                    raise ProviderError("routing write denied", failure=failure)
                custom[gid] = choices[0]["gid"]
            data["custom_fields"] = custom
        await self._write(
            "PUT", f"/tasks/{provider_work_id}", data,
            unknown_on_server_error=True,
        )

    async def append(self, provider_work_id: str, text: str) -> str | None:
        response = await self._write(
            "POST", f"/tasks/{provider_work_id}/stories", {"text": text},
            unknown_on_server_error=True,
        )
        try:
            data = response.json()["data"]
            story_gid = cast(JSON, data).get("gid") if isinstance(data, dict) else None
            return story_gid if isinstance(story_gid, str) else None
        except (KeyError, TypeError, ValueError):
            return None

    def _search_routing(self, task: JSON) -> Routing:
        raw_fields = task.get("custom_fields")
        if not isinstance(raw_fields, list) or any(
            not isinstance(field, dict) for field in cast(list[object], raw_fields)
        ):
            raise TypeError
        fields = [cast(JSON, field) for field in cast(list[object], raw_fields)]
        values: dict[str, str | None] = {}
        for name, field_gid in FIELDS.items():
            matches = [field for field in fields if field.get("gid") == field_gid]
            if len(matches) > 1:
                raise TypeError
            if not matches:
                values[name] = None
                continue
            field = matches[0]
            if field.get("enabled") is False and name not in REQUIRED_ROUTING_TRUTH:
                values[name] = None
                continue
            options = field.get("enum_options")
            value = field.get("display_value")
            current = field.get("enum_value")
            if (
                field.get("enabled") is not True
                or field.get("resource_subtype") != "enum"
                or not isinstance(options, list)
                or any(not isinstance(option, dict) for option in cast(list[object], options))
            ):
                raise TypeError
            if value is None:
                if current is not None:
                    raise TypeError
                values[name] = None
                continue
            current_gid = self._gid(current)
            parsed_options = [
                cast(JSON, option) for option in cast(list[object], options)
            ]
            valid = [
                option for option in parsed_options if self._gid(option) == current_gid
            ]
            if (
                not isinstance(value, str)
                or not value
                or current_gid is None
                or len(valid) != 1
                or valid[0].get("enabled") is not True
                or valid[0].get("name") != value
            ):
                raise TypeError
            values[name] = value
        return Routing(**values)

    def _search_owner_project(self, task: JSON) -> str:
        memberships = task.get("memberships")
        if not isinstance(memberships, list):
            raise TypeError
        admitted: set[str] = set()
        for raw in cast(list[object], memberships):
            if not isinstance(raw, dict):
                raise TypeError
            project_gid = self._gid(cast(JSON, raw).get("project"))
            if project_gid is None:
                raise TypeError
            if project_gid in self._admission_projects:
                admitted.add(project_gid)
        if not admitted:
            raise TypeError
        return min(admitted)

    @staticmethod
    def _search_position(cursor: str | None, project_count: int) -> tuple[int, str | None]:
        if cursor is None:
            return 0, None
        index_text, separator, offset = cursor.partition(":")
        if separator != ":" or not index_text.isdigit():
            raise TypeError
        index = int(index_text)
        if index >= project_count:
            raise TypeError
        return index, offset or None

    @staticmethod
    def _search_cursor(index: int, offset: str | None) -> str:
        value = f"{index}:{offset or ''}"
        if len(value) > 1024:
            raise TypeError
        return value

    async def search_work(
        self, text: str | None, completed: bool | None, cursor: str | None, limit: int,
    ) -> ProviderSearchPage:
        try:
            if not 1 <= limit <= 100:
                raise ProviderError("provider request invalid")
            if cursor is not None and (not cursor or len(cursor) > 1024):
                raise ProviderError("provider request invalid")
            if text is not None and (not text or len(text) > 500):
                raise ProviderError("provider request invalid")

            next_cursor: str | None = None
            projects: tuple[str, ...] = ()
            project: str | None = None
            index = 0
            offset: str | None = None

            if text is not None:
                if cursor is not None:
                    raise ProviderError("text search is bounded and non-continuable")
                params: dict[str, str | int] = {
                    "projects.any": ",".join(sorted(self._admission_projects)),
                    "limit": limit,
                    "opt_fields": "gid",
                    "text": text,
                }
                if completed is not None:
                    params["completed"] = "true" if completed else "false"
                response = await self._read(
                    "task_search", f"/workspaces/{WORKSPACE}/tasks/search", params=params,
                )
            else:
                projects = tuple(sorted(self._admission_projects))
                index, offset = self._search_position(cursor, len(projects))
                project = projects[index]
                params = {
                    "project": project,
                    "completed_since": "1970-01-01T00:00:00Z",
                    "limit": limit,
                    "opt_fields": "gid",
                }
                if offset is not None:
                    params["offset"] = offset
                response = await self._read("tasks", "/tasks", params=params)

            response.raise_for_status()
            payload = response.json()
            raw_rows = payload["data"]
            if not isinstance(raw_rows, list):
                raise TypeError
            rows = cast(list[object], raw_rows)
            if len(rows) > limit:
                raise TypeError
            if text is not None and payload.get("next_page") is not None:
                raise ProviderError("text search continuation unsupported")

            if text is None:
                next_page = payload.get("next_page")
                if next_page is not None:
                    next_offset = (
                        cast(JSON, next_page).get("offset")
                        if isinstance(next_page, dict)
                        else None
                    )
                    if (
                        not isinstance(next_offset, str)
                        or not next_offset
                        or next_offset == offset
                    ):
                        raise TypeError
                    next_cursor = self._search_cursor(index, next_offset)
                elif index + 1 < len(projects):
                    next_cursor = self._search_cursor(index + 1, None)
                else:
                    next_cursor = None

            seen: set[str] = set()
            items: list[ProviderSearchItem] = []
            for raw in rows:
                gid = self._gid(raw)
                if gid is None or not gid or gid in seen:
                    raise TypeError
                seen.add(gid)
                task = await self._task(gid)
                if task is None or self._gid(task) != gid or not await self._canonical(task):
                    raise ProviderError("provider search readback failed")
                if text is None:
                    if project is None:
                        raise TypeError
                    if self._search_owner_project(task) != project:
                        continue
                title = task.get("name")
                current_completed = task.get("completed")
                revision = task.get("modified_at")
                if (
                    not isinstance(title, str)
                    or not isinstance(current_completed, bool)
                    or not isinstance(revision, str)
                ):
                    raise TypeError
                if completed is not None and current_completed != completed:
                    continue
                items.append(ProviderSearchItem(
                    provider_work_id=gid,
                    title=title,
                    completed=current_completed,
                    revision=revision,
                    routing=self._search_routing(task),
                    context=self._work_context(task),
                ))
            return ProviderSearchPage(tuple(items), next_cursor)
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise ProviderError("provider request failed") from None
