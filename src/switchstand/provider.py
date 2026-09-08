from typing import Any, cast

import httpx

from .contracts import Routing, WorkPatch
from .core import ProviderError, ProviderHead, ProviderWork, UnknownEffect

PROJECT = "1218210259719507"
ANCESTRY_GETS = 9
FIELDS = {
    "priority": "1217653169990249", "horizon": "1218212397743203",
    "review_next_action": "1218212397743210", "stage3_gate": "1218212397743217"}
OPT_FIELDS = ("name,notes,completed,modified_at,memberships.project.gid,parent.gid,"
              "custom_fields.gid,custom_fields.display_value,custom_fields.enabled,"
              "custom_fields.enum_options.gid,custom_fields.enum_options.name,"
              "custom_fields.enum_options.enabled")
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
        except (httpx.HTTPError, KeyError, TypeError, ValueError): raise ProviderError("provider request failed") from None
    @staticmethod
    def _gid(value: object) -> str | None:
        gid = cast(JSON, value).get("gid") if isinstance(value, dict) else None; return gid if isinstance(gid, str) else None
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
    async def _write(
        self, method: str, path: str, data: JSON, *, unknown_on_server_error: bool = False
    ) -> httpx.Response:
        try:
            response = await self.client.request(method, path, json={"data": data})
        except httpx.RequestError: raise UnknownEffect("provider effect unknown") from None
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
    async def append(self, provider_work_id: str, text: str) -> bool:
        response = await self._write(
            "POST", f"/tasks/{provider_work_id}/stories", {"text": text},
            unknown_on_server_error=True,
        )
        try:
            data = response.json()["data"]
            return isinstance(data, dict) and isinstance(cast(JSON, data).get("gid"), str)
        except (KeyError, TypeError, ValueError): return False

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
                if not isinstance(value, dict):
                    raise TypeError
                item = cast(JSON, value)
                gid, title = item.get("gid"), item.get("name")
                completed = item.get("completed")
                raw_fields = item.get("custom_fields")
                if (not isinstance(gid, str) or not isinstance(title, str)
                        or not isinstance(completed, bool) or not isinstance(raw_fields, list)
                        or any(not isinstance(field, dict) for field in raw_fields)):
                    raise TypeError
                if gid in excluded or completed:
                    continue
                fields = cast(list[JSON], raw_fields)
                priorities = [field.get("display_value") for field in fields
                              if field.get("gid") == FIELDS["priority"]]
                horizons = [field.get("display_value") for field in fields
                            if field.get("gid") == FIELDS["horizon"]]
                if len(priorities) > 1 or len(horizons) > 1:
                    raise TypeError
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
