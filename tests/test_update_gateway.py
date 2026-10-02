import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import httpx
import pytest
from conftest import activate_stage1
from mcp import Client
from pydantic import ValidationError
from sqlalchemy import delete, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.chatgpt import ChatGPTService
from switchstand.chatgpt_mcp import build_ordinary_tools
from switchstand.contracts import LaunchAuthority
from switchstand.core import Controller, Provider, authoritative_revision
from switchstand.discovery import ProviderSearchItem
from switchstand.effect_recovery import EffectRecovery
from switchstand.grant_state import GrantState, effect_intents
from switchstand.grants import (
    PrincipalContext,
    ProtectedRelation,
    ProtectedUpdate,
    RelationPatch,
    ScalarPatch,
    WorkGrant,
)
from switchstand.managed_identity import managed_principal, rotate_managed_grant
from switchstand.mcp import build_server
from switchstand.provider import AsanaProvider
from switchstand.relations import RelationGateway
from switchstand.state import (
    PostgresState,
    metadata,
    work_edges,
    workset_authority,
    workset_cutovers,
)
from switchstand.updates import UpdateGateway
from switchstand.work_metadata import (
    ProviderMetadataSnapshot,
    activate_metadata,
    generate_worksheet,
)
from switchstand.worksets import Membership, Workset, WorksetSnapshot, stage_snapshot

PROJECT = "9999999999999999"
PRIORITY = "1217653169990249"
PRIORITY_OPTIONS = {"p0": "P0", "p1": "P1"}
WORK_TYPE, WORK_TYPE_OPTIONS = "1218431623135287", {"w0": "Research", "w1": "Implementation"}
REVIEW = "1218212397743210"
REVIEW_OPTIONS = {"r0": "Code Review", "r1": "Needs Work"}


class AsanaBoundary(httpx.AsyncBaseTransport):
    def __init__(self, engine):
        self.engine, self.puts, self.gets = engine, [], 0
        self.mode = "commit_lost"
        self.task = {"gid": "123", "name": "Initial", "notes": "old", "completed": False,
                     "modified_at": "r1", "assignee": None,
                     "memberships": [{"project": {"gid": PROJECT, "name": "Test"},
                                      "section": None}],
                     "parent": None, "custom_fields": [{
                         "gid": PRIORITY, "enabled": True, "resource_subtype": "enum",
                         "display_value": "P0", "enum_value": {"gid": "p0"},
                         "enum_options": [
                             {"gid": gid, "name": name, "enabled": True}
                             for gid, name in PRIORITY_OPTIONS.items()]}, {"gid": WORK_TYPE, "enabled": True, "resource_subtype": "enum", "display_value": "Research", "enum_value": {"gid": "w0"},
                         "enum_options": [{"gid": g, "name": n, "enabled": True} for g, n in WORK_TYPE_OPTIONS.items()]}, {"gid": REVIEW, "enabled": True, "resource_subtype": "enum", "display_value": "Code Review", "enum_value": {"gid": "r0"},
                         "enum_options": [{"gid": g, "name": n, "enabled": True} for g, n in REVIEW_OPTIONS.items()]}]}

    async def handle_async_request(self, request):
        if request.method == "GET":
            self.gets += 1
            return httpx.Response(200, request=request, json={"data": self.task})
        payload = json.loads(request.content)["data"]
        async with self.engine.connect() as connection:
            outcomes = (await connection.execute(select(effect_intents.c.outcome).where(
                effect_intents.c.outcome["effect"].astext == "unknown"
            ))).scalars().all()
        assert outcomes
        self.puts.append(payload)
        if self.mode == "reject":
            return httpx.Response(400, request=request, json={"errors": []})
        if self.mode != "lost":
            custom = payload.get("custom_fields")
            self.task.update({key: value for key, value in payload.items()
                              if key != "custom_fields"})
            if custom is not None:
                for field in self.task["custom_fields"]:
                    if field["gid"] in custom:
                        options = {PRIORITY: PRIORITY_OPTIONS, WORK_TYPE: WORK_TYPE_OPTIONS,
                                   REVIEW: REVIEW_OPTIONS}[field["gid"]]
                        field["display_value"] = options[custom[field["gid"]]]
                        field["enum_value"] = {"gid": custom[field["gid"]]}
            self.task["modified_at"] = f"r{len(self.puts) + 1}"
        if self.mode in {"commit_lost", "lost"}:
            raise httpx.ReadError("response lost", request=request)
        return httpx.Response(200, request=request, json={"data": self.task})


class ReadOnlyProvider:
    def __init__(self, current):
        self.current = current

    async def get(self, _provider_work_id):
        return self.current


def request(grant, revision, **patch):
    return ProtectedUpdate(api_version="1", operation_id=uuid4(),
                           work_id=grant.authority.active_work_id,
                           grant_version=grant.version, observed_revision=revision,
                           patch=ScalarPatch(**patch))


