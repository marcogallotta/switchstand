import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.contracts import Routing, WorkContext
from switchstand.core import Handle, ProviderError, ProviderWork
from switchstand.discovery import ProviderSearchItem, ProviderSearchPage
from switchstand.provider import FIELDS, PROJECT, AsanaProvider, ProviderWorkDecodeError
from switchstand.work_corpus import (
    CorpusCaptureDecodeError,
    _capture,
    capture_manifest,
    capture_preflight_manifest,
    capture_to_path,
    compare_manifests,
    compare_parity_exports,
    failure_receipt_path,
    load_manifest,
    parity_manifest,
    run,
    write_manifest,
)

SHA = "3a04669a9f7a5c094bd7617c55003ed47a098f80"


@pytest.fixture
async def index(database_prerequisite):
    del database_prerequisite
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for work-corpus tests")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    await engine.dispose()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_async_engine(url)
    yield type("Index", (), {"engine": engine})()
    await engine.dispose()


def search_item(gid: str, revision: str | None = None) -> ProviderSearchItem:
    return ProviderSearchItem(
        gid,
        f"Task {gid}",
        False,
        revision or f"revision-{gid}",
        Routing(priority="P1"),
        WorkContext(assignee="Marco"),
    )


def provider_work(gid: str, *, canonical: bool = True) -> ProviderWork:
    item = search_item(gid)
    return ProviderWork(
        item.title,
        f"notes-{gid}",
        item.completed,
        item.revision,
        item.routing,
        item.context,
        canonical,
    )


class FakeProvider:
    def __init__(
        self,
        pages: dict[str | None, ProviderSearchPage],
        exact: dict[str, ProviderWork | None],
        dependencies: dict[str, frozenset[str]] | None = None,
    ):
        self.pages = pages
        self.exact = exact
        self.dependencies = dependencies or {}

    async def search_work(self, _text, _completed, cursor, _limit):
        return self.pages[cursor]

    async def get(self, provider_work_id):
        return self.exact[provider_work_id]

    async def has_zero_memberships(self, _provider_work_id):
        return False

    async def dependencies_for_import(self, provider_work_id):
        return self.dependencies.get(provider_work_id, frozenset())


async def insert_handles(engine, *handles: Handle) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO work_handles (id, provider, provider_work_id) "
                "VALUES (:id, :provider, :provider_work_id)"
            ),
            [
                {
                    "id": handle.id,
                    "provider": handle.provider,
                    "provider_work_id": handle.provider_work_id,
                }
                for handle in handles
            ],
        )


async def test_stable_union_includes_readable_bound_continuity_targets(index):
    grant = Handle(UUID(int=11), "asana", "grant-target")
    unknown = Handle(UUID(int=12), "asana", "unknown-target")
    dependency = Handle(UUID(int=13), "asana", "dependency-target")
    mailbox = Handle(UUID(int=14), "agent-mailbox", "Coordinator")
    await insert_handles(index.engine, grant, unknown, dependency, mailbox)
    provider = FakeProvider(
        {None: ProviderSearchPage((search_item("broad"),), None)},
        {
            gid: provider_work(gid)
            for gid in ("grant-target", "unknown-target", "dependency-target")
        },
        {"broad": frozenset({"dependency-target"})},
    )

    manifest = await capture_manifest(index.engine, provider, SHA)

    rows = {row["provider_work_id"]: row for row in manifest["rows"]}
    assert set(rows) == {"broad", "grant-target", "unknown-target", "dependency-target"}
    assert rows["broad"]["dependencies"] == ["dependency-target"]
    assert rows["grant-target"]["work_id"] == str(grant.id)
    assert "Coordinator" not in str(manifest)
    assert manifest["counts"] == {
        "broad": 1, "bound": 3, "included": 4, "exceptions": 0, "retired": 0,
    }


