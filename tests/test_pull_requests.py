from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.agent_mailboxes import AgentMailboxState
from switchstand.canonical_work import CanonicalWorkRepository, CurrentWork, canonical_revision
from switchstand.chatgpt_mcp import OrdinaryRelationPatch
from switchstand.messages import message_deliveries, messages
from switchstand.pull_requests import PullRequestChange, change, project, pull_requests


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
async def subject(canonical_database_schema: str):
    engine = create_async_engine(canonical_database_schema)
    works = CanonicalWorkRepository(engine)
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
    await engine.dispose()


async def test_links_preserve_first_owner_filter_backlinks_and_retain_orphan(subject) -> None:
    engine, _, work_ids, principal, first, second, patch = subject
    for index, work_id in enumerate(work_ids):
        async with engine.begin() as connection:
            row = await change(connection, work_id, "add", patch,
                               first if index == 0 else second, principal, uuid4())
        assert row.owner_key == first.name_key and row.row_version == 1

    async def readable(work_id):
        return work_id != work_ids[-1]

    async with engine.connect() as connection:
        page = await project(connection, work_ids[0], principal,
                             canonical_revision(work_ids[0], 1), readable, None, None)
        assert page.status == "ok" and len(page.records) == 1
        record = page.records[0]
        assert record.visibility == "PARTIAL" and record.link_has_more
        assert work_ids[-1] not in record.work_ids
        rest = await project(connection, work_ids[0], principal,
                             canonical_revision(work_ids[0], 1), readable,
                             None, record.link_next_cursor)
        assert set(record.work_ids + rest.records[0].work_ids) == set(work_ids[:-1])
        assert not rest.records[0].link_has_more
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


async def test_transfer_requires_exact_durable_recipient_acceptance(subject) -> None:
    engine, _, work_ids, principal, first, second, patch = subject
    async with engine.begin() as connection:
        await change(connection, work_ids[0], "add", patch, first, principal, uuid4())
    delivery_id, message_id = uuid4(), uuid4()
    transfer = patch.model_copy(update={
        "expected_version": 1, "recipient_key": second.name_key,
        "acceptance_delivery_id": delivery_id,
    })
    with pytest.raises(ValueError, match="acceptance"):
        async with engine.begin() as connection:
            await change(connection, work_ids[0], "set", transfer, first, principal, uuid4())
    async with engine.begin() as connection:
        await connection.execute(insert(messages).values(
            sender_work_id=second.endpoint_id, message_id=message_id,
            route_ref="agent.message", kind="request", digest="test-evidence",
            payload={"kind": "pr_owner_acceptance", "repository": patch.repository,
                     "number": 1, "previous_owner": first.name_key,
                     "expected_version": 1, "package_ref": patch.package_ref},
        ))
        await connection.execute(insert(message_deliveries).values(
            delivery_id=delivery_id, sender_work_id=second.endpoint_id, message_id=message_id,
            recipient_work_id=first.endpoint_id, state="AVAILABLE", recipient_grant_version=1,
        ))
        accepted = await change(connection, work_ids[0], "set", transfer,
                                first, principal, uuid4())
        assert accepted.owner_key == second.name_key and accepted.row_version == 2
    with pytest.raises(ValueError, match="version"):
        async with engine.begin() as connection:
            await change(connection, work_ids[0], "set", transfer, first, principal, uuid4())


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