async def activate_stage2(gateway, current):
    await activate_stage1(gateway.state.engine, (ProviderSearchItem(
        provider_work_id="123", title=current.title, completed=current.completed,
        revision=current.revision, routing=current.routing, context=current.context,
    ),))
    worksheet = await generate_worksheet(gateway.state.engine)
    await activate_metadata(gateway.state.engine, worksheet, (
        ProviderMetadataSnapshot(
            "123", current.revision, current.routing, current.notes,
            current.context, frozenset(),
        ),
    ))
    return await gateway.state.work_index.project(
        worksheet.rows[0].work_id, current
    )


@pytest.fixture
async def subject(database_prerequisite):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    state, grants = PostgresState(engine), GrantState(engine)
    handle = await state.bind("asana", "123")
    principal = PrincipalContext(issuer="fixture", subject=str(uuid4()), client_id="test",
                                 assurance="test")
    grant = WorkGrant(id=uuid4(), version=1, principal=principal,
                      authority=LaunchAuthority(active_work_id=handle.id), scope="workspace",
                      operations=frozenset({"work_update", "work_relate"}), issuer="fixture",
                      provenance="disposable", expires_at=datetime.now(UTC) + timedelta(hours=1),
                      update_qualification="test:hermetic-asana",
                      relation_qualification="test:hermetic-asana")
    await grants.issue(grant, None)
    boundary = AsanaBoundary(engine)
    client = httpx.AsyncClient(base_url="https://app.asana.com/api/1.0", transport=boundary)
    gateway = UpdateGateway(state, grants, {"asana": AsanaProvider(
        client, PROJECT, test_only=True,
    )})
    yield gateway, grants, principal, grant, boundary
    await client.aclose()
    async with engine.begin() as connection:
        await connection.execute(text(
            "TRUNCATE workset_cutovers, workset_authority, work_parent_edges, "
            "workset_memberships, worksets, work_metadata_cutovers, "
            "work_metadata_authority, work_edges, work_authority_cutovers, "
            "work_authority, work_index, work_handles CASCADE"
        ))
    await engine.dispose()


async def test_real_journal_and_asana_boundary_enforce_replay_and_reconciliation(subject):
    gateway, grants, principal, grant, boundary = subject
    first = request(grant, "r1", title="Changed", notes="", completed=True)
    applied = await gateway.update(principal, first)
    assert applied.status == "ok" and applied.effect == "applied"
    assert applied.receipt.patch == first.patch
    assert applied.receipt.observed_revision == "r1"
    assert applied.receipt.resulting_revision == "r2"
    assert boundary.puts == [{"name": "Changed", "notes": "", "completed": True}]

    restarted = UpdateGateway(gateway.state, GrantState(grants.engine), gateway.providers)
    assert await restarted.update(principal, first) == applied
    assert len(boundary.puts) == 1

    boundary.mode = "lost"
    unresolved = request(grant, "r2", title="Not visible")
    unknown = await restarted.update(principal, unresolved)
    assert unknown.effect == "unknown" and len(boundary.puts) == 2
    reads = boundary.gets
    assert (await restarted.update(principal, unresolved)).effect == "unknown"
    assert boundary.gets == reads + 1 and len(boundary.puts) == 2
    changed = unresolved.model_copy(update={"patch": ScalarPatch(title="Other")})
    assert (await restarted.update(principal, changed)).reason == "operation_identity_conflict"
    blocked = await restarted.update(principal, request(grant, "r2", completed=False))
    assert blocked.reason == "target_has_unresolved_effect" and len(boundary.puts) == 2

    boundary.task["custom_fields"][1]["enum_options"].append({"gid": "broken"})
    readback = await gateway.providers["asana"].get("123"); assert readback and (readback.routing.priority, readback.routing.work_type) == ("P0", None)
    boundary.task.update(name="Not visible", modified_at="r3")
    resolved = await restarted.update(principal, unresolved)
    assert resolved.effect == "applied" and resolved.receipt.resulting_revision == "r3"
    assert len(boundary.puts) == 2

    boundary.mode = "reject"
    rejected = await restarted.update(principal, request(grant, "r3", completed=False))
    assert rejected.status == "not_applied" and rejected.effect == "not_sent"
    boundary.mode = "ok"
    reopened = await restarted.update(principal, request(grant, "r3", completed=False))
    assert reopened.effect == "applied" and boundary.puts[-1] == {"completed": False}


