from typing import Any, cast

import httpx

from .contracts import (
    GroupedCandidate,
    GroupedLookup,
    RelatedCandidate,
    RelatedLookup,
    Routing,
    WorkPatch,
)
from .core import (
    ProviderError,
    ProviderHead,
    ProviderSourceStory,
    ProviderSourceTask,
    ProviderStoriesPage,
    ProviderWork,
    UnknownEffect,
)
from .discovery import ProviderSearchItem, ProviderSearchPage

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
WORKSPACE = "1200569426771227"
ROOT_WORK_GID = "1218524557926403"
ANCESTRY_GETS = 9
WORK_TYPE = "1218431623135287"
FIELDS = {
    "priority": "1217653169990249", "work_type": WORK_TYPE,
    "horizon": "1218212397743203", "review_next_action": "1218212397743210",
    "stage3_gate": "1218212397743217"}
FINDER_LIMIT = 20
FINDER_MAX_CHILDREN = 100
OPT_FIELDS = ("gid,name,notes,completed,modified_at,"
              "memberships.project.gid,parent.gid,"
              "custom_fields.gid,custom_fields.display_value,custom_fields.enum_value.gid,"
              "custom_fields.enabled,custom_fields.resource_subtype,custom_fields.text_value,"
              "custom_fields.enum_options.gid,custom_fields.enum_options.name,"
              "custom_fields.enum_options.enabled")
STORY_FIELDS = "gid,resource_subtype,text,created_at,created_by.name,target.gid"
JSON = dict[str, Any]
PRIORITIES = {f"P{value}": value for value in range(4)}


