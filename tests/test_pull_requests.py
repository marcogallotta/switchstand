import asyncio
import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from test_canonical_service_runtime import relation
from test_canonical_service_runtime import subject as service_subject_fixture

from switchstand.agent_mailboxes import AgentMailboxResult, AgentMailboxState
from switchstand.canonical_work import CurrentWork, canonical_revision
from switchstand.chatgpt_mcp import OrdinaryRelationPatch, build_ordinary_tools
from switchstand.grants import RelationPatch, RelationReceipt
from switchstand.messages import MessageRoute, MessageState, MessageSubmitRequest
from switchstand.pull_requests import (
    PullRequestChange,
    StaleVersion,
    change,
    project,
    pull_requests,
)

canonical_subject = service_subject_fixture


async def readable(work_id):
    return True


async def task_page(connection, work_id, principal, revision, cursor):
    return await project(connection, work_id, principal, revision, readable, cursor, None)



def test_pr_identity_normalizes_and_rejects_unrelated_relation_fields() -> None:
    patch = OrdinaryRelationPatch.model_validate({
        "kind": "pull_request", "action": "add", "pull_request": {
            "repository": "Marco/Switchstand", "number": 1048, "package_ref": "work:exact",
        },
    })
    assert patch.internal().pull_request is not None
    assert patch.internal().pull_request.repository == "marco/switchstand"
    with pytest.raises(ValueError):
        OrdinaryRelationPatch.model_validate({**patch.model_dump(), "project_id": str(uuid4())})
    with pytest.raises(ValueError):
        PullRequestChange(repository="all repositories", number=0, package_ref="work:exact")


@pytest.fixture
async def subject(canonical_subject):
    engine, works = canonical_subject.engine, canonical_subject.runtime.works
    work_ids = [uuid4() for _ in range(12)]
    for work_id in work_ids:
        await works.create(CurrentWork(work_id, "PR task", False, "approved scope"))
    mailboxes = AgentMailboxState(engine)
    principal = str(uuid4())
    first = await mailboxes.register_agent(f"author-{uuid4().hex}", principal, "first")
    second = await mailboxes.register_agent(f"recipient-{uuid4().hex}", principal, "second")
    assert first.mailbox is not None and second.mailbox is not None
    patch = PullRequestChange(
        repository=f"owner-{uuid4().hex}/repository", number=1, package_ref="work:exact-package",
    )
    yield engine, works, work_ids, principal, first.mailbox, second.mailbox, patch


async def test_links_preserve_first_owner_and_retain_orphan(subject) -> None:
    engine, _, work_ids, principal, first, second, patch = subject
    for index, work_id in enumerate(work_ids):
        async with engine.begin() as connection:
            row = await change(connection, work_id, "add", patch,
                               first if index == 0 else second, principal, uuid4())
        assert row.owner_key == first.name_key and row.row_version == 1

    async with engine.connect() as connection:
        page = await task_page(connection, work_ids[0], principal,
                             canonical_revision(work_ids[0], 1), None)
        assert page.status == "ok" and len(page.records) == 1
        assert page.records[0].owner_key == first.name_key
    for work_id in work_ids:
        async with engine.begin() as connection:
            await change(connection, work_id, "remove", patch, second, principal, uuid4())
    async with engine.connect() as connection:
        retained = (await connection.execute(select(pull_requests).where(
            pull_requests.c.repository == patch.repository,
        ))).mappings().one()
        assert retained["owner_key"] == first.name_key
    async with engine.begin() as connection:
        relinked = await change(connection, work_ids[0], "add", patch, second, principal, uuid4())
        assert relinked.owner_key == first.name_key


async def test_fk_and_fencing_fail_without_leaving_an_identity(subject) -> None:
    engine, _, _, principal, first, _, patch = subject
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await change(connection, uuid4(), "add", patch, first, principal, uuid4())
    async with engine.connect() as connection:
        assert await connection.scalar(select(pull_requests.c.repository).where(
            pull_requests.c.repository == patch.repository,
        )) is None
    with pytest.raises(ValueError, match="caller"):
        async with engine.begin() as connection:
            await change(connection, uuid4(), "add", patch,
                         first.model_copy(update={"generation": first.generation + 1}),
                         principal, uuid4())