async def test_post_cutover_scalars_are_db_only_and_mixed_patch_is_inert(subject):
    gateway, _grants, principal, grant, boundary = subject
    current = await gateway.providers["asana"].get("123")
    assert current is not None
    await activate_stage1(gateway.state.engine, (ProviderSearchItem(
        provider_work_id="123", title=current.title, completed=current.completed,
        revision=current.revision, routing=current.routing, context=current.context,
    ),))
    try:
        projected = await gateway.state.work_index.project(grant.authority.active_work_id, current)
        changed = request(
            grant, projected.revision, title="Database title", completed=True,
        )
        result = await gateway.update(principal, changed)
        assert result.status == "ok" and result.receipt.patch.completed is True
        assert boundary.puts == []
        assert (boundary.task["name"], boundary.task["completed"]) == ("Initial", False)
        indexed = await gateway.state.work_index.get(grant.authority.active_work_id)
        assert indexed is not None
        assert (indexed.title, indexed.completed) == ("Database title", True)

        mixed = request(
            grant, result.receipt.resulting_revision,
            completed=False, notes="provider field",
        )
        denied = await gateway.update(principal, mixed)
        assert denied.status == "denied" and denied.reason == "patch_spans_authority_domains"
        assert boundary.puts == []
        indexed = await gateway.state.work_index.get(grant.authority.active_work_id)
        assert indexed is not None
        assert (indexed.title, indexed.completed) == ("Database title", True)
    finally:
        async with gateway.state.engine.begin() as connection:
            await connection.execute(text(
                "TRUNCATE work_authority_cutovers, work_authority, work_index CASCADE"
            ))


async def test_review_next_action_is_inert_before_stage2_and_reconciles_after(subject, monkeypatch):
    gateway, grants, principal, grant, boundary = subject
    before = request(grant, "r1", review_next_action="Needs Work")
    denied = await gateway.update(principal, before)
    assert (denied.status, denied.reason, boundary.puts) == (
        "denied", "review_next_action_requires_notes", [],
    )
    assert await grants.exact(before.operation_id) is None

    current = await gateway.providers["asana"].get("123")
    assert current is not None
    projected = await activate_stage2(gateway, current)
    invalid = request(grant, projected.revision, lifecycle_state="CURRENT")
    rejected = await gateway.update(principal, invalid)
    assert (rejected.status, rejected.effect, rejected.reason) == (
        "denied", "not_sent", "invalid_database_metadata",
    )
    assert await grants.exact(invalid.operation_id) is None

    relation = ProtectedRelation(
        api_version="1", operation_id=uuid4(), work_id=grant.authority.active_work_id,
        grant_version=grant.version, observed_revision=projected.revision,
        patch=RelationPatch(
            kind="dependency", action="add",
            target_work_id=grant.authority.active_work_id,
        ),
    )
    invalid_relation = await RelationGateway(
        gateway.state, grants, gateway.providers
    ).update(principal, relation)
    assert (invalid_relation.status, invalid_relation.effect, invalid_relation.reason) == (
        "denied", "not_sent", "invalid_database_dependency",
    )
    assert await grants.exact(relation.operation_id) is None

    provider = gateway.providers["asana"]
    original_get = provider.get
    reads = 0

    async def unavailable_readback(provider_work_id):
        nonlocal reads
        reads += 1
        if reads == 2:
            return None
        return await original_get(provider_work_id)

    monkeypatch.setattr(provider, "get", unavailable_readback)
    changed = request(
        grant, projected.revision, review_next_action="Needs Work",
    )
    unknown = await gateway.update(principal, changed)
    assert (unknown.status, unknown.effect, boundary.puts) == ("unknown", "unknown", [])

    monkeypatch.setattr(provider, "get", original_get)
    reconciled = await gateway.update(principal, changed)
    assert (reconciled.status, reconciled.effect, boundary.puts) == ("ok", "applied", [])
    indexed = await gateway.state.work_index.get(grant.authority.active_work_id)
    assert indexed is not None and indexed.routing.review_next_action == "Needs Work"

    async with gateway.state.engine.begin() as connection:
        await connection.execute(text(
            "TRUNCATE work_metadata_cutovers, work_metadata_authority, work_edges, "
            "work_authority_cutovers, work_authority, work_index CASCADE"
        ))


