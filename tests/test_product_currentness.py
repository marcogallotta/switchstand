from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from switchstand.product_currentness import (
    STATEFUL_PRODUCT_WORK_ID,
    SourceEvidence,
    SourceName,
    StatefulAcceptanceBinding,
    evaluate_stateful_currentness,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
SOURCES: tuple[SourceName, ...] = (
    "runtime_identity",
    "feature_enabled",
    "schema_surface",
    "persistence_ready",
    "functional_proof",
)
BOUND_CONTRACT = StatefulAcceptanceBinding(
    contract_revision="test-contract-v1",
    required_sources=SOURCES,
    evidence_id="test:accepted-contract",
    currentness_token="contract:1",
    basis_token="release:1",
)


class LiveSources:
    def __init__(self, results: dict[SourceName, str] | None = None) -> None:
        self.results = results or {}
        self.tokens: dict[SourceName, str | None] = {source: f"{source}:1" for source in SOURCES}
        self.basis = "release:1"
        self.advance_basis_on: SourceName | None = None
        self.advance_basis_to = "release:2"
        self.raise_on: SourceName | None = None
        self.wrong_source: SourceName | None = None
        self.binding: StatefulAcceptanceBinding | None = BOUND_CONTRACT
        self.ending_binding: StatefulAcceptanceBinding | None = None
        self.binding_reads = 0

    async def read(self, source: SourceName) -> SourceEvidence:
        if source == self.raise_on:
            raise RuntimeError("offline")
        if source == self.advance_basis_on:
            self.basis = self.advance_basis_to
        returned = "runtime_identity" if source == self.wrong_source else source
        return SourceEvidence(
            source=returned,  # type: ignore[arg-type]
            result=self.results.get(source, "TRUE"),  # type: ignore[arg-type]
            evidence_id=f"evidence:{source}",
            currentness_token=self.tokens[source],
            basis_token=self.basis,
            observed_at=NOW,
        )

    async def currentness_token(self, source: SourceName) -> str | None:
        return self.tokens[source]

    async def basis_token(self) -> str:
        return self.basis

    async def acceptance_binding(self) -> StatefulAcceptanceBinding | None:
        self.binding_reads += 1
        if self.binding_reads > 1 and self.ending_binding is not None:
            return self.ending_binding
        return self.binding


async def evaluate(sources: LiveSources, product_work_id: UUID = STATEFUL_PRODUCT_WORK_ID):
    return await evaluate_stateful_currentness(product_work_id, sources)


async def test_complete_coherent_contract_is_true_and_stable() -> None:
    sources = LiveSources()
    first = await evaluate(sources)
    second = await evaluate(sources)

    assert first.status == "ok" and first.current == "TRUE"
    assert first.blockers == ()
    assert len(first.conditions) == len(SOURCES)
    assert first.basis_id == second.basis_id
    assert first.reconciliation_id == second.reconciliation_id


async def test_each_false_condition_makes_product_false() -> None:
    for source in SOURCES:
        result = await evaluate(LiveSources({source: "FALSE"}))
        assert result.status == "ok" and result.current == "FALSE"
        assert source in result.blockers


async def test_unknown_and_conflict_never_claim_true() -> None:
    unknown = await evaluate(LiveSources({"schema_surface": "UNKNOWN"}))
    conflict = await evaluate(LiveSources({"runtime_identity": "CONFLICT"}))
    assert (unknown.status, unknown.current) == ("unknown", "UNKNOWN")
    assert (conflict.status, conflict.current) == ("conflict", "CONFLICT")
    assert unknown.reason == "evidence_or_coherence_unproved"
    assert conflict.reason == "conflicting_evidence"


async def test_known_false_is_decisive_but_conflict_is_not_hidden() -> None:
    false_unknown = await evaluate(
        LiveSources({"feature_enabled": "FALSE", "functional_proof": "UNKNOWN"})
    )
    false_conflict = await evaluate(
        LiveSources({"feature_enabled": "FALSE", "functional_proof": "CONFLICT"})
    )
    assert (false_unknown.status, false_unknown.current) == ("ok", "FALSE")
    assert (false_conflict.status, false_conflict.current) == ("conflict", "CONFLICT")


async def test_source_change_during_evaluation_is_unknown() -> None:
    sources = LiveSources()
    original = sources.currentness_token

    async def changed(source: SourceName) -> str | None:
        return "runtime_identity:2" if source == "runtime_identity" else await original(source)

    sources.currentness_token = changed  # type: ignore[method-assign]
    result = await evaluate_stateful_currentness(STATEFUL_PRODUCT_WORK_ID, sources)
    runtime = next(item for item in result.conditions if item.name == "runtime_identity")
    assert result.status == "unknown" and result.current == "UNKNOWN"
    assert runtime.detail == "source_changed_during_reconciliation"


async def test_missing_token_reader_failure_and_wrong_identity_are_unknown() -> None:
    sources = LiveSources()
    sources.tokens["feature_enabled"] = None
    sources.raise_on = "schema_surface"
    result = await evaluate(sources)
    by_name = {item.name: item for item in result.conditions}
    assert result.status == "unknown"
    assert by_name["feature_enabled"].detail == "source_currentness_unavailable"
    assert by_name["schema_surface"].evidence_id == "unavailable:schema_surface"

    wrong = LiveSources()
    wrong.wrong_source = "functional_proof"
    mismatch = await evaluate(wrong)
    proof = next(item for item in mismatch.conditions if item.name == "functional_proof")
    assert mismatch.current == "UNKNOWN"
    assert proof.detail == "source_identity_mismatch"


async def test_unsupported_product_is_denied_without_conditions() -> None:
    result = await evaluate(LiveSources(), UUID("11111111-1111-1111-1111-111111111111"))
    assert result.status == "denied" and result.current == "UNKNOWN"
    assert result.reason == "unsupported_product"
    assert result.conditions == ()


async def test_unbound_acceptance_contract_is_unknown_without_evaluation() -> None:
    sources = LiveSources()
    sources.binding = None
    result = await evaluate(sources)
    assert result.status == "unknown" and result.current == "UNKNOWN"
    assert result.reason == "acceptance_contract_unbound"
    assert result.contract_revision == "UNBOUND"
    assert result.conditions == ()


async def test_mixed_release_observations_never_claim_true() -> None:
    sources = LiveSources()
    sources.advance_basis_on = "schema_surface"
    result = await evaluate(sources)
    assert result.status == "unknown" and result.current == "UNKNOWN"
    assert {condition.detail for condition in result.conditions} == {"evaluation_basis_unproved"}


async def test_distinct_mixed_release_evaluations_have_distinct_identities() -> None:
    release_two = LiveSources()
    release_two.advance_basis_on = "schema_surface"
    release_three = LiveSources()
    release_three.advance_basis_on = "schema_surface"
    release_three.advance_basis_to = "release:3"
    first = await evaluate(release_two)
    second = await evaluate(release_three)
    assert first.current == second.current == "UNKNOWN"
    assert first.basis_id != second.basis_id
    assert first.reconciliation_id != second.reconciliation_id


async def test_contract_change_during_evaluation_is_unknown_and_attributable() -> None:
    contract_two = LiveSources()
    contract_two.ending_binding = BOUND_CONTRACT.model_copy(
        update={"contract_revision": "test-contract-v2"}
    )
    contract_three = LiveSources()
    contract_three.ending_binding = BOUND_CONTRACT.model_copy(
        update={"contract_revision": "test-contract-v3"}
    )
    first = await evaluate(contract_two)
    second = await evaluate(contract_three)
    assert first.current == second.current == "UNKNOWN"
    assert first.basis_id != second.basis_id
    assert first.reconciliation_id != second.reconciliation_id
