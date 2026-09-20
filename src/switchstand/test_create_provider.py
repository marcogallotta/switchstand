"""Test-only Asana create/recovery adapter for isolated cutover qualification."""

import asyncio
from typing import cast
from uuid import UUID

import httpx

from .core import ProviderError, UnknownEffect
from .provider import JSON, WORKSPACE, AsanaProvider

RECOVERY_DELAYS = (0.0, 1.0, 2.0, 5.0, 10.0, 15.0)


class TestCreateAsanaProvider(AsanaProvider):
    __test__ = False

    def __init__(
        self, client: httpx.AsyncClient, test_project_gid: str, correlation_field_gid: str,
        *, lose_confirmation_after_post: bool = False,
    ):
        if not correlation_field_gid.isdigit():
            raise ValueError("create correlation field GID must be numeric")
        super().__init__(client, test_project_gid, test_only=True)
        self.test_project_gid = test_project_gid
        self.correlation_field_gid = correlation_field_gid
        self.lose_confirmation_after_post = lose_confirmation_after_post
        self._same_process_unknown: set[UUID] = set()

    def recovery_identity(self) -> str:
        return (
            f"asana-custom-field-v1:{WORKSPACE}:"
            f"{self.test_project_gid}:{self.correlation_field_gid}"
        )

    def _correlation(self, task: JSON) -> str | None:
        matches = [field for field in self._custom_fields(task)
                   if field.get("gid") == self.correlation_field_gid]
        if (len(matches) != 1 or matches[0].get("enabled") is not True
                or matches[0].get("resource_subtype") != "text"):
            return None
        value = matches[0].get("text_value")
        return value if isinstance(value, str) else None

    async def _verify_created(
        self, task_gid: str, parent_task_gid: str, operation_id: UUID,
    ) -> bool:
        task = await self._task(task_gid)
        return bool(
            task is not None and self._gid(task) == task_gid
            and self._gid(task.get("parent")) == parent_task_gid
            and self._correlation(task) == str(operation_id)
            and await self._canonical(task)
        )

    async def create_child(
        self, parent_task_gid: str, title: str, notes: str, operation_id: UUID,
    ) -> str:
        parent = await self._task(parent_task_gid)
        if parent is None or not await self._canonical(parent):
            raise ProviderError("create parent denied")
        response = await self._write("POST", "/tasks", {
            "workspace": WORKSPACE, "name": title, "notes": notes, "parent": parent_task_gid,
            "projects": [self.test_project_gid],
            "custom_fields": {self.correlation_field_gid: str(operation_id)},
        }, unknown_on_server_error=True)
        if self.lose_confirmation_after_post:
            self._same_process_unknown.add(operation_id)
            raise UnknownEffect("injected confirmation loss after successful provider POST")
        try:
            payload = response.json()["data"]
            task_gid = self._gid(payload)
            if task_gid is None or not await self._verify_created(
                task_gid, parent_task_gid, operation_id,
            ):
                raise UnknownEffect("created task readback unknown")
            return task_gid
        except UnknownEffect:
            raise
        except (ProviderError, KeyError, TypeError, ValueError):
            raise UnknownEffect("created task readback unknown") from None

    async def recover_created(self, parent_task_gid: str, operation_id: UUID) -> str | None:
        if operation_id in self._same_process_unknown:
            return None
        for delay in RECOVERY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await self.client.get(
                    f"/workspaces/{WORKSPACE}/tasks/search",
                    params={
                        f"custom_fields.{self.correlation_field_gid}.value": str(operation_id),
                        "projects.any": self.test_project_gid, "limit": 2, "opt_fields": "gid",
                    },
                )
                response.raise_for_status()
                payload = response.json()
                rows = payload["data"]
                if not isinstance(rows, list) or payload.get("next_page") is not None:
                    raise ProviderError("create correlation search invalid")
                gids = [self._gid(row) for row in cast(list[object], rows)]
                if any(gid is None for gid in gids) or len(gids) > 1:
                    raise ProviderError("create correlation conflict")
                if not gids:
                    continue
                task_gid = cast(str, gids[0])
                if not await self._verify_created(task_gid, parent_task_gid, operation_id):
                    raise ProviderError("create correlation mismatch")
                return task_gid
            except httpx.HTTPError:
                continue
        return None