async def test_capture_reports_bounded_scan_progress(index):
    items = tuple(search_item(str(number)) for number in range(51))
    progress: list[str] = []

    await capture_preflight_manifest(
        index.engine,
        FakeProvider({None: ProviderSearchPage(items, None)}, {}),
        SHA,
        progress=progress.append,
    )

    assert progress == [
        "broad-scan:start", "broad-scan:progress items=51", "broad-scan:complete items=51",
        "bound-only:start items=0",
        "bound-only:complete included=51 exceptions=0", "dependencies:start items=51",
        "dependencies:progress items=50/51", "dependencies:progress items=51/51",
        "capture:complete",
    ]


async def test_missing_and_noncanonical_bound_work_are_explicit_exceptions(index):
    missing = Handle(UUID(int=21), "asana", "missing")
    moved = Handle(UUID(int=22), "asana", "moved")
    await insert_handles(index.engine, missing, moved)
    provider = FakeProvider(
        {None: ProviderSearchPage((search_item("broad"),), None)},
        {"missing": None, "moved": provider_work("moved", canonical=False)},
    )

    manifest = await capture_manifest(index.engine, provider, SHA)

    assert manifest["exceptions"] == [
        {"provider_work_id": "missing", "work_id": str(missing.id), "reason": "missing"},
        {"provider_work_id": "moved", "work_id": str(moved.id), "reason": "noncanonical"},
    ]
    progress: list[str] = []
    preflight = await capture_preflight_manifest(index.engine, provider, SHA, progress=progress.append)
    assert preflight["handle_classifications"] == [
        {"provider_work_id": "missing", "work_id": str(missing.id),
         "state": "historical-missing", "reason": "missing"},
        {"provider_work_id": "moved", "work_id": str(moved.id),
         "state": "unresolved", "reason": "noncanonical"},
    ]
    assert "bound-only:progress items=2/2" in progress


async def test_preflight_classifies_bound_decode_failure_and_continues(index):
    provider_work_id = "1218467001205757"
    bound = Handle(UUID(int=23), "asana", provider_work_id)
    await insert_handles(index.engine, bound)

    class DecodeFailure(FakeProvider):
        async def get(self, _provider_work_id):
            raise ProviderWorkDecodeError("priority_truth")

    manifest = await capture_preflight_manifest(
        index.engine,
        DecodeFailure({None: ProviderSearchPage((), None)}, {}),
        SHA,
    )

    assert manifest["handle_classifications"] == [{
        "provider_work_id": provider_work_id,
        "work_id": str(bound.id),
        "state": "unresolved",
        "reason": "decode:priority_truth",
    }]
    assert manifest["counts"] == {
        "task_handles": 1,
        "current": 0,
        "historical-missing": 0,
        "retired": 0,
        "unresolved": 1,
    }


async def test_zero_membership_retires_bound_decode_failure(index):
    provider_work_id = "1218467001205757"
    bound = Handle(UUID(int=24), "asana", provider_work_id)
    await insert_handles(index.engine, bound)

    class OrphanedDecodeFailure(FakeProvider):
        async def get(self, _provider_work_id):
            raise ProviderWorkDecodeError("priority_truth")

        async def has_zero_memberships(self, _provider_work_id):
            return True

    provider = OrphanedDecodeFailure({None: ProviderSearchPage((), None)}, {})
    corpus = await capture_manifest(index.engine, provider, SHA)
    assert corpus["exceptions"] == [{
        "provider_work_id": provider_work_id,
        "work_id": str(bound.id),
        "reason": "zero-membership",
    }]
    assert corpus["counts"]["retired"] == 1

    preflight = await capture_preflight_manifest(index.engine, provider, SHA)
    assert preflight["handle_classifications"] == [{
        "provider_work_id": provider_work_id,
        "work_id": str(bound.id),
        "state": "retired",
        "reason": "zero-membership",
    }]
    assert preflight["counts"] == {
        "task_handles": 1,
        "current": 0,
        "historical-missing": 0,
        "retired": 1,
        "unresolved": 0,
    }


