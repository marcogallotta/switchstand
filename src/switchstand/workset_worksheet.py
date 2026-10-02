"""Exact receipt-bound Stage 3 worksheet for explicit Human Review decisions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from .core import Handle
from .work_index import ActivationUnknown, PrepareReceipt, validate_prepare_receipt
from .work_index_migration import FrozenCorpus
from .work_metadata import prepare_receipt_digest
from .workset_capture import ProviderProject, ProviderStructureCapture, ProviderTaskStructure
from .worksets import Membership, ParentEdge, Workset, WorksetSnapshot


@dataclass(frozen=True)
class ProjectMapping:
    provider_project_id: str
    kind: str = "PROJECT"
    role_identity: str | None = None
    master_provider_work_id: str | None = None


@dataclass(frozen=True)
class WorkMapping:
    provider_work_id: str
    authoritative_project_id: str


@dataclass(frozen=True)
class WorksetWorksheet:
    corpus_digest: str
    exception_digest: str
    prepare_receipt_digest: str
    capture: ProviderStructureCapture
    snapshot: WorksetSnapshot
    exceptions: tuple[dict[str, str], ...]

    def document(self) -> dict[str, object]:
        return {
            "format_version": 1,
            "review_status": "HUMAN_REVIEW_REQUIRED",
            "stage1": {
                "corpus_sha256": self.corpus_digest,
                "exception_sha256": self.exception_digest,
                "prepare_receipt_sha256": self.prepare_receipt_digest,
            },
            "provider_capture": asdict(self.capture),
            "provider_capture_sha256": self.capture.digest(),
            "worksets": [asdict(row) for row in self.snapshot.worksets],
            "memberships": [asdict(row) for row in self.snapshot.memberships],
            "parent_edges": [asdict(row) for row in self.snapshot.parent_edges],
            "exceptions": list(self.exceptions),
        }

    def bytes(self) -> bytes:
        return (
            json.dumps(
                self.document(), default=str, ensure_ascii=False, indent=2, sort_keys=True
            ).encode()
            + b"\n"
        )

    def digest(self) -> str:
        return hashlib.sha256(self.bytes()).hexdigest()


def snapshot_from_reviewed_bytes(data: bytes, expected_digest: str) -> WorksetSnapshot:
    """Parse the snapshot only when the complete reviewed worksheet bytes match."""
    if hashlib.sha256(data).hexdigest() != expected_digest:
        raise ValueError("worksheet does not match the Human-Reviewed digest")
    raw: object = json.loads(data)
    if not isinstance(raw, dict):
        raise TypeError("Stage 3 worksheet is malformed")
    document = cast(dict[str, object], raw)
    if set(document) != {
        "format_version", "review_status", "stage1", "provider_capture",
        "provider_capture_sha256", "worksets", "memberships", "parent_edges",
        "exceptions",
    } or document["format_version"] != 1 or document["review_status"] != "HUMAN_REVIEW_REQUIRED":
        raise ValueError("unsupported Stage 3 worksheet")
    stage1_value = document["stage1"]
    capture_value = document["provider_capture"]
    capture_digest = document["provider_capture_sha256"]
    exceptions_value = document["exceptions"]
    if not isinstance(stage1_value, dict) or not isinstance(capture_value, dict):
        raise TypeError("invalid Stage 3 worksheet evidence")
    stage1 = cast(dict[object, object], stage1_value)
    capture = cast(dict[object, object], capture_value)
    if (
        set(stage1) != {"corpus_sha256", "exception_sha256", "prepare_receipt_sha256"}
        or any(not isinstance(value, str) or not value for value in stage1.values())
        or not isinstance(capture_digest, str)
        or hashlib.sha256(json.dumps(
            capture, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ).encode()).hexdigest() != capture_digest
        or not isinstance(exceptions_value, list)
        or any(
            not _exact_fields(row, {"provider_work_id", "work_id", "reason"})
            for row in cast(list[object], exceptions_value)
        )
    ):
        raise ValueError("invalid Stage 3 worksheet evidence")

    def rows(name: str, fields: set[str]) -> list[dict[str, object]]:
        value = document[name]
        if not isinstance(value, list):
            raise TypeError(f"invalid Stage 3 worksheet {name}")
        values = cast(list[object], value)
        if any(not _exact_fields(row, fields) for row in values):
            raise ValueError(f"invalid Stage 3 worksheet {name}")
        return cast(list[dict[str, object]], values)

    def string(row: dict[str, object], field: str) -> str:
        value = row[field]
        if not isinstance(value, str):
            raise TypeError(f"invalid Stage 3 worksheet {field}")
        return value

    def integer(row: dict[str, object], field: str) -> int:
        value = row[field]
        if type(value) is not int:
            raise TypeError(f"invalid Stage 3 worksheet {field}")
        return value

    def optional_string(row: dict[str, object], field: str) -> str | None:
        value = row[field]
        if value is not None and not isinstance(value, str):
            raise TypeError(f"invalid Stage 3 worksheet {field}")
        return value

    return WorksetSnapshot(
        tuple(Workset(
            UUID(string(row, "workset_id")), string(row, "workset_key"),
            string(row, "name"), string(row, "kind"), optional_string(row, "role_identity"),
            string(row, "state"), integer(row, "row_version"),
        ) for row in rows(
            "worksets", {"workset_id", "workset_key", "name", "kind", "role_identity",
                         "state", "row_version"},
        )),
        tuple(Membership(
            UUID(string(row, "workset_id")), UUID(string(row, "work_id")),
            string(row, "semantics"), string(row, "member_role"), integer(row, "row_version"),
        ) for row in rows(
            "memberships", {"workset_id", "work_id", "semantics", "member_role", "row_version"},
        )),
        tuple(ParentEdge(
            UUID(string(row, "child_work_id")), UUID(string(row, "parent_work_id")),
            integer(row, "row_version"),
        ) for row in rows(
            "parent_edges", {"child_work_id", "parent_work_id", "row_version"},
        )),
    ).validated()


def _exact_fields(value: object, fields: set[str]) -> bool:
    return isinstance(value, dict) and set(cast(dict[object, object], value)) == fields


def _exact_bindings(corpus: FrozenCorpus, receipt: PrepareReceipt) -> dict[str, Handle]:
    if receipt.corpus_digest != corpus.corpus_digest:
        raise ValueError("preparation receipt does not bind the reviewed corpus")
    expected_before = tuple(sorted(
        [(provider, str(work_id)) for provider, work_id in corpus.work_ids.items()
         if work_id is not None]
        + [(provider, str(work_id)) for provider, work_id in corpus.exceptions.items()]
    ))
    try:
        return validate_prepare_receipt(receipt, corpus.items, expected_before)
    except (ActivationUnknown, TypeError, ValueError) as error:
        raise ValueError("preparation receipt does not bind the exact reviewed identities") from error


def _validated_capture(
    corpus: FrozenCorpus, capture: ProviderStructureCapture
) -> tuple[dict[str, ProviderProject], dict[str, ProviderTaskStructure]]:
    capture.validated()
    if capture.source_candidate != corpus.source_candidate:
        raise ValueError("provider capture does not bind the Stage 1 source candidate")
    projects = {row.provider_project_id: row for row in capture.projects}
    tasks = {row.provider_work_id: row for row in capture.tasks}
    expected = {row.provider_work_id: row.revision for row in corpus.items}
    if {provider: row.revision for provider, row in tasks.items()} != expected:
        raise ValueError("provider task identities or revisions changed after Stage 1 capture")
    return projects, tasks


def build_worksheet(
    corpus: FrozenCorpus,
    receipt: PrepareReceipt,
    capture: ProviderStructureCapture,
    project_mappings: tuple[ProjectMapping, ...],
    work_mappings: tuple[WorkMapping, ...],
) -> WorksetWorksheet:
    """Apply only supplied Human Review decisions to exact Stage 1/provider evidence."""
    handles = _exact_bindings(corpus, receipt)
    projects, tasks = _validated_capture(corpus, capture)
    project_map = {row.provider_project_id: row for row in project_mappings}
    work_map = {row.provider_work_id: row for row in work_mappings}
    if (
        len(project_map) != len(project_mappings)
        or set(project_map) != set(projects)
        or len(work_map) != len(work_mappings)
        or set(work_map) != set(tasks)
    ):
        raise ValueError("review decisions must cover every project and work item exactly once")

    effective: dict[str, set[str]] = {}
    visiting: set[str] = set()

    def memberships(provider_id: str) -> set[str]:
        if provider_id in effective:
            return effective[provider_id]
        if provider_id in visiting:
            raise ValueError("parent structure contains a cycle")
        visiting.add(provider_id)
        task = tasks[provider_id]
        values = {row.provider_project_id for row in task.placements}
        if task.parent_provider_work_id is not None:
            values |= memberships(task.parent_provider_work_id)
        visiting.remove(provider_id)
        effective[provider_id] = values
        return values

    worksets: list[Workset] = []
    workset_ids: dict[str, UUID] = {}
    masters: set[str] = set()
    roles: set[str] = set()
    for provider_project_id, mapping in sorted(project_map.items()):
        project = projects[provider_project_id]
        if (
            not mapping.kind.strip()
            or (mapping.role_identity is None) != (mapping.master_provider_work_id is None)
            or mapping.role_identity is not None and not mapping.role_identity.strip()
            or mapping.role_identity is not None and mapping.role_identity in roles
        ):
            raise ValueError("role identity and unique MASTER mapping must be supplied together")
        if mapping.role_identity is not None:
            roles.add(mapping.role_identity)
        if mapping.master_provider_work_id is not None:
            if (
                mapping.master_provider_work_id not in tasks
                or mapping.master_provider_work_id in masters
                or work_map[mapping.master_provider_work_id].authoritative_project_id
                != provider_project_id
            ):
                raise ValueError("MASTER must be unique and authoritative in its role workset")
            masters.add(mapping.master_provider_work_id)
        workset_id = uuid5(NAMESPACE_URL, f"switchstand:asana-project:{provider_project_id}")
        workset_ids[provider_project_id] = workset_id
        worksets.append(Workset(
            workset_id,
            f"asana.project.{provider_project_id}",
            project.name,
            mapping.kind,
            mapping.role_identity,
            "RETIRED" if project.archived else "ACTIVE",
        ))

    rows: list[Membership] = []
    for provider_id, mapping in sorted(work_map.items()):
        choices = memberships(provider_id)
        if mapping.authoritative_project_id not in choices:
            raise ValueError("authoritative project is not supported by provider structure")
        for project_id in sorted(choices):
            rows.append(Membership(
                workset_ids[project_id],
                handles[provider_id].id,
                "AUTHORITATIVE" if project_id == mapping.authoritative_project_id else "RELATED",
                "MASTER" if project_map[project_id].master_provider_work_id == provider_id
                else "MEMBER",
            ))
    parents = tuple(
        ParentEdge(handles[row.provider_work_id].id, handles[row.parent_provider_work_id].id)
        for row in capture.tasks
        if row.parent_provider_work_id is not None
    )
    snapshot = WorksetSnapshot(tuple(worksets), tuple(rows), parents).validated()
    exceptions = tuple(
        {
            "provider_work_id": provider_id,
            "work_id": str(work_id),
            "reason": corpus.exception_reasons[provider_id],
        }
        for provider_id, work_id in sorted(corpus.exceptions.items())
    )
    return WorksetWorksheet(
        corpus.corpus_digest,
        corpus.exception_digest,
        prepare_receipt_digest(receipt),
        capture,
        snapshot,
        exceptions,
    )