async def test_task_pr_pagination_binds_principal_and_source_revision(subject) -> None:
    engine, _, work_ids, principal, first, _, patch = subject
    for number in range(1, 13):
        async with engine.begin() as connection:
            await change(connection, work_ids[0], "add", patch.model_copy(update={"number": number}),
                         first, principal, uuid4())

    async with engine.connect() as connection:
        revision = canonical_revision(work_ids[0], 1)
        page = await task_page(connection, work_ids[0], principal, revision, None)
        assert page.has_more and len(page.records) == 10
        rest = await task_page(connection, work_ids[0], principal, revision, page.next_cursor)
        assert not rest.has_more
        assert [row.number for row in page.records + rest.records] == list(range(1, 13))
        wrong_principal = await task_page(connection, work_ids[0], "another-principal", revision,
                                        page.next_cursor)
        assert wrong_principal.status == "stale" and not wrong_principal.records
        stale_source = await task_page(connection, work_ids[0], principal,
                                     canonical_revision(work_ids[0], 2), None)
        assert stale_source.status == "stale" and not stale_source.records


async def test_concurrent_registration_converges_without_owner_reset(subject) -> None:
    engine, _, work_ids, principal, first, second, patch = subject

    async def register(work_id, caller):
        async with engine.begin() as connection:
            return await change(connection, work_id, "add", patch, caller, principal, uuid4())

    left, right = await asyncio.gather(register(work_ids[0], first), register(work_ids[1], second))
    assert left == right and left.owner_key in {first.name_key, second.name_key}
    with pytest.raises(StaleVersion):
        async with engine.begin() as connection:
            await change(connection, work_ids[0], "remove",
                         patch.model_copy(update={"expected_version": 2}), first, principal, uuid4())
    async with engine.connect() as connection:
        retained = (await connection.execute(select(pull_requests).where(
            pull_requests.c.repository == patch.repository,
        ))).mappings().one()
        assert retained["owner_key"] == left.owner_key


async def test_unlinked_remove_cannot_read_or_change_pr_metadata(subject) -> None:
    engine, _, work_ids, principal, first, second, patch = subject
    async with engine.begin() as connection:
        registered = await change(connection, work_ids[0], "add", patch, first, principal, uuid4())
    with pytest.raises(ValueError, match="not linked"):
        async with engine.begin() as connection:
            await change(connection, work_ids[1], "remove", patch, second, principal, uuid4())
    async with engine.connect() as connection:
        page = await task_page(connection, work_ids[0], principal,
                             canonical_revision(work_ids[0], 1), None)
        assert page.records[0].owner_key == registered.owner_key
        unrelated = await task_page(connection, work_ids[1], principal,
                                  canonical_revision(work_ids[1], 1), None)
        assert not unrelated.records


async def test_ordinary_relation_replay_preserves_owner_and_atomic_revision(
    canonical_subject, monkeypatch,
) -> None:
    subject = canonical_subject
    registration = await AgentMailboxState(subject.engine).register_agent(
        f"author-{uuid4().hex}", subject.principal.key, "session",
    )
    assert registration.mailbox is not None
    patch = RelationPatch(kind="pull_request", action="add", pull_request=PullRequestChange(
        repository="Owner/Repository", number=1, package_ref="work:approved",
    ))
    request = relation(subject, patch)
    from switchstand import pull_requests as pr_module

    async def fail_after_write(*args, **kwargs):
        await change(*args, **kwargs)
        raise SQLAlchemyError("injected PR transaction failure")

    monkeypatch.setattr(pr_module, "change", fail_after_write)
    interrupted = await subject.service.relate(request, caller=registration.mailbox)
    assert interrupted.effect == "unknown" and interrupted.retry == "reconcile"
    async with subject.engine.connect() as connection:
        assert await connection.scalar(select(pull_requests.c.repository)) is None
    monkeypatch.setattr(pr_module, "change", change)
    first = await subject.service.relate(request, caller=registration.mailbox)
    replay = await subject.service.relate(request, caller=registration.mailbox)
    assert first.effect == "applied" and first == replay
    assert isinstance(first.receipt, RelationReceipt)
    assert first.receipt.pull_request is not None
    assert first.receipt.pull_request.owner_key == registration.mailbox.name_key
    conflict = await subject.service.relate(request.model_copy(update={
        "patch": patch.model_copy(update={"action": "remove"}),
    }), caller=registration.mailbox)
    assert conflict.reason == "operation_identity_conflict"
    current = await subject.runtime.works.get(subject.work_id)
    assert current is not None and current.row_version == 2
    subject.service.messages = MessageState(subject.engine, subject.grants)
    tools = dict(build_ordinary_tools(subject.service, agent_identity=lambda: "session"))
    projected = await tools["work_get"]("1", subject.work_id)
    assert projected.pull_requests.records[0].owner_key == registration.mailbox.name_key
    removed = await tools["work_relate"](
        "1", uuid4(), subject.work_id, canonical_revision(subject.work_id, current.row_version),
        OrdinaryRelationPatch(kind="pull_request", action="remove", pull_request=patch.pull_request),
    )
    assert removed.effect == "applied"
    empty = await tools["work_get"]("1", subject.work_id)
    assert not empty.pull_requests.records


