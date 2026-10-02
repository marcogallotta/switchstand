"""Read-only capture of the stable Asana corpus and zero-Asana preflight."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import UUID

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .core import ProviderError, ProviderWork
from .discovery import ProviderSearchItem, ProviderSearchPage
from .provider import AsanaProvider, ProviderWorkDecodeError, WorkDecodeReason
from .secure_file import create_new_private_bytes
from .state import work_event_handles, work_handles


class CorpusProvider(Protocol):
    async def search_work(
        self,
        text: str | None,
        completed: bool | None,
        cursor: str | None,
        limit: int,
    ) -> ProviderSearchPage: ...

    async def get(self, provider_work_id: str) -> ProviderWork | None: ...

    async def dependencies_for_import(self, provider_work_id: str) -> frozenset[str]: ...


@dataclass(frozen=True)
class CorpusRow:
    provider_work_id: str
    work_id: str | None
    title: str
    completed: bool
    revision: str
    routing: dict[str, object]
    context: dict[str, object]
    dependencies: tuple[str, ...]


@dataclass(frozen=True)
class CorpusException:
    provider_work_id: str
    work_id: str
    reason: str


@dataclass(frozen=True)
class HandleClassification:
    provider_work_id: str
    work_id: str
    state: Literal["current", "historical-missing", "unresolved"]
    reason: str | None


@dataclass(frozen=True)
class ParityResult:
    records: int
    digest: str


def parity_value(value: object) -> object:
    """Return the stable JSON representation shared by parity exporters."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("parity timestamps must include a timezone")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _populated_fields(rows: list[dict[str, object]], attribute: str) -> list[str]:
    populated: set[str] = set()
    for row in rows:
        values = cast(dict[str, object], row[attribute])
        populated.update(
            key for key, value in values.items()
            if value is not None and value not in ("", [], {})
        )
    return sorted(populated)


async def _database_preflight(engine: AsyncEngine) -> dict[str, object]:
    """Read migration blockers and provider-bound residue without mutating state."""
    async with engine.connect() as connection:
        event_counts = (await connection.execute(
            select(work_event_handles.c.provider, func.count())
            .group_by(work_event_handles.c.provider)
            .order_by(work_event_handles.c.provider)
        )).all()
        unknown_rows = (await connection.execute(
            text(
                "SELECT operation_id, work_id, outcome FROM effect_intents "
                "WHERE outcome->>'effect' = 'unknown' ORDER BY operation_id"
            )
        )).all()
        projection_rows = (await connection.execute(
            text(
                "SELECT projection_id, sender_work_id, message_id, operation_id, "
                "provider, target, state, receipt FROM message_projection "
                "ORDER BY projection_id"
            )
        )).mappings().all()

    unknown_effects: list[dict[str, object]] = []
    for operation_id, work_id, raw_outcome in unknown_rows:
        outcome = cast(dict[str, object], raw_outcome)
        unknown_effects.append({
            "operation_id": str(operation_id),
            "work_id": str(work_id),
            "operation": outcome.get("operation"),
            "reason": outcome.get("reason"),
        })
    projections = [{
        "projection_id": str(row["projection_id"]),
        "sender_work_id": str(row["sender_work_id"]),
        "message_id": str(row["message_id"]),
        "operation_id": str(row["operation_id"]),
        "provider": row["provider"],
        "target": row["target"],
        "state": row["state"],
        "receipt": row["receipt"],
    } for row in projection_rows]
    return {
        "event_aliases": {
            "total": sum(count for _, count in event_counts),
            "by_provider": [
                {"provider": provider, "count": count}
                for provider, count in event_counts
            ],
        },
        "unknown_effects": unknown_effects,
        "message_projections": projections,
    }


class CorpusCaptureDecodeError(ProviderError):
    """A bound provider item failed strict decoding during corpus capture."""

    def __init__(self, provider_work_id: str, reason: WorkDecodeReason):
        super().__init__("provider response invalid")
        self.provider_work_id = provider_work_id
        self.reason = reason


