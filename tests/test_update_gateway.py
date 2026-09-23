import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from mcp import Client
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.contracts import LaunchAuthority
from switchstand.core import Controller
from switchstand.grant_state import GrantState, effect_intents
from switchstand.grants import PrincipalContext, ProtectedUpdate, ScalarPatch, WorkGrant
from switchstand.managed_identity import managed_principal, rotate_managed_grant
from switchstand.mcp import build_server
from switchstand.provider import AsanaProvider
from switchstand.state import PostgresState, metadata
from switchstand.updates import UpdateGateway

PROJECT = "9999999999999999"


class AsanaBoundary(httpx.AsyncBaseTransport):
    def __init__(self, engine):
        self.engine, self.puts, self.gets = engine, [], 0
        self.mode = "commit_lost"
        self.task = {"gid": "123", "name": "Initial", "notes": "old", "completed": False,
                     "modified_at": "r1", "memberships": [{"project": {"gid": PROJECT}}],
                     "parent": None, "custom_fields": []}

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
            self.task.update(payload)
            self.task["modified_at"] = f"r{len(self.puts) + 1}"
        if self.mode in {"commit_lost", "lost"}:
            raise httpx.ReadError("response lost", request=request)
        return httpx.Response(200, request=request, json={"data": self.task})


def request(grant, revision, **patch):
    return ProtectedUpdate(api_version="1", operation_id=uuid4(),
                           work_id=grant.authority.active_work_id,
                           grant_version=grant.version, observed_revision=revision,
                           patch=ScalarPatch(**patch))


@pytest.fixture
async def subject():
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
                      operations=frozenset({"work_update"}), issuer="fixture",
                      provenance="disposable", expires_at=datetime.now(UTC) + timedelta(hours=1),
                      update_qualification="test:hermetic-asana")
    await grants.issue(grant, None)
    boundary = AsanaBoundary(engine)
    client = httpx.AsyncClient(base_url="https://app.asana.com/api/1.0", transport=boundary)
    gateway = UpdateGateway(state, grants, {"asana": AsanaProvider(
        client, PROJECT, test_only=True,
    )})
    yield gateway, grants, principal, grant, boundary
    await client.aclose()
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


@pytest.mark.parametrize("field", ["title", "notes", "completed"])
def test_public_update_patch_rejects_explicit_null(field):
    with pytest.raises(ValidationError):
        ScalarPatch.model_validate({field: None})


async def test_managed_update_derives_active_identity_and_current_grant(subject):
    gateway, grants, _, grant, boundary = subject
    authority = LaunchAuthority(active_work_id=grant.authority.active_work_id)
    await rotate_managed_grant(grants, authority)
    current = await rotate_managed_grant(grants, authority)
    server = build_server(
        Controller(authority, gateway.state, gateway.providers), authority.active_work_id,
        grants=grants, principal=managed_principal(authority.active_work_id), updates=gateway,
    )
    operation_id = uuid4()
    arguments = {
        "api_version": "1", "operation_id": str(operation_id),
        "observed_revision": "r1",
        "patch": {"title": "Managed", "notes": "managed", "completed": True},
    }
    async with Client(server) as client:
        tool = next(tool for tool in (await client.list_tools()).tools
                    if tool.name == "work_update")
        assert set(tool.input_schema["properties"]) == {
            "api_version", "operation_id", "observed_revision", "patch",
        }
        first = (await client.call_tool("work_update", arguments)).structured_content
        assert first["receipt"]["work_id"] == str(authority.active_work_id)
        assert first["receipt"]["principal"] == current.principal.model_dump(mode="json")
        assert first["receipt"]["grant_version"] == current.version
        assert first["receipt"]["qualification"] == "managed:task-bound"
        assert (await client.call_tool("work_update", arguments)).structured_content == first
        assert boundary.puts == [{"name": "Managed", "notes": "managed", "completed": True}]
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


@pytest.mark.parametrize("field", ["horizon", "review_next_action", "stage3_gate"])
def test_public_update_patch_is_closed_to_scalar_fields(field):
    values = {"api_version": "1", "operation_id": uuid4(), "work_id": uuid4(),
              "grant_version": 1, "observed_revision": "r1", "patch": {field: "x"}}
    with pytest.raises(ValidationError):
        ProtectedUpdate.model_validate(values)