async def test_stage3_parent_and_dependency_mutations_are_db_only_and_recoverable(
    subject, monkeypatch,
):
    gateway, grants, principal, grant, boundary = subject
    current = await gateway.providers["asana"].get("123")
    assert current is not None
    parent = await gateway.state.bind("asana", "456")
    items = tuple(
        ProviderSearchItem(
            provider_work_id=provider_id, title=title, completed=False,
            revision=current.revision, routing=current.routing, context=current.context,
        )
        for provider_id, title in (("123", "Child"), ("456", "Parent"))
    )
    await activate_stage1(gateway.state.engine, items)
    worksheet = await generate_worksheet(gateway.state.engine)
    await activate_metadata(gateway.state.engine, worksheet, tuple(
        ProviderMetadataSnapshot(
            provider_id, current.revision, current.routing, current.notes,
            current.context, frozenset(),
        )
        for provider_id in ("123", "456")
    ))
    project, raced_project, second_project = uuid4(), uuid4(), uuid4()
    await stage_snapshot(gateway.state.engine, WorksetSnapshot(
        worksets=(
            Workset(project, "project.test", "Test", "PROJECT"),
            Workset(raced_project, "project.raced", "Raced", "PROJECT"),
            Workset(second_project, "project.second", "Second", "PROJECT"),
        ),
        memberships=(
            Membership(project, grant.authority.active_work_id, "AUTHORITATIVE"),
            Membership(project, parent.id, "AUTHORITATIVE"),
        ),
        parent_edges=(),
    ))
    async with gateway.state.engine.begin() as connection:
        await connection.execute(workset_authority.insert().values(
            scope="workspace", state="POSTGRES_AUTHORITY", generation=1,
        ))
        await connection.execute(workset_cutovers.insert().values(
            scope="workspace", generation=1,
        ))
    boundary.task["memberships"] = []
    current = await gateway.providers["asana"].get("123")
    assert current is not None
    gateway.providers["asana"] = cast(Provider, ReadOnlyProvider(current))

    async def revision(work_id):
        return await authoritative_revision(
            gateway.state, work_id, current.revision,
            provider_notes=current.notes, provider_context=current.context,
        )

    observed = await revision(grant.authority.active_work_id)
    relation = RelationGateway(gateway.state, grants, gateway.providers)
    parent_request = ProtectedRelation(
        api_version="1", operation_id=uuid4(), work_id=grant.authority.active_work_id,
        grant_version=grant.version, observed_revision=observed,
        patch=RelationPatch(kind="parent", action="set", target_work_id=parent.id),
    )
    original = gateway.state.worksets.update_parent

    async def commit_then_lose(*args):
        assert await original(*args)
        raise SQLAlchemyError("lost after commit")

    monkeypatch.setattr(gateway.state.worksets, "update_parent", commit_then_lose)
    unknown = await relation.update(principal, parent_request)
    assert (unknown.status, unknown.effect, boundary.puts) == ("unknown", "unknown", [])
    monkeypatch.setattr(gateway.state.worksets, "update_parent", original)
    recovered = await relation.update(principal, parent_request)
    assert recovered.status == "ok" and recovered.effect == "applied"
    assert await gateway.state.worksets.parent_matches(
        grant.authority.active_work_id, parent.id
    )
    assert (await relation.update(principal, parent_request)) == recovered

    stale = parent_request.model_copy(update={"operation_id": uuid4()})
    assert (await relation.update(principal, stale)).status == "stale"
    current_revision = await revision(grant.authority.active_work_id)
    dependency = ProtectedRelation(
        api_version="1", operation_id=uuid4(), work_id=grant.authority.active_work_id,
        grant_version=grant.version, observed_revision=current_revision,
        patch=RelationPatch(kind="dependency", action="add", target_work_id=parent.id),
    )
    assert (await relation.update(principal, dependency)).status == "ok"
    async with gateway.state.engine.connect() as connection:
        assert (await connection.execute(select(work_edges.c.work_id).where(
            work_edges.c.work_id == grant.authority.active_work_id,
            work_edges.c.depends_on_work_id == parent.id,
        ))).one()[0] == grant.authority.active_work_id
    assert boundary.puts == []

    stale_race = ProtectedRelation(
        api_version="1", operation_id=uuid4(), work_id=grant.authority.active_work_id,
        grant_version=grant.version,
        observed_revision=await revision(grant.authority.active_work_id),
        patch=RelationPatch(
            kind="placement", action="move", workset_id=second_project,
        ),
    )
    capture_versions = gateway.state.worksets.membership_move_versions

    async def race_after_observed_revision(work_id, target_workset_id):
        source, source_set_version, source_version, raced_version = await capture_versions(
            work_id, raced_project,
        )
        assert await gateway.state.worksets.move_authoritative_membership(
            work_id, source, raced_project, source_set_version,
            source_version, raced_version,
        )
        return await capture_versions(work_id, target_workset_id)

    monkeypatch.setattr(
        gateway.state.worksets, "membership_move_versions", race_after_observed_revision,
    )
    rejected_race = await relation.update(principal, stale_race)
    assert (rejected_race.status, rejected_race.effect) == ("stale", "not_sent")
    assert await gateway.state.worksets.authoritative_membership_matches(
        grant.authority.active_work_id, raced_project,
    )
    assert not await gateway.state.worksets.authoritative_membership_matches(
        grant.authority.active_work_id, second_project,
    )
    assert await grants.exact(stale_race.operation_id) is None
    monkeypatch.setattr(
        gateway.state.worksets, "membership_move_versions", capture_versions,
    )

    version_race = stale_race.model_copy(update={
        "operation_id": uuid4(),
        "observed_revision": await revision(grant.authority.active_work_id),
    })
    original_move = gateway.state.worksets.move_authoritative_membership

    async def change_source_workset_before_send(
        work_id, source, target, source_set_version, source_version, target_version,
    ):
        assert await gateway.state.worksets.update_workset(
            source, source_set_version, name="Raced after capture",
        )
        return await original_move(
            work_id, source, target, source_set_version, source_version, target_version,
        )

    monkeypatch.setattr(
        gateway.state.worksets, "move_authoritative_membership",
        change_source_workset_before_send,
    )
    rejected_version_race = await relation.update(principal, version_race)
    assert (rejected_version_race.status, rejected_version_race.effect) == (
        "stale", "not_sent",
    )
    assert await gateway.state.worksets.authoritative_membership_matches(
        grant.authority.active_work_id, raced_project,
    )
    assert not await gateway.state.worksets.authoritative_membership_matches(
        grant.authority.active_work_id, second_project,
    )
    monkeypatch.setattr(
        gateway.state.worksets, "move_authoritative_membership", original_move,
    )

    move_request = version_race.model_copy(update={
        "operation_id": uuid4(),
        "observed_revision": await revision(grant.authority.active_work_id),
    })

    async def move_then_lose(*args):
        assert await original_move(*args)
        raise SQLAlchemyError("lost after workset move commit")

    monkeypatch.setattr(
        gateway.state.worksets, "move_authoritative_membership", move_then_lose,
    )
    unknown_move = await relation.update(principal, move_request)
    assert (unknown_move.status, unknown_move.effect) == ("unknown", "unknown")
    monkeypatch.setattr(
        gateway.state.worksets, "move_authoritative_membership", original_move,
    )
    recovered_move = await relation.update(principal, move_request)
    assert (recovered_move.status, recovered_move.effect) == ("ok", "applied")
    assert await gateway.state.worksets.authoritative_membership_matches(
        grant.authority.active_work_id, second_project,
    )
    assert await relation.update(principal, move_request) == recovered_move
    stale_move = move_request.model_copy(update={"operation_id": uuid4()})
    assert (await relation.update(principal, stale_move)).status == "stale"
    legacy_placement = move_request.model_copy(update={
        "operation_id": uuid4(),
        "observed_revision": await revision(grant.authority.active_work_id),
        "patch": RelationPatch(kind="placement", action="move", project_gid=PROJECT),
    })
    denied_legacy = await relation.update(principal, legacy_placement)
    assert (denied_legacy.status, denied_legacy.reason) == (
        "denied", "legacy_provider_placement_is_read_only",
    )
    assert boundary.puts == []

    child_revision = await revision(grant.authority.active_work_id)
    cleared = await relation.update(principal, ProtectedRelation(
        api_version="1", operation_id=uuid4(), work_id=grant.authority.active_work_id,
        grant_version=grant.version, observed_revision=child_revision,
        patch=RelationPatch(kind="parent", action="clear"),
    ))
    assert cleared.status == "ok"

    other_principal = PrincipalContext(
        issuer="fixture", subject=str(uuid4()), client_id="test", assurance="test",
    )
    other_grant = grant.model_copy(update={
        "id": uuid4(), "principal": other_principal,
        "authority": LaunchAuthority(active_work_id=parent.id),
    })
    await grants.issue(other_grant, None)
    child_revision = await revision(grant.authority.active_work_id)
    parent_revision = await revision(parent.id)
    entered, ready = 0, asyncio.Event()
    validate = gateway.state.worksets.validate_parent

    async def synchronize_validation(*args):
        nonlocal entered
        await validate(*args)
        entered += 1
        if entered == 2:
            ready.set()
        await ready.wait()

    monkeypatch.setattr(gateway.state.worksets, "validate_parent", synchronize_validation)
    requests = (
        (principal, ProtectedRelation(
            api_version="1", operation_id=uuid4(), work_id=grant.authority.active_work_id,
            grant_version=grant.version, observed_revision=child_revision,
            patch=RelationPatch(kind="parent", action="set", target_work_id=parent.id),
        )),
        (other_principal, ProtectedRelation(
            api_version="1", operation_id=uuid4(), work_id=parent.id,
            grant_version=other_grant.version, observed_revision=parent_revision,
            patch=RelationPatch(
                kind="parent", action="set", target_work_id=grant.authority.active_work_id,
            ),
        )),
    )
    competing = await asyncio.wait_for(asyncio.gather(*(
        relation.update(owner, relation_request)
        for owner, relation_request in requests
    )), timeout=10)
    assert entered == 2
    assert sorted(result.status for result in competing) == ["denied", "ok"]
    denied = next(result for result in competing if result.status == "denied")
    assert denied.effect == "not_sent" and denied.reason == "invalid_database_parent"
    record = await grants.exact(denied.operation_id)
    assert record is not None and record.outcome == denied
    assert await grants.previous(uuid4(), denied.work_id) is None
    fresh_revision = await revision(parent.id)
    fresh = await relation.update(other_principal, ProtectedRelation(
        api_version="1", operation_id=uuid4(), work_id=parent.id,
        grant_version=other_grant.version, observed_revision=fresh_revision,
        patch=RelationPatch(kind="parent", action="clear"),
    ))
    assert fresh.status == "ok" and fresh.effect == "applied"
    async with gateway.state.engine.begin() as connection:
        await connection.execute(text(
            "TRUNCATE workset_cutovers, workset_authority, work_parent_edges, "
            "workset_memberships, worksets, work_metadata_cutovers, "
            "work_metadata_authority, work_edges, work_authority_cutovers, "
            "work_authority, work_index, work_handles CASCADE"
        ))