def _row(
    item: ProviderSearchItem,
    work_id: str | None,
    dependencies: frozenset[str],
) -> CorpusRow:
    return CorpusRow(
        provider_work_id=item.provider_work_id,
        work_id=work_id,
        title=item.title,
        completed=item.completed,
        revision=item.revision,
        routing=item.routing.model_dump(mode="json"),
        context=item.context.model_dump(mode="json"),
        dependencies=tuple(sorted(dependencies)),
    )


def _search_item(provider_work_id: str, work: ProviderWork) -> ProviderSearchItem:
    return ProviderSearchItem(
        provider_work_id=provider_work_id,
        title=work.title,
        completed=work.completed,
        revision=work.revision,
        routing=work.routing,
        context=work.context,
    )


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _with_digest(document: dict[str, object]) -> dict[str, object]:
    normalized = cast(dict[str, object], json.loads(_canonical(document)))
    return normalized | {"sha256": hashlib.sha256(_canonical(normalized)).hexdigest()}


async def capture_manifest(
    engine: AsyncEngine,
    provider: CorpusProvider,
    source_candidate: str,
    *,
    classify_decode_errors: bool = False,
) -> dict[str, object]:
    """Capture broad admission plus every readable bound Asana work item."""
    if len(source_candidate) != 40 or any(c not in "0123456789abcdef" for c in source_candidate):
        raise ValueError("source candidate must be a lowercase 40-character Git SHA")

    broad: dict[str, ProviderSearchItem] = {}
    cursor: str | None = None
    cursors: set[str] = set()
    while True:
        page = await provider.search_work(None, None, cursor, 100)
        for item in page.items:
            if item.provider_work_id in broad:
                raise ValueError("provider scan returned duplicate work")
            broad[item.provider_work_id] = item
        cursor = page.next_cursor
        if cursor is None:
            break
        if cursor in cursors:
            raise ValueError("provider scan repeated a cursor")
        cursors.add(cursor)

    async with engine.connect() as connection:
        bindings = (
            await connection.execute(
                select(
                    work_handles.c.provider_work_id,
                    work_handles.c.id,
                )
                .where(work_handles.c.provider == "asana")
                .order_by(work_handles.c.provider_work_id)
            )
        ).all()
    bound = {provider_id: str(work_id) for provider_id, work_id in bindings}
    if len(bound) != len(bindings):
        raise ValueError("database contains duplicate Asana bindings")

    included = dict(broad)
    exceptions: list[CorpusException] = []
    for provider_id in sorted(set(bound) - set(broad)):
        try:
            work = await provider.get(provider_id)
        except ProviderWorkDecodeError as error:
            if not classify_decode_errors:
                raise CorpusCaptureDecodeError(provider_id, error.reason) from None
            exceptions.append(CorpusException(
                provider_id, bound[provider_id], f"decode:{error.reason}",
            ))
            continue
        if work is None:
            exceptions.append(CorpusException(provider_id, bound[provider_id], "missing"))
        elif not work.canonical:
            exceptions.append(CorpusException(provider_id, bound[provider_id], "noncanonical"))
        else:
            included[provider_id] = _search_item(provider_id, work)

    rows = [
        _row(item, bound.get(provider_id), await provider.dependencies_for_import(provider_id))
        for provider_id, item in sorted(included.items())
    ]
    document: dict[str, object] = {
        "schema_version": 1,
        "source_candidate": source_candidate,
        "rows": [asdict(row) for row in rows],
        "exceptions": [asdict(item) for item in exceptions],
        "counts": {
            "broad": len(broad),
            "bound": len(bound),
            "included": len(rows),
            "exceptions": len(exceptions),
        },
    }
    return _with_digest(document)


