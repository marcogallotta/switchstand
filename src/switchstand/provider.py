from typing import Any, cast

import httpx

from .contracts import Routing, WorkPatch
from .core import ProviderError, ProviderWork, UnknownEffect

PROJECT = "1218210259719507"
FIELDS = {
    "priority": "1217653169990249", "horizon": "1218212397743203",
    "review_next_action": "1218212397743210", "stage3_gate": "1218212397743217"}
OPT_FIELDS = ("name,notes,completed,modified_at,memberships.project.gid,parent.gid,"
              "custom_fields.gid,custom_fields.display_value,custom_fields.enabled,"
              "custom_fields.enum_options.gid,custom_fields.enum_options.name,"
              "custom_fields.enum_options.enabled")
JSON = dict[str, Any]
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
            if (ancestor := await self._task(parent)) is None: return False
            task = ancestor
    @staticmethod
    def _custom_fields(task: JSON) -> list[JSON]:
        fields = task.get("custom_fields")
        return ([cast(JSON, value) for value in cast(list[object], fields) if isinstance(value, dict)]
                if isinstance(fields, list) else [])
    async def get(self, provider_work_id: str) -> ProviderWork | None:
        task = await self._task(provider_work_id)
        if task is None:
            return None
        fields = self._custom_fields(task)
        values = {
            name: next((f.get("display_value") for f in fields if f.get("gid") == gid), None)
            for name, gid in FIELDS.items()
        }
        try:
            route = {key: value if isinstance(value, str) else None
                     for key, value in values.items()}
            routing = Routing(**route)
            return ProviderWork(
                task["name"], task["notes"], task["completed"], task["modified_at"],
                routing, await self._canonical(task),
            )
        except (KeyError, TypeError, ValueError):
            raise ProviderError("provider response invalid") from None
    async def _write(self, method: str, path: str, data: JSON) -> httpx.Response:
        try:
            response = await self.client.request(method, path, json={"data": data})
        except httpx.RequestError:
            raise UnknownEffect("provider effect unknown") from None
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
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
                choices = [option for option in self._custom_fields({"custom_fields": options})
                           if option.get("name") == getattr(patch, name)
                           and option.get("enabled") is True]
                if len(choices) != 1 or not isinstance(choices[0].get("gid"), str):
                    raise ProviderError("routing write denied")
                custom[FIELDS[name]] = choices[0]["gid"]
            data["custom_fields"] = custom
        await self._write("PUT", f"/tasks/{provider_work_id}", data)
    async def append(self, provider_work_id: str, text: str) -> bool:
        path = f"/tasks/{provider_work_id}/stories"
        response = await self._write("POST", path, {"text": text})
        try:
            data = response.json()["data"]
            return isinstance(data, dict) and isinstance(cast(JSON, data).get("gid"), str)
        except (KeyError, TypeError, ValueError): return False
