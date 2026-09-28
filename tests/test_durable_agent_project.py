import json
from argparse import Namespace

import httpx
import pytest
from chatgpt_fixture import service

from switchstand import chatgpt_mcp
from switchstand.chatgpt_mcp import build_chatgpt_server
from switchstand.durable_agent_project import MARKER, BootstrapError, apply, dry_run


def arguments() -> Namespace:
    return Namespace(
        role="Asana",
        project_name="SW — Asana Agent",
        workspace_gid="1",
        team_gid="2",
        main_project_gid="3",
        fields={"priority": "11", "work_kind": "12", "currentness": "13",
                "canonical_concern": "14"},
    )


class AsanaBoundary:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self.posts: list[str] = []
        self.projects: list[dict[str, object]] = []
        self.sections: list[dict[str, object]] = [{"gid": "20", "name": "Untitled section"}]
        self.settings: list[dict[str, object]] = []
        self.tasks: list[dict[str, object]] = []
        self.sequence = 100
        self.incompatible_field: str | None = None
        self.ambiguous_path: str | None = None
        self.fail_read_after_write: str | None = None
        self.drop_multihome = False

    def gid(self) -> str:
        self.sequence += 1
        return str(self.sequence)

    def field(self, gid: str) -> dict[str, object]:
        options = {
            "11": ["P-CRITICAL", "P0", "P1", "P2", "UNSET"],
            "12": ["concern", "research", "design", "implementation", "investigation",
                   "review", "incident"],
            "13": ["CURRENT", "MOVING", "STALE", "UNKNOWN"],
            "14": [],
        }[gid]
        subtype = "text" if gid == "14" else "enum"
        if self.incompatible_field == gid:
            subtype = "text"
        return {"gid": gid, "workspace": {"gid": "1"}, "resource_subtype": subtype,
                "enum_options": [{"name": name, "enabled": True} for name in options]}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/1.0")
        self.requests.append((request.method, path))
        if request.method == "GET":
            if path == self.fail_read_after_write and self.posts:
                return httpx.Response(503, json={"errors": [{"message": "unavailable"}]})
            if path == "/teams/2":
                data: object = {"gid": "2", "organization": {"gid": "1"}}
            elif path == "/projects/3":
                data = {"gid": "3", "workspace": {"gid": "1"}}
            elif path.startswith("/custom_fields/"):
                data = self.field(path.rsplit("/", 1)[-1])
            elif path == "/teams/2/projects":
                data = self.projects
            elif path.endswith("/sections"):
                data = self.sections
            elif path.endswith("/custom_field_settings"):
                data = self.settings
            elif path.endswith("/tasks"):
                data = self.tasks
            elif path.startswith("/tasks/"):
                gid = path.rsplit("/", 1)[-1]
                data = next(row for row in self.tasks if row["gid"] == gid)
            else:
                raise AssertionError(path)
            return httpx.Response(200, json={"data": data, "next_page": None})

        self.posts.append(path)
        body = json.loads(request.content)["data"]
        if path == "/teams/2/projects":
            data = body | {"gid": self.gid(), "workspace": {"gid": "1"}}
            self.projects.append(data)
        elif path.endswith("/sections"):
            data = {"gid": self.gid(), "name": body["name"]}
            self.sections.append(data)
        elif path.endswith("/addCustomFieldSetting"):
            data = {}
            self.settings.append({"custom_field": {"gid": body["custom_field"]}})
        elif path == "/tasks":
            data = body | {"gid": self.gid(), "memberships": [
                {"project": {"gid": body["projects"][0]}}]}
            self.tasks.append(data)
        elif path.endswith("/addProject"):
            data = {}
            task = next(row for row in self.tasks if row["gid"] == path.split("/")[2])
            if not self.drop_multihome:
                memberships = task["memberships"]
                assert isinstance(memberships, list)
                memberships.append({"project": {"gid": body["project"]}})
        else:
            raise AssertionError(path)
        if path == self.ambiguous_path:
            return httpx.Response(503, json={"errors": [{"message": "uncertain"}]})
        return httpx.Response(201, json={"data": data})