@pytest.mark.parametrize("invalid_title", [" \t ", "before\0after"])
async def test_unindexable_title_is_rejected_before_effect_preparation(
    subject, invalid_title,
):
    gateway, grants, principal, grant, boundary = subject
    invalid = request(grant, "r1", title=invalid_title)

    outcome = await gateway.update(principal, invalid)

    assert outcome.status == "denied" and outcome.reason == "title_not_indexable"
    assert boundary.puts == []
    assert await grants.exact(invalid.operation_id) is None


async def test_post_cutover_db_write_reports_unknown_on_provider_revision_race(
    subject, monkeypatch,
):
    gateway, _grants, principal, grant, boundary = subject
    current = await gateway.providers["asana"].get("123")
    assert current is not None
    await activate_stage1(gateway.state.engine, (ProviderSearchItem(
        provider_work_id="123", title=current.title, completed=current.completed,
        revision=current.revision, routing=current.routing, context=current.context,
    ),))
    try:
        projected = await gateway.state.work_index.project(
            grant.authority.active_work_id, current
        )
        provider = gateway.providers["asana"]
        original_get = provider.get
        reads = 0

        async def racing_get(provider_work_id):
            nonlocal reads
            reads += 1
            if reads == 2:
                boundary.task["modified_at"] = "r2"
            return await original_get(provider_work_id)

        monkeypatch.setattr(provider, "get", racing_get)
        changed = request(grant, projected.revision, completed=True)
        unknown = await gateway.update(principal, changed)
        assert (unknown.status, unknown.effect) == ("unknown", "unknown")
        assert boundary.puts == []
        indexed = await gateway.state.work_index.get(grant.authority.active_work_id)
        assert indexed is not None and indexed.completed is True

        reconciled = await gateway.update(principal, changed)
        assert reconciled.status == "ok" and reconciled.effect == "applied"
        assert boundary.puts == []
    finally:
        async with gateway.state.engine.begin() as connection:
            await connection.execute(text(
                "TRUNCATE work_authority_cutovers, work_authority, work_index CASCADE"
            ))


