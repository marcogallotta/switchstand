import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.agent_mailboxes import AgentMailboxState
from switchstand.canonical_work import CanonicalWorkRepository, CurrentWork, canonical_revision
from switchstand.chatgpt_mcp import OrdinaryRelationPatch
from switchstand.pull_requests import (
    PullRequestChange,
    StaleVersion,
    change,
    project,
    pull_requests,
)


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

    async def readable(work_id):
        return work_id in work_ids

    async with engine.connect() as connection:
        revision = canonical_revision(work_ids[0], 1)
        page = await project(connection, work_ids[0], principal, revision, readable, None, None)
        assert page.has_more and len(page.records) == 10
        rest = await project(connection, work_ids[0], principal, revision, readable,
                             page.next_cursor, None)
        assert not rest.has_more
        assert [row.number for row in page.records + rest.records] == list(range(1, 13))
        wrong_principal = await project(connection, work_ids[0], "another-principal", revision,
                                        readable, page.next_cursor, None)
        assert wrong_principal.status == "stale" and not wrong_principal.records
        stale_source = await project(connection, work_ids[0], principal,
                                     canonical_revision(work_ids[0], 2), readable, None, None)
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