def client(boundary: AsanaBoundary) -> httpx.Client:
    return httpx.Client(base_url="https://app.asana.com/api/1.0",
                        transport=httpx.MockTransport(boundary))


def test_dry_run_plans_without_any_http_request() -> None:
    boundary = AsanaBoundary()
    result = dry_run(arguments())
    assert result["mode"] == "dry-run"
    assert result["project"] == "SW — Asana Agent"
    assert boundary.requests == []


def test_apply_creates_reads_back_and_is_idempotent() -> None:
    boundary = AsanaBoundary()
    with client(boundary) as session:
        result = apply(session, arguments())
        writes = list(boundary.posts)
        again = apply(session, arguments())
    assert result["sections"] == ["CURRENT", "WAITING", "DEFERRED"]
    assert set(result["custom_field_gids"]) == {"11", "12", "13", "14"}
    assert set(result["memberships"]) == {result["project_gid"], "3"}
    assert boundary.posts == writes
    assert again["project_gid"] == result["project_gid"]


async def test_ordinary_mcp_tool_previews_and_applies_with_server_token(monkeypatch) -> None:
    boundary = AsanaBoundary()
    clients: list[dict[str, object]] = []
    client_type = httpx.Client

    def client_factory(**kwargs):
        clients.append(kwargs)
        return client_type(base_url="https://app.asana.com/api/1.0",
                           transport=httpx.MockTransport(boundary))

    monkeypatch.setenv("ASANA_TOKEN", "server-token")
    monkeypatch.setattr(chatgpt_mcp.httpx, "Client", client_factory)
    server = build_chatgpt_server(service())
    tool = next(tool for tool in await server.list_tools()
                if tool.name == "agent_project_bootstrap")
    assert set(tool.input_schema["properties"]) == {
        "api_version", "role", "project_name", "workspace_gid", "team_gid",
        "main_project_gid", "priority_field_gid", "work_kind_field_gid",
        "currentness_field_gid", "canonical_concern_field_gid", "apply",
    }
    inputs = {
        "api_version": "1", "role": "Asana", "project_name": "SW — Asana Agent",
        "workspace_gid": "1", "team_gid": "2", "main_project_gid": "3",
        "priority_field_gid": "11", "work_kind_field_gid": "12",
        "currentness_field_gid": "13", "canonical_concern_field_gid": "14",
    }
    preview = await server.call_tool("agent_project_bootstrap", inputs)
    assert preview.structured_content["mode"] == "dry-run"
    assert clients == [] and boundary.requests == []
    applied = await server.call_tool("agent_project_bootstrap", inputs | {"apply": True})
    assert applied.structured_content["mode"] == "apply"
    assert clients == [{
        "base_url": "https://app.asana.com/api/1.0", "trust_env": False,
        "headers": {"Authorization": "Bearer server-token"},
    }]


@pytest.mark.parametrize("problem", ["project", "field", "master"])
def test_apply_fails_closed_before_writes(problem: str) -> None:
    boundary = AsanaBoundary()
    if problem in {"project", "master"}:
        boundary.projects.append({"gid": "31", "name": "SW — Asana Agent",
                                  "notes": MARKER, "workspace": {"gid": "1"}})
    if problem == "project":
        boundary.projects.append({"gid": "32", "name": "SW — Asana Agent",
                                  "notes": MARKER, "workspace": {"gid": "1"}})
    if problem == "field":
        boundary.incompatible_field = "13"
    if problem == "master":
        boundary.tasks.extend([
            {"gid": "41", "name": "AGENT MASTER — Asana", "notes": MARKER},
            {"gid": "42", "name": "AGENT MASTER — Asana", "notes": MARKER},
        ])
    with client(boundary) as session, pytest.raises(BootstrapError):
        apply(session, arguments())
    assert boundary.posts == []