async def test_zero_membership_retires_readable_parent_inherited_task(index):
    bound = Handle(UUID(int=25), "asana", "inherited")
    await insert_handles(index.engine, bound)

    class ParentInherited(FakeProvider):
        async def has_zero_memberships(self, provider_work_id):
            return provider_work_id == "inherited"

    manifest = await capture_manifest(
        index.engine,
        ParentInherited(
            {None: ProviderSearchPage((), None)},
            {"inherited": provider_work("inherited", canonical=True)},
        ),
        SHA,
    )

    assert manifest["rows"] == []
    assert manifest["exceptions"] == [{
        "provider_work_id": "inherited", "work_id": str(bound.id),
        "reason": "zero-membership",
    }]


async def test_manifest_reports_db_residue_needed_for_cutover_preflight(index, tmp_path: Path):
    sender = UUID(int=41)
    message_id = UUID(int=42)
    projection_id = UUID(int=43)
    operation_id = UUID(int=44)
    event_id = UUID(int=45)
    effect_id = UUID(int=46)
    async with index.engine.begin() as connection:
        await connection.execute(text(
            "INSERT INTO work_handles (id, provider, provider_work_id) "
            "VALUES (:id, 'asana', 'task')"
        ), {"id": sender})
        await connection.execute(text(
            "INSERT INTO work_event_handles "
            "(id, work_id, provider, provider_work_id, provider_event_id) "
            "VALUES (:id, :work_id, 'asana', 'task', 'story')"
        ), {"id": event_id, "work_id": sender})
        await connection.execute(text(
            "INSERT INTO effect_intents "
            "(operation_id, fingerprint, principal_key, work_id, grant_id, grant_version, "
            " intent, outcome) VALUES "
            "(:operation_id, 'fingerprint', 'principal', :work_id, :grant_id, 1, "
            " '{}'::jsonb, CAST(:outcome AS jsonb))"
        ), {
            "operation_id": str(effect_id), "work_id": str(sender),
            "grant_id": str(UUID(int=47)),
            "outcome": json.dumps({
                "effect": "unknown", "operation": "work_update", "reason": "ambiguous"
            }),
        })
        await connection.execute(text(
            "INSERT INTO messages "
            "(sender_work_id, message_id, route_ref, kind, payload, digest) "
            "VALUES (:sender, :message, 'route', 'request', '{}'::jsonb, 'digest')"
        ), {"sender": sender, "message": message_id})
        await connection.execute(text(
            "INSERT INTO message_projection "
            "(projection_id, sender_work_id, message_id, operation_id, provider, target) "
            "VALUES (:projection, :sender, :message, :operation, 'asana', 'task')"
        ), {
            "projection": projection_id, "sender": sender,
            "message": message_id, "operation": operation_id,
        })

    manifest = await capture_preflight_manifest(
        index.engine,
        FakeProvider(
            {None: ProviderSearchPage((search_item("task"),), None)},
            {},
        ),
        SHA,
    )

    assert manifest["event_aliases"] == {
        "total": 1, "by_provider": [{"provider": "asana", "count": 1}]
    }
    assert manifest["unknown_effects"] == [{
        "operation_id": str(effect_id), "work_id": str(sender),
        "operation": "work_update", "reason": "ambiguous",
    }]
    assert manifest["message_projections"] == [{
        "projection_id": str(projection_id), "sender_work_id": str(sender),
        "message_id": str(message_id), "operation_id": str(operation_id),
        "provider": "asana", "target": "task", "state": "PENDING", "receipt": None,
    }]
    assert manifest == await capture_preflight_manifest(index.engine, FakeProvider(
        {None: ProviderSearchPage((search_item("task"),), None)}, {}
    ), SHA)
    path = tmp_path / "preflight.json"
    write_manifest(path, manifest)
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_manifest(path) == manifest