async def test_reconcile_by_operation_id_survives_grant_rotation_and_never_resends(subject):
    gateway, grants, principal, grant, boundary = subject
    boundary.mode = "lost"
    unresolved = request(grant, "r1", title="Recovered")
    unknown = await gateway.update(principal, unresolved)
    assert unknown.effect == "unknown" and len(boundary.puts) == 1

    later = request(grant, "r1", completed=True)
    blocked = await gateway.update(principal, later)
    assert (blocked.status, blocked.effect, blocked.retry) == ("unknown", "not_sent", "none")
    assert blocked.operation_id == later.operation_id and blocked.blocked_by is not None
    assert blocked.blocked_by.operation_id == unresolved.operation_id
    assert blocked.blocked_by.outcome.effect == "unknown"
    assert "effect_reconcile" in blocked.next_action

    restarted_grants = GrantState(grants.engine)
    restarted_update = UpdateGateway(gateway.state, restarted_grants, gateway.providers)
    recovery = EffectRecovery(
        restarted_grants, restarted_update,
    )
    wrong = principal.model_copy(update={"subject": str(uuid4())})
    assert (await recovery.reconcile(wrong, unresolved.operation_id)).reason == (
        "effect_recovery_not_granted"
    )

    denied_grant = grant.model_copy(update={
        "id": uuid4(), "version": 2, "operations": frozenset({"work_get"}),
    })
    await grants.issue(denied_grant, 1)
    unauthorized = await gateway.update(
        principal, request(denied_grant, "r1", completed=True),
    )
    assert unauthorized.reason == "operation_or_work_not_granted"
    assert unauthorized.blocked_by is None
    assert str(unresolved.operation_id) not in unauthorized.model_dump_json()
    assert (await recovery.reconcile(principal, unresolved.operation_id)).reason == (
        "effect_recovery_not_granted"
    )
    wrong_work = denied_grant.model_copy(update={
        "id": uuid4(), "version": 3, "scope": "launch",
        "authority": LaunchAuthority(active_work_id=uuid4()),
        "operations": frozenset({"work_update"}),
    })
    await grants.issue(wrong_work, 2)
    wrong_target = request(grant, "r1", completed=True).model_copy(update={
        "grant_version": wrong_work.version,
    })
    ungranted_target = await gateway.update(principal, wrong_target)
    assert ungranted_target.reason == "operation_or_work_not_granted"
    assert ungranted_target.blocked_by is None
    assert str(unresolved.operation_id) not in ungranted_target.model_dump_json()
    assert (await recovery.reconcile(principal, unresolved.operation_id)).reason == (
        "effect_recovery_not_granted"
    )
    current = wrong_work.model_copy(update={
        "id": uuid4(), "version": 4, "scope": "workspace",
        "authority": grant.authority,
    })
    await grants.issue(current, 3)

    async def current_principal():
        return principal
    service = ChatGPTService(
        current_principal, gateway.state, restarted_grants, gateway.providers,
    )
    reconcile_tool = dict(build_ordinary_tools(service))["effect_reconcile"]
    reads = boundary.gets
    still_unknown = await reconcile_tool("1", unresolved.operation_id)
    assert still_unknown.status == "unknown"
    assert still_unknown.inspection is not None
    assert still_unknown.inspection.intent.observed_revision == "r1"
    assert still_unknown.inspection.intent.patch == unresolved.patch
    assert still_unknown.inspection.readback == "unconfirmed"
    assert still_unknown.inspection.original_outcome.effect == "unknown"
    assert still_unknown.inspection.current_outcome.effect == "unknown"
    assert boundary.gets == reads + 1 and len(boundary.puts) == 1
    rendered = still_unknown.model_dump(mode="json")
    encoded = json.dumps(rendered)
    assert all(f'"{key}"' not in encoded for key in (
        "provider", "task_gid", "principal", "qualification",
    ))

    boundary.task.update(name="Recovered", modified_at="r2")
    applied = await reconcile_tool("1", unresolved.operation_id)
    assert applied.status == "ok" and applied.inspection is not None
    assert applied.inspection.readback == "matched"
    assert applied.inspection.current_outcome.effect == "applied"
    assert len(boundary.puts) == 1
    replay = await reconcile_tool("1", unresolved.operation_id)
    assert replay.status == "ok" and replay.inspection is not None
    assert replay.inspection.current_outcome.effect == "applied"
    assert len(boundary.puts) == 1
    async with grants.engine.connect() as connection:
        stored = (await connection.execute(select(effect_intents.c.outcome).where(
            effect_intents.c.operation_id == str(unresolved.operation_id)
        ))).scalar_one()
    assert stored["effect"] == "applied"