async def capture_preflight_manifest(
    engine: AsyncEngine,
    provider: CorpusProvider,
    source_candidate: str,
) -> dict[str, object]:
    """Capture the read-only zero-Asana source inventory."""
    corpus = await capture_manifest(
        engine, provider, source_candidate, classify_decode_errors=True,
    )
    rows = cast(list[dict[str, object]], corpus["rows"])
    exceptions = cast(list[dict[str, object]], corpus["exceptions"])
    classifications = [
        HandleClassification(
            cast(str, row["provider_work_id"]), cast(str, row["work_id"]), "current", None,
        )
        for row in rows if row["work_id"] is not None
    ]
    classifications.extend(
        HandleClassification(
            cast(str, item["provider_work_id"]), cast(str, item["work_id"]),
            "historical-missing" if item["reason"] == "missing" else "unresolved",
            cast(str, item["reason"]),
        )
        for item in exceptions
    )
    classifications.sort(key=lambda item: item.provider_work_id)
    classification_counts = {
        state: sum(item.state == state for item in classifications)
        for state in ("current", "historical-missing", "unresolved")
    }
    document: dict[str, object] = {
        "schema_version": 1,
        "source_candidate": source_candidate,
        "corpus_sha256": corpus["sha256"],
        "handle_classifications": [asdict(item) for item in classifications],
        "populated_fields": {
            "routing": _populated_fields(rows, "routing"),
            "context": _populated_fields(rows, "context"),
        },
        **await _database_preflight(engine),
        "counts": {
            "task_handles": len(classifications),
            **classification_counts,
        },
    }
    return _with_digest(document)


def write_manifest(path: Path, manifest: dict[str, object]) -> None:
    data = json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def load_manifest(path: Path) -> dict[str, object]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("manifest must be a JSON object")
    mapping = cast(dict[object, object], raw)
    if not all(isinstance(key, str) for key in mapping):
        raise TypeError("manifest keys must be strings")
    value = cast(dict[str, object], mapping)
    digest = value.pop("sha256", None)
    if digest != hashlib.sha256(_canonical(value)).hexdigest():
        raise ValueError("manifest digest is invalid")
    return _with_digest(value)


def compare_manifests(first: Path, second: Path) -> str:
    left, right = load_manifest(first), load_manifest(second)
    if left != right:
        raise ValueError("corpus manifests do not match")
    return str(left["sha256"])


def parity_manifest(records: Sequence[object]) -> dict[str, object]:
    """Build the small common artifact consumed by full source/target parity."""
    document: dict[str, object] = {"schema_version": 1, "records": records}
    result = _with_digest(document)
    _parity_records(result)
    return result


def _parity_records(document: dict[str, object]) -> dict[tuple[str, str], object]:
    if set(document) != {"schema_version", "records", "sha256"}:
        raise ValueError("parity export has unexpected top-level fields")
    if document["schema_version"] != 1 or not isinstance(document["records"], list):
        raise ValueError("parity export schema is invalid")
    indexed: dict[tuple[str, str], object] = {}
    for raw in cast(list[object], document["records"]):
        if not isinstance(raw, dict):
            raise TypeError("parity record must be an object")
        mapping = cast(dict[object, object], raw)
        if set(mapping) != {"kind", "id", "fields"}:
            raise ValueError("parity record schema is invalid")
        record = cast(dict[str, object], mapping)
        kind, identity = record["kind"], record["id"]
        if not isinstance(kind, str) or not kind or not isinstance(identity, str) or not identity:
            raise ValueError("parity record identity is invalid")
        if not isinstance(record["fields"], dict):
            raise TypeError("parity record fields must be an object")
        key = kind, identity
        if key in indexed:
            raise ValueError(f"duplicate parity record: {kind}:{identity}")
        indexed[key] = record["fields"]
    return indexed


