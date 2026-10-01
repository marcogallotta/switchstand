from dataclasses import replace
from uuid import UUID

import pytest

from switchstand.contracts import Routing, WorkContext
from switchstand.core import Handle
from switchstand.discovery import ProviderSearchItem
from switchstand.work_index import PrepareReceipt, canonical_digest, manifest_digest
from switchstand.work_index_migration import FrozenCorpus
from switchstand.workset_capture import (
    ProviderPlacement,
    ProviderProject,
    ProviderSection,
    ProviderStructureCapture,
    ProviderTaskStructure,
)
from switchstand.workset_worksheet import (
    ProjectMapping,
    WorkMapping,
    build_worksheet,
)

SHA = "3a04669a9f7a5c094bd7617c55003ed47a098f80"
MASTER = UUID("10000000-0000-4000-8000-000000000001")
CHILD = UUID("20000000-0000-4000-8000-000000000002")
ORPHAN = UUID("30000000-0000-4000-8000-000000000003")


def item(provider_id: str, revision: str) -> ProviderSearchItem:
    return ProviderSearchItem(
        provider_id, f"Task {provider_id}", False, revision, Routing(), WorkContext()
    )


@pytest.fixture
def evidence():
    items = (item("master", "r1"), item("child", "r2"))
    corpus = FrozenCorpus(
        items,
        {"master": MASTER, "child": None},
        {"orphan": ORPHAN},
        {"master": frozenset(), "child": frozenset()},
        SHA,
        "c" * 64,
        "e" * 64,
        {"orphan": "missing"},
    )
    before = (("master", str(MASTER)), ("orphan", str(ORPHAN)))
    inserted = (("child", str(CHILD)),)
    final = tuple(sorted((*before, *inserted)))
    handles = {
        provider: Handle(UUID(work_id), "asana", provider) for provider, work_id in final
    }
    receipt = PrepareReceipt(
        corpus.corpus_digest,
        manifest_digest(items, handles),
        canonical_digest(inserted),
        before,
        final,
        inserted,
    )
    capture = ProviderStructureCapture(
        SHA,
        (
            ProviderProject(
                "10", "Coordinator", "p1", False,
                (ProviderSection("11", "CURRENT"),),
            ),
            ProviderProject("20", "Main", "p2", False, ()),
        ),
        (
            ProviderTaskStructure("child", "r2", "master", ()),
            ProviderTaskStructure(
                "master", "r1", None,
                (ProviderPlacement("10", "11"), ProviderPlacement("20")),
            ),
        ),
    )
    projects = (
        ProjectMapping("10", "DURABLE_ROLE", "Coordinator", "master"),
        ProjectMapping("20"),
    )
    homes = (WorkMapping("master", "10"), WorkMapping("child", "10"))
    return corpus, receipt, capture, projects, homes


def test_human_review_worksheet_preserves_exact_evidence_and_decisions(evidence):
    worksheet = build_worksheet(*evidence)

    document = worksheet.document()
    assert document["review_status"] == "HUMAN_REVIEW_REQUIRED"
    assert document["stage1"] == {
        "corpus_sha256": "c" * 64,
        "exception_sha256": "e" * 64,
        "prepare_receipt_sha256": worksheet.prepare_receipt_digest,
    }
    assert document["provider_capture_sha256"] == worksheet.capture.digest()
    assert document["exceptions"] == [
        {"provider_work_id": "orphan", "work_id": str(ORPHAN), "reason": "missing"}
    ]
    memberships = {
        (row.work_id, row.semantics, row.member_role)
        for row in worksheet.snapshot.memberships
    }
    assert (MASTER, "AUTHORITATIVE", "MASTER") in memberships
    assert (MASTER, "RELATED", "MEMBER") in memberships
    assert (CHILD, "AUTHORITATIVE", "MEMBER") in memberships
    assert worksheet.snapshot.parent_edges[0].parent_work_id == MASTER
    assert worksheet.bytes().endswith(b"\n") and len(worksheet.digest()) == 64


@pytest.mark.parametrize(
    "forge",
    [
        lambda receipt: replace(receipt, corpus_digest="forged"),
        lambda receipt: replace(
            receipt,
            before_bindings=(("master", str(MASTER)), ("orphan", str(MASTER))),
        ),
        lambda receipt: replace(
            receipt,
            before_bindings=(("child", str(CHILD)), ("orphan", str(ORPHAN))),
            inserted_bindings=(("master", str(MASTER)),),
            inserted_digest=canonical_digest((("master", str(MASTER)),)),
        ),
        lambda receipt: replace(receipt, inserted_digest="forged"),
        lambda receipt: replace(receipt, prepared_digest="forged"),
        lambda receipt: replace(
            receipt,
            final_bindings=(
                ("child", str(MASTER)),
                ("master", str(CHILD)),
                ("orphan", str(ORPHAN)),
            ),
        ),
    ],
    ids=[
        "corpus", "exception-binding", "swapped-partitions",
        "inserted-digest", "prepared-digest", "swapped-final",
    ],
)
def test_worksheet_rejects_forged_or_swapped_stage1_receipt(evidence, forge):
    corpus, receipt, capture, projects, homes = evidence

    with pytest.raises(ValueError, match="receipt"):
        build_worksheet(corpus, forge(receipt), capture, projects, homes)


@pytest.mark.parametrize("failure", ["revision", "home", "role", "coverage"])
def test_worksheet_fails_closed_on_stale_or_ambiguous_review_decision(evidence, failure):
    corpus, receipt, capture, projects, homes = evidence
    if failure == "revision":
        capture = replace(
            capture,
            tasks=(replace(capture.tasks[0], revision="changed"), capture.tasks[1]),
        )
    elif failure == "home":
        homes = (WorkMapping("master", "10"), WorkMapping("child", "99"))
    elif failure == "role":
        projects = (ProjectMapping("10", "DURABLE_ROLE", "Coordinator"), projects[1])
    else:
        homes = homes[:1]

    with pytest.raises(ValueError):
        build_worksheet(corpus, receipt, capture, projects, homes)
