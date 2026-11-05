import hashlib
import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.failure_journal import (
    EffectState,
    FailureJournal,
    FailureRecord,
    FailureResolution,
    redact_environment,
    redact_mcp_content,
    validate_failure_record_row,
    validate_failure_resolution_row,
)

NOW = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
WORK_ID = "20000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("value", [
    '{"access_token": "quoted secret canary", "notes": "ordinary prose"}',
    "{'password': 'quoted secret canary', 'notes': 'ordinary prose'}",
    'authorization="quoted secret canary" ordinary prose',
    'cookie="quoted secret canary" ordinary prose',
    'authorization: Bearer quoted-secret-canary ordinary prose',
    'authorization: Basic quoted-secret-canary ordinary prose',
])
def test_quoted_credentials_preserve_neighboring_prose(value):
    result = redact_mcp_content(value)
    assert "secret canary" not in result and "quoted-secret-canary" not in result
    assert "[redacted]" in result and "ordinary prose" in result


@pytest.mark.parametrize("ending", ["\nordinary prose", " ordinary prose"])
def test_complete_cookie_header_masks_all_pairs_and_preserves_neighboring_prose(ending):
    value = 'Cookie: session=first-secret; csrf=second-secret' + ending
    result = redact_mcp_content(value)
    assert "first-secret" not in result and "second-secret" not in result
    assert result.endswith(ending) and "[redacted]" in result


def failure(*, attempt_id=None, operation_id=None, result="connection refused"):
    return FailureRecord(
        attempt_id=attempt_id or uuid4(),
        operation_id=operation_id or uuid4(),
        attempted_claim="start managed worker",
        observed_result=result,
        clearing_action="verify runtime state and retry safely",
        effect_state=EffectState.NOT_SENT,
        owner=WORK_ID,
        occurred_at=NOW,
        evidence=("logs/worker.err",),
    )


def test_validation_and_redaction():
    plain = failure()
    assert plain.canonical() == plain.model_dump(mode="json")
    with pytest.raises(ValidationError):
        failure().model_copy(update={"owner": "not-a-work-id"}).model_validate(
            {**failure().model_dump(), "owner": "not-a-work-id"}
        )
    with pytest.raises(ValidationError):
        FailureRecord(**{**failure().model_dump(), "occurred_at": NOW.replace(tzinfo=None)})

    value = failure(result="Bearer abc.def password=hunter2 postgresql://user:pass@db/x")
    sanitized = value.sanitized()
    assert "abc.def" not in sanitized.observed_result
    assert "hunter2" not in sanitized.observed_result
    assert "user:pass" not in sanitized.observed_result


def test_boundary_redaction_covers_controller_only_and_encoded_url_credentials():
    text = (
        "controller-value postgresql+psycopg://user%40name:p%2Fass@db/x "
        "Bearer abc.def token=loose"
    )
    redacted = redact_environment(text, {"CONTROLLER_SECRET": "controller-value"})
    for secret in ("controller-value", "user%40name", "p%2Fass", "abc.def", "loose"):
        assert secret not in redacted


@pytest.fixture
async def journal(database_prerequisite):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for failure journal tests")
    assert make_url(url).database == "switchstand_test"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    await engine.dispose()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_async_engine(url)
    yield FailureJournal(engine)
    await engine.dispose()


async def test_canonical_replay_conflict_resolution_and_open_reads(journal):
    value = failure()
    assert (await journal.record(value)).status == "APPLIED"
    assert (await journal.record(value)).status == "REPLAYED"
    assert (
        await journal.record(value.model_copy(update={"observed_result": "different"}))
    ).status == "CONFLICT"
    assert await journal.get(value.attempt_id) == value
    assert await journal.attempt_ids() == (value.attempt_id,)
    assert await journal.open(owner=WORK_ID) == (value,)

    resolution = FailureResolution(
        resolution_id=uuid4(),
        operation_id=uuid4(),
        attempt_id=value.attempt_id,
        resolved_at=NOW,
        summary="fixed",
        evidence=("commit:abc",),
    )
    assert (await journal.resolve(resolution)).status == "APPLIED"
    assert (await journal.resolve(resolution)).status == "REPLAYED"
    assert await journal.open(owner=WORK_ID) == ()

    foreign = failure(attempt_id=uuid4(), operation_id=value.operation_id)
    assert (await journal.record(foreign)).status == "CONFLICT"