async def test_pagination_rejects_duplicate_and_repeated_cursor(index):
    duplicate = FakeProvider(
        {
            None: ProviderSearchPage((search_item("1"),), "next"),
            "next": ProviderSearchPage((search_item("1"),), None),
        },
        {},
    )
    with pytest.raises(ValueError, match="duplicate"):
        await capture_manifest(index.engine, duplicate, SHA)

    repeated = FakeProvider(
        {
            None: ProviderSearchPage((search_item("1"),), "next"),
            "next": ProviderSearchPage((search_item("2"),), "next"),
        },
        {},
    )
    with pytest.raises(ValueError, match="cursor"):
        await capture_manifest(index.engine, repeated, SHA)


async def test_provider_failure_aborts_instead_of_becoming_exception(index):
    bound = Handle(UUID(int=31), "asana", "failed")
    await insert_handles(index.engine, bound)

    class Failed(FakeProvider):
        async def get(self, _provider_work_id):
            raise ProviderError("down")

    provider = Failed({None: ProviderSearchPage((), None)}, {})
    with pytest.raises(ProviderError):
        await capture_manifest(index.engine, provider, SHA)


async def test_bound_only_real_provider_decode_failure_writes_private_receipt(
    tmp_path: Path, caplog,
):
    result = MagicMock()
    result.all.return_value = [("secret-bound-id", UUID(int=32))]
    connection = AsyncMock()
    connection.execute.return_value = result
    engine = MagicMock()
    engine.connect.return_value.__aenter__.return_value = connection
    payload = {
        "data": {
            "gid": "secret-bound-id",
            "name": "secret-title",
            "notes": "secret-notes",
            "completed": False,
            "modified_at": "r1",
            "memberships": [{
                "project": {"gid": PROJECT, "name": "secret-project"},
                "section": None,
            }],
            "assignee": None,
            "parent": None,
            "custom_fields": [{
                "gid": FIELDS["priority"],
                "enabled": True,
                "resource_subtype": "enum",
                "display_value": "secret-priority",
            }],
        }
    }

    class BoundOnlyProvider(AsanaProvider):
        async def search_work(self, _text, _completed, _cursor, _limit):
            return ProviderSearchPage((), None)

        async def dependencies_for_import(self, _provider_work_id):
            return frozenset()

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0",
        transport=httpx.MockTransport(respond),
    )
    provider = BoundOnlyProvider(client)
    manifest_path = tmp_path / "corpus.json"
    try:
        with caplog.at_level("ERROR"), pytest.raises(CorpusCaptureDecodeError) as rejected:
            await capture_to_path(engine, provider, manifest_path, SHA)
    finally:
        await client.aclose()

    receipt_path = failure_receipt_path(manifest_path)
    assert not manifest_path.exists()
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(receipt_path.read_text()) == {
        "provider_work_id": "secret-bound-id",
        "reason": "priority_truth",
    }
    assert str(rejected.value) == "provider response invalid"
    assert all(secret not in caplog.text for secret in (
        "secret-bound-id", "secret-title", "secret-notes", "secret-project",
        "secret-priority", "priority_truth",
    ))


async def test_manifest_is_deterministic_create_new_and_revision_sensitive(index, tmp_path: Path):
    provider = FakeProvider(
        {
            None: ProviderSearchPage((search_item("2"), search_item("1")), None),
        },
        {},
    )
    first = await capture_manifest(index.engine, provider, SHA)
    second = await capture_manifest(index.engine, provider, SHA)
    assert first == second
    assert [row["provider_work_id"] for row in first["rows"]] == ["1", "2"]

    first_path, second_path = tmp_path / "first.json", tmp_path / "second.json"
    write_manifest(first_path, first)
    write_manifest(second_path, second)
    assert first_path.stat().st_mode & 0o777 == 0o600
    assert compare_manifests(first_path, second_path) == first["sha256"]
    assert load_manifest(first_path) == first
    with pytest.raises(FileExistsError):
        write_manifest(first_path, first)

    changed = await capture_manifest(
        index.engine,
        FakeProvider(
            {
                None: ProviderSearchPage((search_item("1", "changed"), search_item("2")), None),
            },
            {},
        ),
        SHA,
    )
    changed_path = tmp_path / "changed.json"
    write_manifest(changed_path, changed)
    with pytest.raises(ValueError, match="do not match"):
        compare_manifests(first_path, changed_path)


