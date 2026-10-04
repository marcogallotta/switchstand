import hashlib
import json
import os
import stat
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from switchstand.cutover_cleanup import (  # pyright: ignore[reportPrivateUsage]
    _receipt_matches,
    apply_cleanup,
    apply_cleanup_connection,
    cleanup_snapshot,
    require_cleanup_equality,
)
from switchstand.grant_state import effect_intents
from switchstand.grants import (
    EffectReceipt,
    GuardOutcome,
    PrincipalContext,
    ScalarPatch,
    UpdateReceipt,
)
from switchstand.messages import message_deliveries, message_projection, messages
from switchstand.state import metadata
from switchstand.work_corpus import load_manifest, write_manifest

WORK = UUID("10000000-0000-4000-8000-000000000001")
REPLACE = UUID("20000000-0000-4000-8000-000000000002")
DELETE = UUID("30000000-0000-4000-8000-000000000003")
GRANT = UUID("40000000-0000-4000-8000-000000000004")
MESSAGE = UUID("50000000-0000-4000-8000-000000000005")
DELIVERY = UUID("60000000-0000-4000-8000-000000000006")
PROJECTION = UUID("70000000-0000-4000-8000-000000000007")
SENDER = UUID("80000000-0000-4000-8000-000000000008")
RECIPIENT = UUID("90000000-0000-4000-8000-000000000009")
PRINCIPAL = PrincipalContext(
    issuer="switchstand", subject="worker", client_id="client", assurance="authenticated",
)


@pytest.fixture
async def engine(database_prerequisite: None) -> AsyncGenerator[AsyncEngine]:
    del database_prerequisite
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
        await connection.run_sync(metadata.create_all)
    yield engine
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


def unknown(operation_id: UUID) -> dict[str, object]:
    return GuardOutcome(
        status="unknown", operation="work_append", work_id=WORK,
        operation_id=operation_id, reason="ambiguous_provider_send", effect="unknown",
        retry="reconcile", next_action="Do not resend.",
    ).model_dump(mode="json", exclude_none=True)


