import json

import httpx
import pytest
from pydantic import ValidationError

from switchstand.core import ProviderError, ProviderRelation, UnknownEffect
from switchstand.grants import RelationPatch
from switchstand.provider import (PROJECTS, REVIEW_INTAKE_PROJECT, REVIEW_INTAKE_SECTION, AsanaProvider)

PROJECT = "9999999999999999"
SECTION = "8888888888888888"
UNRELATED_PROJECT = "7777777777777777"
TASK = "123"
TARGET = "456"


class Boundary(httpx.AsyncBaseTransport):
    def __init__(self):
        self.calls = []
        self.dependencies = set()
        self.fail_remove_once = False
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
        if path.endswith("/removeProject") and self.fail_remove_once:
            self.fail_remove_once = False
            return httpx.Response(400, request=request, json={"errors": []})
        if path.endswith("/addProject"):
            membership = {
                "project": {"gid": data["project"], "name": "Area"},
                "section": (
                    None if "section" not in data
                    else {"gid": data["section"], "name": "Stage"}
                ),
            }
            task["memberships"] = [
                row for row in task["memberships"]
                if row["project"]["gid"] != data["project"]
            ] + [membership]
        elif path.endswith("/removeProject"):
            task["memberships"] = [
                row for row in task["memberships"]
                if row["project"]["gid"] != data["project"]
            ]
        elif request.method == "PUT":
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


async def test_unprobed_provider_relations_send_once_and_exact_readback(subject):
    provider, boundary = subject
    cases = [
        ProviderRelation("assignee", "set", assignee_gid="42"),
        ProviderRelation("placement", "move", project_gid=PROJECT, section_gid=SECTION),
    ]
    for relation in cases:
        before = len(boundary.calls)
        await provider.update_relation(TASK, relation)
        assert len(boundary.calls) == before + 1
        assert await provider.relation_matches(TASK, relation)


async def test_dependency_readback_exhausts_pages_before_proving_presence_or_absence(subject):
    provider, boundary = subject
    boundary.dependencies = {str(1000 + value) for value in range(150)}
    target = sorted(boundary.dependencies)[120]
    boundary.tasks[target] = boundary.task(target)
    present = ProviderRelation("dependency", "add", target_gid=target)
    assert await provider.relation_matches(TASK, present)

    absent = ProviderRelation("dependency", "remove", target_gid="999999")
    assert await provider.relation_matches(TASK, absent)


async def test_move_removes_old_admitted_membership_but_preserves_unrelated_membership():
    boundary = Boundary()
    old_project = PROJECTS[1]
    boundary.tasks[TASK]["memberships"] = [
        {"project": {"gid": old_project, "name": "Old Area"}, "section": None},
        {
            "project": {"gid": UNRELATED_PROJECT, "name": "Unrelated"},
            "section": None,
        },
    ]
    async with httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0", transport=boundary,
    ) as client:
        provider = AsanaProvider(client, PROJECT)
        move = ProviderRelation(
            "placement", "move", project_gid=PROJECT, section_gid=SECTION,
        )

        await provider.update_relation(TASK, move)

        assert boundary.calls == [
            (
                "POST", f"/api/1.0/tasks/{TASK}/addProject",
                {"project": PROJECT, "section": SECTION},
            ),
            (
                "POST", f"/api/1.0/tasks/{TASK}/removeProject",
                {"project": old_project},
            ),
        ]
        assert await provider.relation_matches(TASK, move)
        assert {
            row["project"]["gid"] for row in boundary.tasks[TASK]["memberships"]
        } == {PROJECT, UNRELATED_PROJECT}


async def test_partial_move_is_unknown_and_resume_sends_only_missing_removal():
    boundary = Boundary()
    old_project = PROJECTS[1]
    boundary.tasks[TASK]["memberships"] = [
        {"project": {"gid": old_project, "name": "Old Area"}, "section": None},
    ]
    boundary.fail_remove_once = True
    async with httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0", transport=boundary,
    ) as client:
        provider = AsanaProvider(client, PROJECT)
        move = ProviderRelation(
            "placement", "move", project_gid=PROJECT, section_gid=SECTION,
        )

        with pytest.raises(UnknownEffect):
            await provider.update_relation(TASK, move)
        assert not await provider.relation_matches(TASK, move)

        await provider.update_relation(TASK, move)

        assert [path.rsplit("/", 1)[-1] for _, path, _ in boundary.calls] == [
            "addProject", "removeProject", "removeProject",
        ]
        assert await provider.relation_matches(TASK, move)


async def test_review_intake_project_is_add_only_not_general_admission():
    boundary = Boundary()
    admitted_project = PROJECTS[0]
    boundary.tasks[TASK]["memberships"] = [
        {"project": {"gid": admitted_project, "name": "Area"}, "section": None},
    ]
    async with httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0", transport=boundary,
    ) as client:
        provider = AsanaProvider(client)
        assert REVIEW_INTAKE_PROJECT not in provider._admission_projects

        registration = ProviderRelation(
            "placement", "add",
            project_gid=REVIEW_INTAKE_PROJECT, section_gid=REVIEW_INTAKE_SECTION,
        )
        await provider.update_relation(TASK, registration)

        assert await provider.relation_matches(TASK, registration)
        assert boundary.calls == [
            (
                "POST", f"/api/1.0/tasks/{TASK}/addProject",
                {"project": REVIEW_INTAKE_PROJECT, "section": REVIEW_INTAKE_SECTION},
            ),
        ]

        with pytest.raises(ProviderError):
            await provider.update_relation(
                TASK,
                ProviderRelation(
                    "placement", "move",
                    project_gid=REVIEW_INTAKE_PROJECT, section_gid=SECTION,
                ),
            )
        with pytest.raises(ProviderError):
            await provider.update_relation(
                TASK,
                ProviderRelation(
                    "placement", "remove", project_gid=REVIEW_INTAKE_PROJECT,
                ),
            )
        with pytest.raises(ProviderError):
            await provider.update_relation(
                TASK,
                ProviderRelation(
                    "placement", "add", project_gid=UNRELATED_PROJECT,
                ),
            )