def compare_parity_exports(source: Path, target: Path) -> ParityResult:
    """Compare every field of every keyed record in two frozen exports."""
    left = _parity_records(load_manifest(source))
    right = _parity_records(load_manifest(target))
    if left.keys() != right.keys():
        missing, extra = left.keys() - right.keys(), right.keys() - left.keys()
        raise ValueError(
            f"parity record identities differ: missing={len(missing)} extra={len(extra)}"
        )
    for key in sorted(left):
        if _canonical(left[key]) != _canonical(right[key]):
            raise ValueError(f"parity fields differ for {key[0]}:{key[1]}")
    rows = [{"kind": key[0], "id": key[1], "fields": left[key]} for key in sorted(left)]
    return ParityResult(len(rows), hashlib.sha256(_canonical(rows)).hexdigest())


def manifest_exception_digest(manifest: dict[str, object]) -> str:
    exceptions = manifest.get("exceptions")
    if not isinstance(exceptions, list):
        raise TypeError("manifest exceptions are invalid")
    return hashlib.sha256(_canonical(cast(list[object], exceptions))).hexdigest()


def failure_receipt_path(manifest_path: Path) -> Path:
    return manifest_path.with_name(f"{manifest_path.name}.failure.json")


async def capture_to_path(
    engine: AsyncEngine,
    provider: CorpusProvider,
    path: Path,
    source_candidate: str,
) -> str:
    try:
        manifest = await capture_manifest(engine, provider, source_candidate)
    except CorpusCaptureDecodeError as error:
        receipt = {
            "provider_work_id": error.provider_work_id,
            "reason": error.reason,
        }
        create_new_private_bytes(
            failure_receipt_path(path),
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode() + b"\n",
        )
        raise
    write_manifest(path, manifest)
    return str(manifest["sha256"])


async def capture_preflight_to_path(
    engine: AsyncEngine,
    provider: CorpusProvider,
    path: Path,
    source_candidate: str,
) -> str:
    try:
        manifest = await capture_preflight_manifest(engine, provider, source_candidate)
    except CorpusCaptureDecodeError as error:
        create_new_private_bytes(
            failure_receipt_path(path),
            json.dumps({
                "provider_work_id": error.provider_work_id, "reason": error.reason,
            }, sort_keys=True, separators=(",", ":")).encode() + b"\n",
        )
        raise
    write_manifest(path, manifest)
    return str(manifest["sha256"])


async def _capture(path: Path, source_candidate: str, *, preflight: bool = False) -> str:
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    client = httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0",
        trust_env=False,
        headers={"Authorization": f"Bearer {os.environ['ASANA_TOKEN']}"},
    )
    try:
        provider = AsanaProvider(client, os.getenv("SWITCHSTAND_TEST_PROJECT_GID"))
        capture = capture_preflight_to_path if preflight else capture_to_path
        return await capture(engine, provider, path, source_candidate)
    finally:
        await client.aclose()
        await engine.dispose()


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    capture = subparsers.add_parser("capture")
    capture.add_argument("path", type=Path)
    capture.add_argument("--source-candidate", required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("path", type=Path)
    preflight.add_argument("--source-candidate", required=True)
    compare = subparsers.add_parser("compare")
    compare.add_argument("first", type=Path)
    compare.add_argument("second", type=Path)
    parity = subparsers.add_parser("parity")
    parity.add_argument("source", type=Path)
    parity.add_argument("target", type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.action in {"capture", "preflight"}:
            digest = asyncio.run(_capture(
                arguments.path, arguments.source_candidate,
                preflight=arguments.action == "preflight",
            ))
        elif arguments.action == "compare":
            digest = compare_manifests(arguments.first, arguments.second)
        else:
            result = compare_parity_exports(arguments.source, arguments.target)
            print(f"parity_records={result.records} parity_sha256={result.digest}")
            return
    except (KeyError, OSError, ProviderError, TypeError, ValueError) as error:
        parser.exit(1, f"Work corpus command failed: {error}\n")
    manifest = load_manifest(
        arguments.path if arguments.action in {"capture", "preflight"} else arguments.first
    )
    if arguments.action == "preflight":
        print(f"preflight_sha256={digest}")
    else:
        print(
            f"corpus_sha256={digest} "
            f"exception_sha256={manifest_exception_digest(manifest)}"
        )


if __name__ == "__main__":
    run()