@pytest.mark.parametrize("failure", ["database", "identity", "not_registered"])
async def test_ordinary_pr_relation_preserves_unavailable_caller_evidence(
    canonical_subject, monkeypatch, failure,
) -> None:
    subject = canonical_subject
    subject.service.messages = MessageState(subject.engine, subject.grants)

    async def unavailable(*args):
        return AgentMailboxResult(status="recovery_required", reason="state_unavailable")

    if failure == "database":
        monkeypatch.setattr(AgentMailboxState, "for_actor", unavailable)
    tools = dict(build_ordinary_tools(
        subject.service, agent_identity=lambda: "" if failure == "identity" else "session",
    ))
    result = await tools["work_relate"](
        "1", uuid4(), subject.work_id, canonical_revision(subject.work_id, 1),
        OrdinaryRelationPatch(kind="pull_request", action="add", pull_request=PullRequestChange(
            repository="owner/repository", number=1, package_ref="work:approved",
        )),
    )
    assert result.effect == "not_sent"
    assert result.status == ("denied" if failure == "not_registered" else "unknown")
    assert result.reason == (
        "agent_not_registered" if failure == "not_registered" else "admission_state_unavailable"
    )
    async with subject.engine.connect() as connection:
        assert await connection.scalar(select(pull_requests.c.repository)) is None


def test_populated_migration_refuses_to_discard_pr_ownership(database_prerequisite) -> None:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", os.environ["TEST_DATABASE_URL"])
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    try:
        with engine.begin() as connection:
            for table in inspect(connection).get_table_names():
                name = connection.dialect.identifier_preparer.quote(table)
                connection.exec_driver_sql(f"DROP TABLE IF EXISTS {name} CASCADE")
        command.upgrade(config, "0027_pull_requests")
        with engine.begin() as connection:
            connection.execute(pull_requests.insert().values(
                repository="owner/repository", number=1, owner_key="author", row_version=1,
                acceptance_ref="operation:test",
            ))
        with pytest.raises(RuntimeError, match="preserve durable PR ownership"):
            command.downgrade(config, "0026_agent_message_ack")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) \
                == "0027_pull_requests"
            record = connection.execute(select(pull_requests)).mappings().one()
            assert record["owner_key"] == "author" and record["row_version"] == 1
        command.upgrade(config, "head")
    finally:
        engine.dispose()


async def test_backlink_cursor_invalidates_on_foreign_link_and_visibility_change(subject) -> None:
    engine, _, work_ids, principal, first, _, patch = subject
    hidden = set()

    async def readable(work_id):
        return work_id not in hidden

    for work_id in work_ids:
        async with engine.begin() as connection:
            await change(connection, work_id, "add", patch, first, principal, uuid4())
    revision = canonical_revision(work_ids[0], 1)
    async with engine.connect() as connection:
        initial = await project(connection, work_ids[0], principal, revision, readable, None, None)
        cursor = initial.records[0].link_next_cursor
        assert cursor is not None
        hidden.add(work_ids[-1])
        stale = await project(connection, work_ids[0], principal, revision, readable, None, cursor)
        assert stale.status == "stale" and not stale.records
        filtered = await project(connection, work_ids[0], principal, revision, readable, None, None)
        assert filtered.records[0].visibility == "PARTIAL"
        assert str(work_ids[-1]) not in filtered.model_dump_json()
        cursor = filtered.records[0].link_next_cursor
        assert cursor is not None
    async with engine.begin() as connection:
        await change(connection, work_ids[1], "remove", patch, first, principal, uuid4())
    async with engine.connect() as connection:
        stale = await project(connection, work_ids[0], principal, revision, readable, None, cursor)
        assert stale.status == "stale" and not stale.records