def test_manifest_rejects_tampering(tmp_path: Path):
    path = tmp_path / "manifest.json"
    path.write_text('{"schema_version":1,"sha256":"bad"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        load_manifest(path)


def test_parity_compares_every_record_and_field_without_order_dependence(tmp_path: Path):
    work = {
        "kind": "work", "id": "work-1",
        "fields": {"title": "Exact title", "completed": False, "metadata": {"priority": "P1"}},
    }
    event = {
        "kind": "event", "id": "event-1",
        "fields": {"sequence": 1, "text": "Exact text", "actor": None},
    }
    source, target = tmp_path / "source.json", tmp_path / "target.json"
    write_manifest(source, parity_manifest([work, event]))
    write_manifest(target, parity_manifest([event, work]))

    result = compare_parity_exports(source, target)

    assert result.records == 2
    assert len(result.digest) == 64


@pytest.mark.parametrize(
    ("target_records", "message"),
    [
        ([{"kind": "work", "id": "work-2", "fields": {"title": "same"}}], "identities"),
        ([{"kind": "work", "id": "work-1", "fields": {"title": "changed"}}], "fields"),
    ],
)
def test_parity_rejects_missing_extra_or_changed_data(
    tmp_path: Path, target_records: list[dict[str, object]], message: str,
):
    source, target = tmp_path / "source.json", tmp_path / "target.json"
    write_manifest(source, parity_manifest([
        {"kind": "work", "id": "work-1", "fields": {"title": "same"}},
    ]))
    write_manifest(target, parity_manifest(target_records))

    with pytest.raises(ValueError, match=message):
        compare_parity_exports(source, target)


@pytest.mark.parametrize(("source_value", "target_value"), [(True, 1), (1, 1.0)])
def test_parity_preserves_json_scalar_types(
    tmp_path: Path, source_value: object, target_value: object,
):
    source, target = tmp_path / "source.json", tmp_path / "target.json"
    write_manifest(source, parity_manifest([
        {"kind": "work", "id": "work-1", "fields": {"value": source_value}},
    ]))
    write_manifest(target, parity_manifest([
        {"kind": "work", "id": "work-1", "fields": {"value": target_value}},
    ]))

    with pytest.raises(ValueError, match="fields differ"):
        compare_parity_exports(source, target)


def test_parity_rejects_duplicate_identity_and_non_object_fields():
    row: dict[str, object] = {"kind": "work", "id": "work-1", "fields": {}}
    with pytest.raises(ValueError, match="duplicate"):
        parity_manifest([row, row])
    with pytest.raises(TypeError, match="fields"):
        parity_manifest([{"kind": "work", "id": "work-1", "fields": []}])


def test_cli_reports_verified_parity_count_and_digest(tmp_path: Path, capsys):
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    document = parity_manifest([{"kind": "work", "id": "work-1", "fields": {}}])
    write_manifest(first, document)
    write_manifest(second, document)

    run(["parity", str(first), str(second)])

    assert capsys.readouterr().out.startswith("parity_records=1 parity_sha256=")


async def test_cli_capture_passes_test_project_to_provider(monkeypatch, tmp_path: Path):
    import switchstand.work_corpus as module

    async def fake_capture(*_args):
        return {"sha256": "digest"}

    seen = []
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused")
    monkeypatch.setenv("ASANA_TOKEN", "unused")
    monkeypatch.setenv("SWITCHSTAND_TEST_PROJECT_GID", "9999999999999999")
    monkeypatch.setattr(module, "AsanaProvider", lambda _client, project: seen.append(project))
    monkeypatch.setattr(module, "capture_manifest", fake_capture)
    assert await _capture(tmp_path / "manifest", SHA) == "digest"
    assert seen == ["9999999999999999"]
