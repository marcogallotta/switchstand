"""Bootstrap one migrated durable-agent Asana project."""

import argparse
import json
import os
from pathlib import Path
from typing import Any, cast

import httpx

from .launch_source import load_asana_token

JSON = dict[str, Any]
MARKER = "DURABLE_AGENT_PROJECT_V1 / MIGRATED"
SECTIONS = ("CURRENT", "WAITING", "DEFERRED")
RULES: dict[str, tuple[str, set[str]]] = {
    "priority": ("enum", {"P-CRITICAL", "P0", "P1", "P2", "UNSET"}),
    "work_kind": ("enum", {"concern", "research", "design", "implementation",
                            "investigation", "review", "incident"}),
    "currentness": ("enum", {"CURRENT", "MOVING", "STALE", "UNKNOWN"}),
    "canonical_concern": ("text", set()),
}


class BootstrapError(RuntimeError):
    pass


def _gid(value: object) -> str:
    gid = cast(JSON, value).get("gid") if isinstance(value, dict) else None
    if not isinstance(gid, str):
        raise BootstrapError("Asana returned an invalid identity")
    return gid


def _get(client: httpx.Client, path: str, fields: str = "") -> JSON | list[JSON]:
    rows: list[JSON] = []
    offset: str | None = None
    seen: set[str] = set()
    while True:
        try:
            response = client.get(path, params={"limit": 100, "opt_fields": fields,
                                                **({"offset": offset} if offset else {})})
            response.raise_for_status()
            payload = cast(JSON, response.json())
            data = payload["data"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise BootstrapError(f"Asana read failed: {path}") from None
        if isinstance(data, dict):
            return cast(JSON, data)
        if not isinstance(data, list):
            raise BootstrapError(f"Asana read failed: {path}")
        raw_rows = cast(list[object], data)
        if any(not isinstance(row, dict) for row in raw_rows):
            raise BootstrapError(f"Asana read failed: {path}")
        rows.extend(cast(list[JSON], raw_rows))
        page = payload.get("next_page")
        if page is None:
            return rows
        offset = cast(JSON, page).get("offset") if isinstance(page, dict) else None
        if not isinstance(offset, str) or not offset or offset in seen:
            raise BootstrapError(f"Asana read failed: {path}")
        seen.add(offset)


def _post(client: httpx.Client, path: str, data: JSON) -> JSON:
    try:
        response = client.post(path, json={"data": data})
    except httpx.RequestError:
        raise BootstrapError(f"UNKNOWN write outcome: {path}; inspect Asana before rerunning") from None
    if response.status_code >= 500:
        raise BootstrapError(f"UNKNOWN write outcome: {path}; inspect Asana before rerunning")
    try:
        response.raise_for_status()
        result = response.json()["data"]
        if not isinstance(result, dict):
            raise TypeError
        return cast(JSON, result)
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        if response.is_success:
            raise BootstrapError(f"UNKNOWN write outcome: {path}; inspect Asana before rerunning") from None
        raise BootstrapError(f"Asana rejected write {path} ({response.status_code})") from None


def _validate(args: argparse.Namespace) -> None:
    gids = (args.workspace_gid, args.team_gid, args.main_project_gid, *args.fields.values())
    if not args.role.strip() or not args.project_name.strip() or any(not gid.isdigit() for gid in gids):
        raise BootstrapError("names must be nonempty and all GIDs must be exact numeric values")
    if len(set(args.fields.values())) != 4:
        raise BootstrapError("four distinct custom-field GIDs are required")


def _preflight(client: httpx.Client, args: argparse.Namespace) -> None:
    team = cast(JSON, _get(client, f"/teams/{args.team_gid}", "organization.gid"))
    main = cast(JSON, _get(client, f"/projects/{args.main_project_gid}", "workspace.gid"))
    if (_gid(team.get("organization")) != args.workspace_gid
            or _gid(main.get("workspace")) != args.workspace_gid):
        raise BootstrapError("workspace, team, and Main project are incompatible")
    for key, gid in args.fields.items():
        field = cast(JSON, _get(client, f"/custom_fields/{gid}",
            "gid,workspace.gid,resource_subtype,enum_options.name,enum_options.enabled"))
        subtype, required = RULES[key]
        raw_options = field.get("enum_options", [])
        options = cast(list[object], raw_options) if isinstance(raw_options, list) else []
        enabled = {str(cast(JSON, row).get("name")) for row in options
                   if isinstance(row, dict) and cast(JSON, row).get("enabled", True)}
        if (_gid(field) != gid or _gid(field.get("workspace")) != args.workspace_gid
                or field.get("resource_subtype") != subtype or not required <= enabled):
            raise BootstrapError(f"custom field {key} ({gid}) is incompatible")


def _state(client: httpx.Client, args: argparse.Namespace) -> tuple[JSON | None, tuple[str, ...], set[str], JSON | None]:
    projects = cast(list[JSON], _get(client, f"/teams/{args.team_gid}/projects",
                                    "gid,name,notes,workspace.gid"))
    matches = [row for row in projects if row.get("name") == args.project_name
               and MARKER in str(row.get("notes", "")).splitlines()]
    if len(matches) > 1:
        raise BootstrapError("project identity is ambiguous")
    project = matches[0] if matches else None
    if project is None:
        return None, (), set(), None
    if _gid(project.get("workspace")) != args.workspace_gid:
        raise BootstrapError("exact project identity is in an incompatible workspace")
    project_gid = _gid(project)
    raw_sections = cast(list[JSON], _get(client, f"/projects/{project_gid}/sections", "gid,name"))
    sections = tuple(str(row.get("name")) for row in raw_sections
                     if row.get("name") != "Untitled section")
    if sections != SECTIONS[:len(sections)]:
        raise BootstrapError("semantic sections are ambiguous or out of order")
    settings = cast(list[JSON], _get(client, f"/projects/{project_gid}/custom_field_settings",
                                    "custom_field.gid"))
    attached = {_gid(row.get("custom_field")) for row in settings}
    if not attached <= set(args.fields.values()):
        raise BootstrapError("project has custom fields outside the exact supplied set")
    tasks = cast(list[JSON], _get(client, f"/projects/{project_gid}/tasks", "gid,name,notes"))
    master_name = f"AGENT MASTER — {args.role}"
    masters = [row for row in tasks if row.get("name") == master_name
               and MARKER in str(row.get("notes", "")).splitlines()]
    if len(masters) > 1:
        raise BootstrapError("AGENT MASTER identity is ambiguous")
    master = masters[0] if masters else None
    return project, sections, attached, master


def dry_run(args: argparse.Namespace) -> JSON:
    _validate(args)
    return {"mode": "dry-run", "marker": MARKER, "project": args.project_name,
            "operations": ["validate workspace/team/Main and four supplied fields",
                "create or reuse the marked project", "ensure CURRENT/WAITING/DEFERRED sections",
                "attach exactly the supplied fields", f"create or reuse AGENT MASTER — {args.role}",
                "multihome the master into Main", "read back and verify"]}


def apply(client: httpx.Client, args: argparse.Namespace) -> JSON:
    _validate(args)
    _preflight(client, args)
    project, sections, attached, master = _state(client, args)
    wrote = False

    def post(path: str, data: JSON) -> JSON:
        nonlocal wrote
        result = _post(client, path, data)
        wrote = True
        return result

    try:
        project = project or post(f"/teams/{args.team_gid}/projects",
                                  {"name": args.project_name, "notes": MARKER})
        project_gid = _gid(project)
        for name in SECTIONS[len(sections):]:
            post(f"/projects/{project_gid}/sections", {"name": name})
        for gid in args.fields.values():
            if gid not in attached:
                post(f"/projects/{project_gid}/addCustomFieldSetting", {"custom_field": gid})
        master = master or post("/tasks", {"workspace": args.workspace_gid,
            "name": f"AGENT MASTER — {args.role}", "notes": MARKER, "projects": [project_gid]})
        master_gid = _gid(master)
        task = cast(JSON, _get(client, f"/tasks/{master_gid}", "gid,memberships.project.gid"))
        memberships = {_gid(row.get("project")) for row in task.get("memberships", [])}
        if args.main_project_gid not in memberships:
            post(f"/tasks/{master_gid}/addProject", {"project": args.main_project_gid})
        after = _state(client, args)
        task = cast(JSON, _get(client, f"/tasks/{master_gid}", "gid,memberships.project.gid"))
        memberships = {_gid(row.get("project")) for row in task.get("memberships", [])}
        if (after[0] is None or after[1] != SECTIONS or after[2] != set(args.fields.values())
                or after[3] is None or _gid(after[3]) != master_gid
                or not {project_gid, args.main_project_gid} <= memberships):
            raise BootstrapError("authoritative post-apply readback failed")
        return {"mode": "apply", "project_gid": project_gid, "master_gid": master_gid,
                "sections": list(after[1]), "custom_field_gids": list(args.fields.values()),
                "memberships": sorted(memberships)}
    except BootstrapError as error:
        if wrote and not str(error).startswith(("Asana rejected write", "UNKNOWN write outcome")):
            raise BootstrapError(
                f"UNKNOWN state after write: {error}; inspect Asana before rerunning"
            ) from None
        raise


def run_mcp_bootstrap(
    role: str, project_name: str, workspace_gid: str, team_gid: str,
    main_project_gid: str, priority_field_gid: str, work_kind_field_gid: str,
    currentness_field_gid: str, canonical_concern_field_gid: str,
    apply_changes: bool = False,
) -> JSON:
    """Invoke the bootstrap for the ordinary MCP's fixed server-owned boundary."""
    args = argparse.Namespace(
        role=role, project_name=project_name, workspace_gid=workspace_gid,
        team_gid=team_gid, main_project_gid=main_project_gid,
        fields={"priority": priority_field_gid, "work_kind": work_kind_field_gid,
                "currentness": currentness_field_gid,
                "canonical_concern": canonical_concern_field_gid},
    )
    if not apply_changes:
        return dry_run(args)
    with httpx.Client(
        base_url="https://app.asana.com/api/1.0", trust_env=False,
        headers={"Authorization": f"Bearer {os.environ['ASANA_TOKEN']}"},
    ) as client:
        return apply(client, args)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    for flag in ("role", "project-name", "workspace-gid", "team-gid", "main-project-gid",
                 "priority-field-gid", "work-kind-field-gid", "currentness-field-gid",
                 "canonical-concern-field-gid"):
        result.add_argument(f"--{flag}", required=True)
    result.add_argument("--apply", action="store_true")
    result.add_argument("--token-config", type=Path, default=Path.home() / ".config/switchstand/.env")
    return result


def main() -> None:
    command = parser()
    args = command.parse_args()
    args.fields = {"priority": args.priority_field_gid, "work_kind": args.work_kind_field_gid,
                   "currentness": args.currentness_field_gid,
                   "canonical_concern": args.canonical_concern_field_gid}
    try:
        if not args.apply:
            print(json.dumps(dry_run(args), indent=2))
            return
        token = load_asana_token(args.token_config)
        with httpx.Client(base_url="https://app.asana.com/api/1.0", trust_env=False,
                          headers={"Authorization": f"Bearer {token}"}) as client:
            print(json.dumps(apply(client, args), indent=2))
    except (BootstrapError, ValueError) as error:
        command.exit(1, f"durable-agent project bootstrap failed: {error}\n")


if __name__ == "__main__":
    main()