async def test_reconcile_denies_malformed_durable_intent(subject):
    gateway, grants, principal, grant, boundary = subject
    boundary.mode = "lost"
    unresolved = request(grant, "r1", title="Unconfirmed")
    assert (await gateway.update(principal, unresolved)).effect == "unknown"
    async with grants.engine.begin() as connection:
        await connection.execute(update(effect_intents).where(
            effect_intents.c.operation_id == str(unresolved.operation_id)
        ).values(intent={"request": {"api_version": "1"}}))
    recovery = EffectRecovery(
        grants, gateway,
    )
    result = await recovery.reconcile(principal, unresolved.operation_id)
    assert (result.status, result.reason, result.inspection) == (
        "denied", "stored_effect_intent_invalid", None,
    )
    assert len(boundary.puts) == 1
    async with grants.engine.begin() as connection:
        await connection.execute(delete(effect_intents).where(
            effect_intents.c.operation_id == str(unresolved.operation_id)
        ))


@pytest.mark.parametrize(("name", "gid", "changed_value", "old_value", "old_gid"), [
    ("priority", PRIORITY, "P1", "P0", "p0"), ("work_type", WORK_TYPE, "Implementation", "Research", "w0"),
    ("review_next_action", REVIEW, "Needs Work", "Code Review", "r0")])
async def test_strict_enum_recovers_without_resend_and_blocks_on_mismatch(subject, name, gid, changed_value, old_value, old_gid):
    gateway, grants, principal, grant, boundary = subject
    coupled = {"notes": "changed"} if name == "review_next_action" else {}
    changed = request(grant, "r1", **{name: changed_value} | coupled)
    applied = await gateway.update(principal, changed)
    assert applied.effect == "applied" and applied.receipt.patch == changed.patch
    option = {"priority": "p1", "work_type": "w1", "review_next_action": "r1"}[name]
    assert boundary.puts == [coupled | {"custom_fields": {gid: option}}]
    restarted = UpdateGateway(gateway.state, GrantState(grants.engine), gateway.providers)
    assert await restarted.update(principal, changed) == applied
    assert len(boundary.puts) == 1

    boundary.mode = "lost"
    old_notes = {"notes": "old"} if name == "review_next_action" else {}
    unresolved = request(grant, "r2", **{name: old_value} | old_notes)
    assert (await restarted.update(principal, unresolved)).effect == "unknown"
    reads = boundary.gets
    assert (await restarted.update(principal, unresolved)).effect == "unknown"
    assert boundary.gets == reads + 1 and len(boundary.puts) == 2
    blocked = await restarted.update(principal, request(grant, "r2", **{name: changed_value} | coupled))
    assert blocked.reason == "target_has_unresolved_effect" and len(boundary.puts) == 2
    target = next(field for field in boundary.task["custom_fields"] if field["gid"] == gid)
    target["display_value"], target["enum_value"] = old_value, {"gid": old_gid}
    boundary.task["modified_at"] = "r3"
    target["enum_options"].append({"gid": "broken"})
    assert (await restarted.update(principal, unresolved)).effect == "unknown"
    assert len(boundary.puts) == 2
    target["enum_options"].pop()
    if name == "review_next_action":
        assert (await restarted.update(principal, unresolved)).effect == "unknown"
        assert (await restarted.update(principal, request(grant, "r2", completed=True))).reason == "target_has_unresolved_effect"
        boundary.task["notes"] = "old"
    assert (await restarted.update(principal, unresolved)).effect == "applied"


