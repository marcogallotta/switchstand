import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

from switchstand.failure_journal import EffectState, FailureRecord, JournalWrite
from switchstand.pending_failures import (
    PendingFailureQueue,
    PendingFailureRegistry,
    RegistrationState,
    legacy_cutoff_readiness,
)


def failure(result="connection refused"):
    return FailureRecord(
        attempt_id=uuid4(),
        operation_id=uuid4(),
        attempted_claim="start worker",
        observed_result=result,
        clearing_action="synchronize the pending record",
        effect_state=EffectState.NOT_SENT,
        owner="COORDINATOR",
        occurred_at=datetime(2026, 10, 3, tzinfo=UTC),
    )


class FakeJournal:
    def __init__(self):
        self.values = {}

    async def record(self, value):
        prior = self.values.setdefault(value.attempt_id, value)
        return JournalWrite("APPLIED" if prior is value else "REPLAYED")

    async def get(self, attempt_id):
        return self.values.get(attempt_id)


def test_registry_gate_and_cutoff_prerequisites():
    value = failure()
    registry = PendingFailureRegistry()
    registry.register(value.attempt_id)
    assert registry.closure_gate() == (False, (value.attempt_id,))
    registry.mark(value.attempt_id, RegistrationState.PENDING_SYNC)
    assert registry.closure_gate() == (True, ())

    pending = legacy_cutoff_readiness(
        source_ids={value.attempt_id},
        imported_ids=set(),
        read_parity=False,
        queue_empty=False,
    )
    assert pending[1] == (
        "IMPORT_INCOMPLETE",
        "READ_PARITY_UNPROVED",
        "PENDING_SYNC_REMAINS",
    )
    assert legacy_cutoff_readiness(
        source_ids={value.attempt_id},
        imported_ids={value.attempt_id},
        read_parity=True,
        queue_empty=True,
    )[0]


async def test_queue_is_private_idempotent_and_readback_gated(tmp_path):
    queue = PendingFailureQueue(tmp_path / "pending")
    value = failure("token=supersecret")
    assert queue.enqueue(value) == "PENDING_SYNC"
    path = queue._path(value.attempt_id)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert b"supersecret" not in path.read_bytes()
    assert queue.enqueue(value) == "PENDING_SYNC"
    assert queue.enqueue(value.model_copy(update={"observed_result": "different"})) == "CONFLICT"

    journal = FakeJournal()
    journal.values[value.attempt_id] = value.model_copy(update={"observed_result": "wrong"})
    assert await queue.sync(journal) == ()
    assert queue.pending() == (value.sanitized(),)
    journal.values.clear()
    assert await queue.sync(journal) == (value.attempt_id,)
    assert queue.pending() == ()


def test_concurrent_enqueue_cannot_replace_an_attempt(tmp_path):
    queue = PendingFailureQueue(tmp_path / "pending")
    value = failure()
    conflict = value.model_copy(update={"observed_result": "different"})
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(queue.enqueue, (value, conflict)))
    assert sorted(results) == ["CONFLICT", "PENDING_SYNC"]
    assert queue.pending() in ((value,), (conflict,))