@pytest.mark.parametrize("field,limit", [
    ("attempted_claim", 2000), ("observed_result", 4000), ("clearing_action", 2000),
    ("record_evidence", 512), ("summary", 2000), ("resolution_evidence", 512),
])
@pytest.mark.parametrize("suffix,replacement", [
    (" token=q", " token=[redacted]"),
    (" Bearer q", " Bearer [redacted]"),
    (" https://u:q@", " https://[redacted]@"),
])
def test_canonical_diagnostics_remain_valid_at_redaction_bounds(field, limit, suffix, replacement):
    prefix = "界" * (limit - len(suffix))
    raw = prefix + suffix
    expected = (prefix + replacement)[:limit]
    if field in {"summary", "resolution_evidence"}:
        value = FailureResolution(
            resolution_id=uuid4(), operation_id=uuid4(), attempt_id=uuid4(),
            resolved_at=NOW, summary=raw if field == "summary" else "fixed",
            evidence=(raw,) if field == "resolution_evidence" else (),
        )
    else:
        payload = failure().model_dump()
        payload.update({"evidence": (raw,)} if field == "record_evidence" else {field: raw})
        value = FailureRecord.model_validate(payload)
        assert value.sanitized().model_dump(mode="json") == value.canonical()
    canonical = value.canonical()
    actual = canonical["evidence"][0] if "evidence" in field else canonical[field]
    assert actual == expected
    assert len(actual) == limit
    assert "q" not in actual
    assert type(value).model_validate(canonical).canonical() == canonical


@pytest.mark.parametrize("evidence", [("",), ("x" * 513,), ("x",) * 17])
def test_resolution_retains_raw_evidence_refusal(evidence):
    with pytest.raises(ValueError):
        FailureResolution(
            resolution_id=uuid4(), operation_id=uuid4(), attempt_id=uuid4(),
            resolved_at=NOW, summary="fixed", evidence=evidence,
        ).canonical()


async def test_redaction_boundary_journal_roundtrip_and_resolution(journal):
    value = failure(result="界" * 3992 + " token=q")
    assert (await journal.record(value)).status == "APPLIED"
    assert (await journal.record(value)).status == "REPLAYED"
    stored = await journal.get(value.attempt_id)
    assert stored == value.sanitized()
    assert isinstance(stored, FailureRecord)
    assert "q" not in stored.observed_result
    assert FailureRecord.model_validate(stored.canonical()) == stored
    assert (await journal.record(value.model_copy(update={"observed_result": "changed"}))
            ).status == "CONFLICT"
    resolution = FailureResolution(
        resolution_id=uuid4(), operation_id=uuid4(), attempt_id=value.attempt_id,
        resolved_at=NOW, summary="界" * 1992 + " token=q",
        evidence=("界" * 504 + " token=q",),
    )
    assert (await journal.resolve(resolution)).status == "APPLIED"
    assert (await journal.resolve(resolution)).status == "REPLAYED"
    async with journal.engine.connect() as connection:
        row = (await connection.execute(text(
            "SELECT * FROM failure_resolutions WHERE resolution_id = :id"
        ), {"id": resolution.resolution_id})).mappings().one()
    persisted = FailureResolution.model_validate({key: row[key]
                                                for key in FailureResolution.model_fields})
    assert validate_failure_resolution_row({f"resolution_{key}": item
                                            for key, item in row.items()}) == persisted
    assert persisted.model_dump(mode="json") == resolution.canonical()
    assert "q" not in persisted.summary
    assert "q" not in persisted.evidence[0]
    assert (await journal.resolve(resolution.model_copy(update={"summary": "changed"}))
            ).status == "CONFLICT"
    assert await journal.open(owner=WORK_ID) == ()


async def test_historical_quoted_credentials_keep_integrity_and_replay(journal):
    value = failure(result='{"access_token":"historical synthetic credential"}')
    original = value.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(original, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert value.canonical() == original
    assert validate_failure_record_row(original | {"content_digest": digest}) == value
    assert validate_failure_record_row(original | {"content_digest": "tampered"}) is None
    assert (await journal.record(value)).status == "APPLIED"
    assert (await journal.record(value)).status == "REPLAYED"
    assert await journal.get(value.attempt_id) == value
    resolution = FailureResolution(resolution_id=uuid4(), operation_id=uuid4(),
                                   attempt_id=value.attempt_id, resolved_at=NOW,
                                   summary='{"access_token":"historical synthetic resolution"}')
    original = resolution.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(original, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    row = {f"resolution_{key}": item for key, item in original.items()}
    assert resolution.canonical() == original
    assert validate_failure_resolution_row(row | {"resolution_content_digest": digest}) == resolution
    assert validate_failure_resolution_row(row | {"resolution_content_digest": "tampered"}) is None
    assert (await journal.resolve(resolution)).status == "APPLIED"
    assert (await journal.resolve(resolution)).status == "REPLAYED"