def test_project_identity_ignores_name_only_and_marker_only_decoys() -> None:
    boundary = AsanaBoundary()
    boundary.projects.extend([
        {"gid": "31", "name": "SW — Asana Agent", "notes": "",
         "workspace": {"gid": "1"}},
        {"gid": "32", "name": "other project", "notes": MARKER,
         "workspace": {"gid": "1"}},
    ])
    with client(boundary) as session:
        result = apply(session, arguments())
    assert result["project_gid"] not in {"31", "32"}
    assert boundary.posts.count("/teams/2/projects") == 1


def test_project_identity_reuses_one_exact_match_among_decoys() -> None:
    boundary = AsanaBoundary()
    boundary.projects.extend([
        {"gid": "31", "name": "SW — Asana Agent", "notes": "",
         "workspace": {"gid": "1"}},
        {"gid": "32", "name": "other project", "notes": MARKER,
         "workspace": {"gid": "1"}},
        {"gid": "33", "name": "SW — Asana Agent", "notes": MARKER,
         "workspace": {"gid": "1"}},
    ])
    with client(boundary) as session:
        result = apply(session, arguments())
    assert result["project_gid"] == "33"
    assert "/teams/2/projects" not in boundary.posts


def test_master_identity_ignores_name_only_and_marker_only_decoys() -> None:
    boundary = AsanaBoundary()
    boundary.projects.append({"gid": "31", "name": "SW — Asana Agent", "notes": MARKER,
                              "workspace": {"gid": "1"}})
    boundary.tasks.extend([
        {"gid": "41", "name": "AGENT MASTER — Asana", "notes": "", "memberships": []},
        {"gid": "42", "name": "old master name", "notes": MARKER, "memberships": []},
    ])
    with client(boundary) as session:
        result = apply(session, arguments())
    assert result["master_gid"] not in {"41", "42"}
    assert boundary.posts.count("/tasks") == 1


def test_master_identity_reuses_one_exact_match_among_decoys() -> None:
    boundary = AsanaBoundary()
    boundary.projects.append({"gid": "31", "name": "SW — Asana Agent", "notes": MARKER,
                              "workspace": {"gid": "1"}})
    boundary.tasks.extend([
        {"gid": "41", "name": "AGENT MASTER — Asana", "notes": "", "memberships": []},
        {"gid": "42", "name": "old master name", "notes": MARKER, "memberships": []},
        {"gid": "43", "name": "AGENT MASTER — Asana", "notes": MARKER,
         "memberships": [{"project": {"gid": "31"}}, {"project": {"gid": "3"}}]},
    ])
    with client(boundary) as session:
        result = apply(session, arguments())
    assert result["master_gid"] == "43"
    assert "/tasks" not in boundary.posts


def test_ambiguous_write_stops_and_reports_unknown() -> None:
    boundary = AsanaBoundary()
    boundary.ambiguous_path = "/teams/2/projects"
    with client(boundary) as session, pytest.raises(BootstrapError, match="UNKNOWN"):
        apply(session, arguments())
    assert boundary.posts == ["/teams/2/projects"]


def test_successful_writes_then_failed_readback_reports_unknown_without_retry() -> None:
    boundary = AsanaBoundary()
    boundary.fail_read_after_write = "/teams/2/projects"
    with client(boundary) as session, pytest.raises(
            BootstrapError, match="UNKNOWN.*inspect Asana before rerunning"):
        apply(session, arguments())
    assert boundary.requests.count(("GET", "/teams/2/projects")) == 2
    assert boundary.requests[-1] == ("GET", "/teams/2/projects")


def test_authoritative_readback_rejects_missing_main_membership() -> None:
    boundary = AsanaBoundary()
    boundary.drop_multihome = True
    with client(boundary) as session, pytest.raises(BootstrapError, match="readback"):
        apply(session, arguments())
