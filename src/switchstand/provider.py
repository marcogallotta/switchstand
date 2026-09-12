from typing import Any, cast

import httpx

from .contracts import Routing, WorkPatch
from .core import (
    ProviderError,
    ProviderHead,
    ProviderSourceStory,
    ProviderSourceTask,
    ProviderStoriesPage,
    ProviderWork,
    UnknownEffect,
)

PROJECT = "1218210259719507"
ANCESTRY_GETS = 9
FIELDS = {
    "priority": "1217653169990249", "horizon": "1218212397743203",
    "review_next_action": "1218212397743210", "stage3_gate": "1218212397743217"}
OPT_FIELDS = ("gid,name,notes,completed,modified_at,memberships.project.gid,parent.gid,"
              "custom_fields.gid,custom_fields.display_value,custom_fields.enabled,"
              "custom_fields.enum_options.gid,custom_fields.enum_options.name,"
              "custom_fields.enum_options.enabled")
STORY_FIELDS = "gid,resource_subtype,text,created_at,created_by.name,target.gid"
JSON = dict[str, Any]
PRIORITIES = {f"P{value}": value for value in range(4)}


class AsanaProvider:
    def __init__(self, client: httpx.AsyncClient): self.client = client

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
            if any(self._gid(cast(JSON, item).get("project")) == PROJECT
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
        routing = {name for name in FIELDS if name != "priority"} & changed
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

    async def suggest_next(self, excluded: frozenset[str]) -> ProviderHead | None:
        try:
            response = await self.client.get(
                f"/projects/{PROJECT}/tasks",
                params={"completed_since": "now", "limit": "100", "opt_fields": OPT_FIELDS},
            )
            response.raise_for_status()
            payload = response.json()
            data = payload["data"]
            if not isinstance(data, list) or payload.get("next_page") is not None:
                raise TypeError
            candidates: list[tuple[int, int, ProviderHead]] = []
            for position, value in enumerate(cast(list[object], data)):
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
                if not isinstance(priority, str) or horizon is not None and not isinstance(horizon, str):
                    raise TypeError
                candidates.append((PRIORITIES[priority], position,
                                   ProviderHead(gid, title, priority, horizon)))
            return min(candidates, key=lambda candidate: candidate[:2])[2] if candidates else None
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise ProviderError("provider request failed") from None