@pytest.mark.parametrize(("patch", "payload"), [
    ({"title": "Changed"}, {"name": "Changed"}),
    ({"notes": "changed"}, {"notes": "changed"}),
    ({"completed": True}, {"completed": True}),
])
async def test_partial_patch_receipt_survives_restart(subject, patch, payload):
    gateway, grants, principal, grant, boundary = subject
    update = request(grant, "r1", **patch)
    applied = await gateway.update(principal, update)
    assert applied.status == "ok" and applied.effect == "applied"
    assert applied.receipt.patch == update.patch
    assert boundary.puts == [payload]

    restarted = UpdateGateway(gateway.state, GrantState(grants.engine), gateway.providers)
    assert await restarted.update(principal, update) == applied
    assert boundary.puts == [payload]


@pytest.mark.parametrize("field", ["title", "notes", "completed", "priority", "work_type", "review_next_action"])
def test_public_update_patch_rejects_explicit_null(field):
    with pytest.raises(ValidationError):
        ScalarPatch.model_validate({field: None})

def test_review_next_action_is_decoupled_from_provider_notes():
    assert ScalarPatch(review_next_action="Code Review").review_next_action == "Code Review"


async def test_managed_update_derives_active_identity_and_current_grant(subject):
    gateway, grants, _, grant, boundary = subject
    authority = LaunchAuthority(active_work_id=grant.authority.active_work_id)
    await rotate_managed_grant(grants, authority)
    current = await rotate_managed_grant(grants, authority)
    assert "work_relate" in current.operations
    assert current.relation_qualification == "managed:task-bound"
    server = build_server(
        Controller(authority, gateway.state, gateway.providers), authority.active_work_id,
        grants=grants, principal=managed_principal(authority.active_work_id), updates=gateway,
    )
    operation_id = uuid4()
    arguments = {
        "api_version": "1", "operation_id": str(operation_id),
        "observed_revision": "r1",
        "patch": {"review_next_action": "Needs Work", "notes": "managed"},
    }
    async with Client(server) as client:
        tool = next(tool for tool in (await client.list_tools()).tools
                    if tool.name == "work_update")
        patch_schema = tool.input_schema["$defs"]["ScalarPatch"]
        assert {"priority", "work_type", "review_next_action"} <= patch_schema["properties"].keys() and "gid" not in json.dumps(patch_schema).lower()
        assert set(tool.input_schema["properties"]) == {
            "api_version", "operation_id", "observed_revision", "patch",
        }
        first = (await client.call_tool("work_update", arguments)).structured_content
        assert first["receipt"]["work_id"] == str(authority.active_work_id)
        assert first["receipt"]["principal"] == current.principal.model_dump(mode="json")
        assert first["receipt"]["grant_version"] == current.version
        assert first["receipt"]["qualification"] == "managed:task-bound"
        assert (await client.call_tool("work_update", arguments)).structured_content == first
        assert boundary.puts == [{"notes": "managed", "custom_fields": {REVIEW: "r1"}}]
        for forbidden in ({"work_id": str(uuid4())}, {"grant_version": current.version}):
            assert (await client.call_tool("work_update", arguments | forbidden)).is_error

        denied_grant = current.model_copy(update={
            "id": uuid4(), "version": current.version + 1,
            "operations": frozenset({"work_get"}), "update_qualification": None,
        })
        await grants.issue(denied_grant, current.version)
        denied = await client.call_tool("work_update", arguments | {
            "operation_id": str(uuid4()), "observed_revision": "r2",
        })
        assert denied.structured_content["reason"] == "operation_or_work_not_granted"
        assert len(boundary.puts) == 1


@pytest.mark.parametrize("field", ["horizon", "stage3_gate"])
def test_public_update_patch_is_closed_to_scalar_fields(field):
    values = {"api_version": "1", "operation_id": uuid4(), "work_id": uuid4(),
              "grant_version": 1, "observed_revision": "r1", "patch": {field: "x"}}
    with pytest.raises(ValidationError):
        ProtectedUpdate.model_validate(values)