async def seed(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        for operation_id in (REPLACE, DELETE):
            await connection.execute(insert(effect_intents).values(
                operation_id=str(operation_id), fingerprint=f"fingerprint-{operation_id}",
                principal_key=PRINCIPAL.key, work_id=str(WORK), grant_id=str(GRANT),
                grant_version=1, intent={
                    "request": {"text": "exact text"}, "provider": "asana",
                    "task_gid": "1218", "qualification": "trusted_frozen_cutover_readback",
                },
                outcome=unknown(operation_id),
            ))
        await connection.execute(insert(messages).values(
            sender_work_id=SENDER, message_id=MESSAGE, route_ref="review", kind="request",
            payload={"instruction": "receive only"}, digest="message-digest",
        ))
        await connection.execute(insert(message_deliveries).values(
            delivery_id=DELIVERY, sender_work_id=SENDER, message_id=MESSAGE,
            recipient_work_id=RECIPIENT, state="AVAILABLE", recipient_grant_version=1,
        ))
        await connection.execute(insert(message_projection).values(
            projection_id=PROJECTION, sender_work_id=SENDER, message_id=MESSAGE,
            operation_id=PROJECTION, provider="asana", target="1218", state="PENDING",
        ))


def applied() -> dict[str, object]:
    return GuardOutcome(
        status="ok", operation="work_append", work_id=WORK, operation_id=REPLACE,
        reason="trusted_frozen_cutover_readback", effect="applied", retry="none",
        next_action="Use the archived exact receipt.", receipt=EffectReceipt(
            operation_id=REPLACE, principal=PRINCIPAL, grant_id=GRANT, grant_version=1,
            work_id=WORK, provider="asana", task_gid="1218", story_gid="9911",
            text="exact text", qualification="trusted_frozen_cutover_readback",
        ),
    ).model_dump(mode="json", exclude_none=True)


def test_update_receipt_matches_legacy_serialized_null_defaults():
    receipt = UpdateReceipt(
        operation_id=REPLACE, principal=PRINCIPAL, grant_id=GRANT, grant_version=1,
        work_id=WORK, provider="asana", task_gid="1218", observed_revision="v1",
        resulting_revision="v2", patch=ScalarPatch(notes="exact notes"),
        qualification="trusted_frozen_cutover_readback",
    )
    row: dict[str, object] = {
        "intent": {
            "provider": "asana", "task_gid": "1218",
            "qualification": "trusted_frozen_cutover_readback",
            "request": {
                "observed_revision": "v1",
                "patch": {"notes": "exact notes", "completed": None, "priority": None},
            },
        },
    }

    assert _receipt_matches("work_update", receipt, row)
    with pytest.raises(ValueError, match="patch values must not be null"):
        ScalarPatch(notes="exact notes", completed=None)


def plan(snapshot: dict[str, object]) -> dict[str, object]:
    effects = cast(list[dict[str, object]], snapshot["effects"])
    projections = cast(list[dict[str, object]], snapshot["projections"])
    actions = []
    for effect in effects:
        row = cast(dict[str, object], effect["row"])
        identity = cast(str, row["operation_id"])
        action: dict[str, object] = {
            "id": identity, "sha256": effect["sha256"],
            "action": "replace" if identity == str(REPLACE) else "delete",
        }
        if identity == str(REPLACE):
            action["outcome"] = applied()
        actions.append(action)
    document: dict[str, object] = {
        "schema_version": 1, "effects": actions,
        "projections": [{
            "id": str(PROJECTION), "sha256": projections[0]["sha256"], "action": "delete",
        }],
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return document | {"sha256": hashlib.sha256(encoded).hexdigest()}
def redigest(document: dict[str, object]) -> None:
    body = {key: value for key, value in document.items() if key != "sha256"}
    document["sha256"] = hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def test_cleanup_equality_accepts_current_reviewed_cardinality_and_rejects_stale_rows():
    effects = [{
        "row": {
            "operation_id": f"operation-{index}",
            "outcome": {"effect": "unknown"},
        },
        "sha256": f"effect-digest-{index}",
    } for index in range(21)]
    projections = [{
        "row": {"projection_id": "projection-1"},
        "sha256": "projection-digest-1",
    }]
    snapshot: dict[str, object] = {
        "effects": effects,
        "projections": projections,
    }
    cleanup_plan: dict[str, object] = {
        "effects": [{
            "id": cast(dict[str, object], effect["row"])["operation_id"],
            "sha256": effect["sha256"],
        } for effect in effects],
        "projections": [{
            "id": "projection-1",
            "sha256": "projection-digest-1",
        }],
    }

    result = require_cleanup_equality(snapshot, cleanup_plan)

    assert result == {
        "effects": sorted(f"operation-{index}" for index in range(21)),
        "projections": ["projection-1"],
        "status": "PASS",
    }

    effects.append({
        "row": {
            "operation_id": "operation-21",
            "outcome": {"effect": "unknown"},
        },
        "sha256": "effect-digest-21",
    })
    cast(list[dict[str, object]], cleanup_plan["effects"]).append({
        "id": "operation-21",
        "sha256": "effect-digest-21",
    })
    assert require_cleanup_equality(snapshot, cleanup_plan)["status"] == "PASS"

    cast(dict[str, object], cast(list[object], cleanup_plan["effects"])[-1])[
        "sha256"
    ] = "stale-digest"
    with pytest.raises(RuntimeError, match="do not exactly match"):
        require_cleanup_equality(snapshot, cleanup_plan)


async def test_cleanup_archives_and_atomically_applies_exact_plan(
    engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    await seed(engine)
    plan_path, archive = tmp_path / "plan.json", tmp_path / "archive.json"
    write_manifest(plan_path, plan(await cleanup_snapshot(engine)))
    fsync_modes: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda descriptor: fsync_modes.append(os.fstat(descriptor).st_mode))

    async with engine.begin() as connection:
        assert await apply_cleanup_connection(connection, plan_path, archive) == {
            "replaced": 1, "deleted": 1, "projections_deleted": 1,
        }
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert any(stat.S_ISDIR(mode) for mode in fsync_modes)
    assert len(cast(list[object], load_manifest(archive)["effects"])) == 2
    async with engine.connect() as connection:
        rows = (await connection.execute(select(effect_intents))).mappings().all()
        outcome = GuardOutcome.model_validate(rows[0]["outcome"])
        assert outcome.operation_id == REPLACE and outcome.effect == "applied"
        assert await connection.scalar(select(message_projection.c.projection_id)) is None


async def test_connection_cleanup_is_owned_by_caller_transaction(
    engine: AsyncEngine, tmp_path: Path,
):
    await seed(engine)
    plan_path, archive = tmp_path / "rollback-plan.json", tmp_path / "rollback-archive.json"
    write_manifest(plan_path, plan(await cleanup_snapshot(engine)))

    async with engine.connect() as connection:
        transaction = await connection.begin()
        assert await apply_cleanup_connection(connection, plan_path, archive) == {
            "replaced": 1, "deleted": 1, "projections_deleted": 1,
        }
        await transaction.rollback()

    assert archive.is_file()
    async with engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(effect_intents)) == 2
        assert await connection.scalar(select(func.count()).select_from(message_projection)) == 1
        assert await connection.scalar(select(messages.c.message_id)) == MESSAGE
        assert await connection.scalar(select(message_deliveries.c.delivery_id)) == DELIVERY


async def test_cleanup_rejects_changed_or_omitted_rows_without_writing(
    engine: AsyncEngine, tmp_path: Path,
):
    await seed(engine)
    snapshot = await cleanup_snapshot(engine)
    plan_path, archive = tmp_path / "plan.json", tmp_path / "archive.json"
    invalid = plan(snapshot)
    replace = cast(dict[str, object], cast(list[object], invalid["effects"])[0])
    outcome = cast(dict[str, object], replace["outcome"])
    outcome["receipt"] = UpdateReceipt(
        operation_id=REPLACE, principal=PRINCIPAL, grant_id=GRANT, grant_version=1,
        work_id=WORK, provider="asana", task_gid="1218", observed_revision="v1",
        resulting_revision="v2", patch=ScalarPatch(title="changed"),
        qualification="trusted_frozen_cutover_readback",
    ).model_dump(mode="json", exclude_none=True)
    redigest(invalid)
    write_manifest(plan_path, invalid)
    with pytest.raises(ValueError, match="replacement outcome identity is invalid"):
        await apply_cleanup(engine, plan_path, archive)
    outcome["receipt"] = applied()["receipt"]
    cast(dict[str, object], outcome["receipt"])["text"] = "wrong text"
    redigest(invalid)
    write_manifest(tmp_path / "wrong-text.json", invalid)
    with pytest.raises(ValueError, match="replacement outcome identity is invalid"):
        await apply_cleanup(engine, tmp_path / "wrong-text.json", archive)
    write_manifest(tmp_path / "valid.json", plan(snapshot))
    async with engine.begin() as connection:
        await connection.execute(update(effect_intents).where(
            effect_intents.c.operation_id == str(REPLACE)
        ).values(fingerprint="changed"))

    with pytest.raises(ValueError, match="effect row changed"):
        await apply_cleanup(engine, tmp_path / "valid.json", archive)
    assert not archive.exists()
    async with engine.connect() as connection:
        assert len((await connection.execute(select(effect_intents))).all()) == 2
        assert await connection.scalar(select(message_projection.c.projection_id)) == PROJECTION

    current = await cleanup_snapshot(engine)
    omitted = plan(current)
    cast(list[object], omitted["effects"]).pop()
    redigest(omitted)
    write_manifest(tmp_path / "omitted.json", omitted)
    with pytest.raises(ValueError, match="does not name every"):
        await apply_cleanup(engine, tmp_path / "omitted.json", tmp_path / "archive-2.json")