async def test_backlinks_page_without_disclosing_unreadable_work(subject) -> None:
    engine, _, work_ids, principal, first, _, patch = subject
    for work_id in work_ids:
        async with engine.begin() as connection:
            await change(connection, work_id, "add", patch, first, principal, uuid4())
    revision = canonical_revision(work_ids[0], 1)
    async with engine.connect() as connection:
        initial = await project(connection, work_ids[0], principal, revision, readable, None, None)
        view = initial.records[0]
        assert view.visibility == "FULL" and view.link_has_more and len(view.work_ids) == 10
        rest = await project(connection, work_ids[0], principal, revision, readable,
                             None, view.link_next_cursor)
        assert set(view.work_ids + rest.records[0].work_ids) == set(work_ids)
        assert not rest.records[0].link_has_more
        mixed = await project(connection, work_ids[0], principal, revision, readable,
                              view.link_next_cursor, None)
        assert mixed.status == "stale"


async def test_accepted_transfer_requires_exact_current_recipient_evidence(subject, canonical_subject):
    engine, _, work_ids, principal, first, second, patch = subject
    async with engine.begin() as connection:
        await change(connection, work_ids[0], "add", patch, first, principal, uuid4())
    transfer = patch.model_copy(update={
        "expected_version": 1, "recipient_key": second.name_key,
    })
    with pytest.raises(ValueError, match="acceptance"):
        async with engine.begin() as connection:
            await change(connection, work_ids[0], "set", transfer, first, principal, uuid4())
    payload = {
        "kind": "pr_owner_acceptance", "repository": patch.repository,
        "number": patch.number, "previous_owner": first.name_key,
        "expected_version": 1, "package_ref": patch.package_ref,
        "recipient_key": second.name_key, "recipient_generation": second.generation,
        "previous_owner_generation": first.generation,
    }
    messages = MessageState(engine, canonical_subject.grants)
    acceptance = await messages.submit_admitted(
        second.endpoint_id, MessageRoute(recipient_work_id=first.endpoint_id,
                                        recipient_grant_version=first.generation),
        MessageSubmitRequest(api_version="1", message_id=uuid4(), grant_version=second.generation,
                             route_ref="pr.owner.acceptance", kind="request", payload=payload),
        agent_binding=second,
    )
    assert acceptance.status == "ok" and acceptance.message is not None
    transfer = transfer.model_copy(update={"acceptance_delivery_id": acceptance.message.delivery_id})
    for work_id, actor, proposed in (
        (work_ids[1], first, transfer),
        (work_ids[0], second, transfer),
        (work_ids[0], first, transfer.model_copy(update={"package_ref": "wrong"})),
    ):
        with pytest.raises(ValueError):
            async with engine.begin() as connection:
                await change(connection, work_id, "set", proposed, actor, principal, uuid4())
    replacement = await AgentMailboxState(engine).takeover(second.name, principal, "replacement")
    assert replacement.status == "ok" and replacement.mailbox is not None
    with pytest.raises(ValueError, match="acceptance"):
        async with engine.begin() as connection:
            await change(connection, work_ids[0], "set", transfer, first, principal, uuid4())
    second = replacement.mailbox
    payload["recipient_generation"] = second.generation
    acceptance = await messages.submit_admitted(
        second.endpoint_id, MessageRoute(recipient_work_id=first.endpoint_id,
                                        recipient_grant_version=first.generation),
        MessageSubmitRequest(api_version="1", message_id=uuid4(), grant_version=second.generation,
                             route_ref="pr.owner.acceptance", kind="request", payload=payload),
        agent_binding=second,
    )
    assert acceptance.status == "ok" and acceptance.message is not None
    transfer = transfer.model_copy(update={"acceptance_delivery_id": acceptance.message.delivery_id})

    async def attempt():
        try:
            async with engine.begin() as connection:
                return await change(connection, work_ids[0], "set", transfer,
                                    first, principal, uuid4())
        except StaleVersion:
            return None

    contenders = await asyncio.gather(attempt(), attempt())
    assert sum(row is not None for row in contenders) == 1
    changed = next(row for row in contenders if row is not None)
    assert changed.owner_key == second.name_key and changed.row_version == 2
    assert changed.acceptance_ref == f"delivery:{acceptance.message.delivery_id}"
    with pytest.raises(StaleVersion):
        async with engine.begin() as connection:
            await change(connection, work_ids[0], "set", transfer, first, principal, uuid4())
    async with engine.connect() as connection:
        recovered = await task_page(connection, work_ids[0], principal,
                                    canonical_revision(work_ids[0], 1), None)
        assert recovered.records[0].owner_key == second.name_key
        assert recovered.records[0].acceptance_ref == changed.acceptance_ref
