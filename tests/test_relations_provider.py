import json

import httpx
import pytest
from pydantic import ValidationError

from switchstand.core import ProviderRelation
from switchstand.grants import RelationPatch
from switchstand.provider import AsanaProvider

PROJECT = "9999999999999999"
SECTION = "8888888888888888"
TASK = "123"
TARGET = "456"


class Boundary(httpx.AsyncBaseTransport):
    def __init__(self):
        self.calls = []
        self.dependencies = set()
        self.tasks = {
            TASK: self.task(TASK),
            TARGET: self.task(TARGET),
        }

    @staticmethod
    def task(gid):
        return {
            "gid": gid, "name": f"Task {gid}", "notes": "", "completed": False,
            "modified_at": "r1", "assignee": None, "parent": None, "custom_fields": [],
            "memberships": [{"project": {"gid": PROJECT, "name": "Area"}, "section": None}],
        }

    async def handle_async_request(self, request):
        path = request.url.path
        if request.method == "GET" and path.endswith("/dependencies"):
            ordered = sorted(self.dependencies)
            offset = request.url.params.get("offset")
            start = 0 if offset is None else int(offset)
            page = ordered[start:start + 100]
            next_page = (
                {"offset": str(start + 100)}
                if start + 100 < len(ordered) else None
            )
            return httpx.Response(
                200, request=request,
                json={"data": [{"gid": gid} for gid in page], "next_page": next_page},
            )
        if request.method == "GET":
            gid = path.rsplit("/", 1)[-1]
            return httpx.Response(200, request=request, json={"data": self.tasks[gid]})
        data = json.loads(request.content)["data"]
        self.calls.append((request.method, path, data))
        task = self.tasks[TASK]
        if path.endswith("/addProject"):
            task["memberships"] = [{
                "project": {"gid": data["project"], "name": "Area"},
                "section": (
                    None if "section" not in data
                    else {"gid": data["section"], "name": "Stage"}
                ),
            }]
        elif path.endswith("/removeProject"):
            task["memberships"] = []
        elif path.endswith("/setParent"):
            task["parent"] = (
                None if data["parent"] is None
                else {"gid": data["parent"]}
            )
        elif path.endswith("/addDependencies"):
            self.dependencies.update(data["dependencies"])
        elif path.endswith("/removeDependencies"):
            self.dependencies.difference_update(data["dependencies"])
        else:
            task["assignee"] = (
                None if data["assignee"] is None
                else {"gid": data["assignee"], "name": "User"}
            )
        return httpx.Response(200, request=request, json={"data": task})


@pytest.fixture
async def subject():
    boundary = Boundary()
    client = httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0", transport=boundary
    )
    provider = AsanaProvider(client, PROJECT, test_only=True)
    yield provider, boundary
    await client.aclose()


@pytest.mark.parametrize("patch", [
    RelationPatch(kind="assignee", action="set", assignee_gid="42"),
    RelationPatch(kind="placement", action="move", project_gid=PROJECT, section_gid=SECTION),
    RelationPatch(kind="parent", action="set", target_work_id="00000000-0000-0000-0000-000000000001"),
    RelationPatch(kind="dependency", action="add", target_work_id="00000000-0000-0000-0000-000000000001"),
])
def test_relation_patch_accepts_only_bounded_shapes(patch):
    assert patch.kind in {"assignee", "placement", "parent", "dependency"}
    with pytest.raises(ValidationError):
        RelationPatch(kind=patch.kind, action="move")


async def test_provider_relations_send_once_and_exact_readback(subject):
    provider, boundary = subject
    cases = [
        ProviderRelation("assignee", "set", assignee_gid="42"),
        ProviderRelation("placement", "move", project_gid=PROJECT, section_gid=SECTION),
        ProviderRelation("parent", "set", target_gid=TARGET),
        ProviderRelation("dependency", "add", target_gid=TARGET),
    ]
    for relation in cases:
        before = len(boundary.calls)
        await provider.update_relation(TASK, relation)
        assert len(boundary.calls) == before + 1
        assert await provider.relation_matches(TASK, relation)

    removal = ProviderRelation("dependency", "remove", target_gid=TARGET)
    await provider.update_relation(TASK, removal)
    assert await provider.relation_matches(TASK, removal)


async def test_dependency_readback_exhausts_pages_before_proving_presence_or_absence(subject):
    provider, boundary = subject
    boundary.dependencies = {str(1000 + value) for value in range(150)}
    target = sorted(boundary.dependencies)[120]
    boundary.tasks[target] = boundary.task(target)
    present = ProviderRelation("dependency", "add", target_gid=target)
    assert await provider.relation_matches(TASK, present)

    absent = ProviderRelation("dependency", "remove", target_gid="999999")
    assert await provider.relation_matches(TASK, absent)

    before = len(boundary.calls)
    removal = ProviderRelation("dependency", "remove", target_gid=target)
    await provider.update_relation(TASK, removal)
    assert len(boundary.calls) == before + 1
    assert await provider.relation_matches(TASK, removal)