class AsanaProvider:
    def __init__(self, client: httpx.AsyncClient, test_project_gid: str | None = None,
                 *, test_only: bool = False):
        if test_only and not test_project_gid:
            raise ValueError("test-only admission requires a test project GID")
        if test_project_gid and (not all(digit in "0123456789" for digit in test_project_gid)
                                 or test_project_gid in PROJECTS):
            raise ValueError("invalid test project GID")
        self.client = client
        self._admission_projects: frozenset[str] = (
            frozenset((test_project_gid,)) if test_only and test_project_gid else
            frozenset((*PROJECTS, test_project_gid)) if test_project_gid else frozenset(PROJECTS)
        )

    async def _task(self, gid: str) -> JSON | None:
        try:
            response = await self.client.get(f"/tasks/{gid}", params={"opt_fields": OPT_FIELDS})
            if response.status_code == 404: return None
            response.raise_for_status()
            data = response.json()["data"]
            if not isinstance(data, dict): raise TypeError
            return cast(JSON, data)
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise ProviderError("provider request failed") from None

    async def _story(self, gid: str) -> JSON | None:
        try:
            response = await self.client.get(
                f"/stories/{gid}", params={"opt_fields": STORY_FIELDS}
            )
            if response.status_code == 404: return None
            response.raise_for_status()
            data = response.json()["data"]
            if not isinstance(data, dict): raise TypeError
            return cast(JSON, data)
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise ProviderError("provider request failed") from None

    @staticmethod
    def _gid(value: object) -> str | None:
        gid = cast(JSON, value).get("gid") if isinstance(value, dict) else None
        return gid if isinstance(gid, str) else None

    async def _canonical(self, task: JSON) -> bool:
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

    def _related_candidate(self, gid: str, task: JSON, parent_gid: str) -> RelatedCandidate:
        title, revision = task.get("name"), task.get("modified_at")
        if not isinstance(title, str) or not isinstance(revision, str):
            raise TypeError
        role_fields = [field for field in self._custom_fields(task)
                       if field.get("gid") == WORK_TYPE]
        option: str | None = None
        if len(role_fields) == 1:
            field = role_fields[0]
            value = self._gid(field.get("enum_value"))
            options = field.get("enum_options")
            if (field.get("enabled") is True
                    and field.get("resource_subtype") == "enum"
                    and isinstance(options, list)):
                valid_options = [cast(JSON, item) for item in cast(list[object], options)
                                 if isinstance(item, dict)]
                if any(self._gid(item) == value and item.get("enabled") is True
                       for item in valid_options):
                    option = value
        return RelatedCandidate(task_gid=gid, title=title, revision=revision,
                                parent_gid=parent_gid, work_type_option_gid=option)

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
        fields = self._custom_fields(task)
        values = {name: next((f.get("display_value") for f in fields
                  if f.get("gid") == gid), None) for name, gid in FIELDS.items()}
        try:
            title, notes, completed, revision = (task[key] for key in ("name", "notes", "completed", "modified_at"))
            if not all(isinstance(value, str) for value in (title, notes, revision)) or not isinstance(completed, bool): raise TypeError
            routing = Routing(**{key: value if isinstance(value, str) else None
                               for key, value in values.items()})
            return ProviderWork(title, notes, completed, revision, routing, await self._canonical(task))
        except (KeyError, TypeError, ValueError):
            raise ProviderError("provider response invalid") from None

    async def find_related(self, work_task_gid: str) -> RelatedLookup:
        """Read bounded direct-child evidence; this does not decide canonical roles."""
        candidates: list[RelatedCandidate] = []
        observed_revision: str | None = None

        def uncertain(reason: str) -> RelatedLookup:
            return RelatedLookup(status="UH_OH", work_task_gid=work_task_gid,
                                 observed_revision=observed_revision,
                                 candidates=() if reason == "work_not_canonical" else tuple(candidates),
                                 reason=reason)

        try:
            work = await self._task(work_task_gid)
            if work is None or self._gid(work) != work_task_gid:
                return uncertain("work_not_returned")
            revision = work.get("modified_at")
            if not isinstance(revision, str):
                return uncertain("work_revision_unavailable")
            observed_revision = revision
            if not await self._canonical(work):
                return uncertain("work_not_canonical")

            offset: str | None = None
            seen_offsets: set[str] = set()
            seen_child_gids: set[str] = set()
            incomplete_reason: str | None = None
            while True:
                params: dict[str, str | int] = {"limit": FINDER_LIMIT, "opt_fields": "gid"}
                if offset is not None:
                    params["offset"] = offset
                response = await self.client.get(f"/tasks/{work_task_gid}/subtasks", params=params)
                response.raise_for_status()
                payload = response.json()
                rows, next_page = payload["data"], payload["next_page"]
                if not isinstance(rows, list):
                    return uncertain("invalid_subtask_page")
                raw_rows = cast(list[object], rows)
                if len(raw_rows) > FINDER_LIMIT:
                    return uncertain("invalid_subtask_page")
                if len(candidates) + len(raw_rows) > FINDER_MAX_CHILDREN:
                    incomplete_reason = "subtask_cap"
                    break
                for row in raw_rows:
                    child_gid = self._gid(row)
                    if child_gid is None:
                        return uncertain("invalid_subtask_page")
                    child = await self._task(child_gid)
                    if child is None or self._gid(child) != child_gid:
                        return uncertain("candidate_not_returned")
                    if self._gid(child.get("parent")) != work_task_gid:
                        return uncertain("relationship_changed")
                    if child_gid in seen_child_gids:
                        return uncertain("duplicate_subtask")
                    seen_child_gids.add(child_gid)
                    candidates.append(self._related_candidate(child_gid, child, work_task_gid))
                if next_page is None:
                    break
                next_offset = cast(JSON, next_page).get("offset") if isinstance(next_page, dict) else None
                if (not isinstance(next_offset, str) or not next_offset
                        or next_offset in seen_offsets):
                    incomplete_reason = "invalid_subtask_offset"
                    break
                if len(candidates) >= FINDER_MAX_CHILDREN:
                    incomplete_reason = "subtask_cap"
                    break
                seen_offsets.add(next_offset)
                offset = next_offset

            readback = await self._task(work_task_gid)
            if readback is None or self._gid(readback) != work_task_gid:
                return uncertain("work_readback_unavailable")
            if not await self._canonical(readback):
                return uncertain("work_not_canonical")
            if readback.get("modified_at") != observed_revision:
                return uncertain("work_stale")
            if incomplete_reason is not None:
                return uncertain(incomplete_reason)
            if not candidates:
                return uncertain("no_direct_subtasks")
            return RelatedLookup(status="CANDIDATES", work_task_gid=work_task_gid,
                                 observed_revision=observed_revision,
                                 candidates=tuple(candidates))
        except (ProviderError, httpx.HTTPError, KeyError, TypeError, ValueError):
            return uncertain("read_unavailable")

    async def find_grouped(self, root_task_gid: str) -> GroupedLookup:
        """Discover bounded field matches; verified candidates never prove family completeness."""
        candidates: list[GroupedCandidate] = []
        observed_revision: str | None = None

        def uncertain(reason: str) -> GroupedLookup:
            return GroupedLookup(status="UH_OH", root_task_gid=root_task_gid,
                                 observed_revision=observed_revision,
                                 candidates=() if reason in ("work_not_canonical", "root_identity_unverified",
                                                            "work_readback_unavailable", "work_stale")
                                 else tuple(candidates),
                                 reason=reason)

        def identifies_root(task: JSON) -> bool:
            fields = [field for field in self._custom_fields(task)
                      if field.get("gid") == ROOT_WORK_GID]
            return (len(fields) == 1 and fields[0].get("enabled") is True
                    and fields[0].get("resource_subtype") == "text"
                    and fields[0].get("text_value") == root_task_gid)

        try:
            root = await self._task(root_task_gid)
            if root is None or self._gid(root) != root_task_gid:
                return uncertain("work_not_returned")
            revision = root.get("modified_at")
            if not isinstance(revision, str):
                return uncertain("work_revision_unavailable")
            observed_revision = revision
            if not await self._canonical(root):
                return uncertain("work_not_canonical")
            if not identifies_root(root):
                return uncertain("root_identity_unverified")

            response = await self.client.get(
                f"/workspaces/{WORKSPACE}/tasks/search",
                params={f"custom_fields.{ROOT_WORK_GID}.value": root_task_gid,
                        "projects.any": ",".join(sorted(self._admission_projects)),
                        "limit": 100, "opt_fields": "gid"},
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload["data"]
            if not isinstance(rows, list) or payload.get("next_page") is not None:
                return uncertain("invalid_search_page")
            raw_rows = cast(list[object], rows)
            if len(raw_rows) > 100:
                return uncertain("invalid_search_page")

            gids: list[str] = []
            seen: set[str] = set()
            for row in raw_rows:
                gid = self._gid(row)
                if gid is None or not gid or gid in seen:
                    return uncertain("invalid_search_row")
                seen.add(gid)
                gids.append(gid)
            for gid in gids:
                task = await self._task(gid)
                if task is None or self._gid(task) != gid:
                    return uncertain("candidate_not_returned")
                fields = [field for field in self._custom_fields(task)
                          if field.get("gid") == ROOT_WORK_GID]
                if (len(fields) != 1 or fields[0].get("enabled") is not True
                        or fields[0].get("resource_subtype") != "text"
                        or fields[0].get("text_value") != root_task_gid):
                    return uncertain("relationship_changed")
                if not await self._canonical(task):
                    return uncertain("candidate_not_canonical")
                title, candidate_revision = task.get("name"), task.get("modified_at")
                if not isinstance(title, str) or not isinstance(candidate_revision, str):
                    return uncertain("candidate_invalid")
                candidates.append(GroupedCandidate(
                    task_gid=gid, title=title, revision=candidate_revision,
                    root_work_gid=root_task_gid, source="asana_root_work_gid_search_exact_get",
                ))

            try:
                readback = await self._task(root_task_gid)
            except (ProviderError, httpx.HTTPError, KeyError, TypeError, ValueError):
                return uncertain("work_readback_unavailable")
            if readback is None or self._gid(readback) != root_task_gid:
                return uncertain("work_readback_unavailable")
            if not identifies_root(readback):
                return uncertain("root_identity_unverified")
            try:
                if not await self._canonical(readback):
                    return uncertain("work_not_canonical")
            except (ProviderError, httpx.HTTPError, KeyError, TypeError, ValueError):
                return uncertain("work_readback_unavailable")
            if readback.get("modified_at") != observed_revision:
                return uncertain("work_stale")
            if not candidates:
                return uncertain("no_search_matches")
            return GroupedLookup(status="CANDIDATES", root_task_gid=root_task_gid,
                                 observed_revision=observed_revision,
                                 candidates=tuple(candidates))
        except (ProviderError, httpx.HTTPError, KeyError, TypeError, ValueError):
            return uncertain("read_unavailable")

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
                title, notes, completed, revision, await self._canonical(task)
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
            response = await self.client.get(f"/tasks/{provider_task_id}/stories", params=params)
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
            raise ProviderError("provider write failed") from None
        return response

    async def update(self, provider_work_id: str, patch: WorkPatch) -> None:
        changed = patch.model_fields_set
        data = {name: getattr(patch, name) for name in {"notes", "completed"} & changed}
        if "title" in changed:
            data["name"] = patch.title
        routing = set(FIELDS) & changed
        if routing:
            task = await self._task(provider_work_id)
            fields = [] if task is None else self._custom_fields(task)
            custom: dict[str, str] = {}
            for name in routing:
                matches = [field for field in fields if field.get("gid") == FIELDS[name]]
                enabled = len(matches) == 1 and matches[0].get("enabled") is True
                options: object = matches[0].get("enum_options", []) if enabled else []
                choices = [o for o in self._custom_fields({"custom_fields": options})
                           if o.get("name") == getattr(patch, name) and o.get("enabled") is True]
                if len(choices) != 1 or not isinstance(choices[0].get("gid"), str):
                    raise ProviderError("routing write denied")
                custom[FIELDS[name]] = choices[0]["gid"]
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
                response = await self.client.get(
                    f"/workspaces/{WORKSPACE}/tasks/search", params=params
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
                response = await self.client.get("/tasks", params=params)

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
                ))
            return ProviderSearchPage(tuple(items), next_cursor)
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise ProviderError("provider request failed") from None

    async def suggest_next(self, excluded: frozenset[str]) -> ProviderHead | None:
        try:
            candidates: list[tuple[int, int, ProviderHead]] = []
            seen: set[str] = set()
            position = 0
            for project in PROJECTS:
                response = await self.client.get(
                    f"/projects/{project}/tasks",
                    params={"completed_since": "now", "limit": "100", "opt_fields": OPT_FIELDS},
                )
                response.raise_for_status()
                payload = response.json()
                data = payload["data"]
                if not isinstance(data, list) or payload.get("next_page") is not None:
                    raise TypeError
                for value in cast(list[object], data):
                    current_position, position = position, position + 1
                    if not isinstance(value, dict): raise TypeError
                    item = cast(JSON, value)
                    gid, title = item.get("gid"), item.get("name")
                    completed = item.get("completed")
                    raw_fields = item.get("custom_fields")
                    if (not isinstance(gid, str) or not isinstance(title, str)
                            or not isinstance(completed, bool) or not isinstance(raw_fields, list)):
                        raise TypeError
                    raw_values = cast(list[object], raw_fields)
                    if any(not isinstance(field, dict) for field in raw_values): raise TypeError
                    if gid in seen: continue
                    seen.add(gid)
                    if gid in excluded or completed: continue
                    fields = [cast(JSON, field) for field in raw_values]
                    priorities = [field.get("display_value") for field in fields
                                  if field.get("gid") == FIELDS["priority"]]
                    horizons = [field.get("display_value") for field in fields
                                if field.get("gid") == FIELDS["horizon"]]
                    if len(priorities) > 1 or len(horizons) > 1: raise TypeError
                    priority = priorities[0] if priorities else None
                    horizon = horizons[0] if horizons else None
                    if priority is None or isinstance(priority, str) and priority not in PRIORITIES:
                        continue
                    if (not isinstance(priority, str)
                            or horizon is not None and not isinstance(horizon, str)):
                        raise TypeError
                    candidates.append((PRIORITIES[priority], current_position,
                                       ProviderHead(gid, title, priority, horizon)))
            return min(candidates, key=lambda candidate: candidate[:2])[2] if candidates else None
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise ProviderError("provider request failed") from None
