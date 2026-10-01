"""Read-only capture of the stable Stage 1 Asana work corpus."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, cast

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .core import ProviderError, ProviderWork
from .discovery import ProviderSearchItem, ProviderSearchPage
from .provider import AsanaProvider
from .state import work_handles


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
        work = await provider.get(provider_id)
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


def manifest_exception_digest(manifest: dict[str, object]) -> str:
    exceptions = manifest.get("exceptions")
    if not isinstance(exceptions, list):
        raise TypeError("manifest exceptions are invalid")
    return hashlib.sha256(_canonical(cast(list[object], exceptions))).hexdigest()


async def _capture(path: Path, source_candidate: str) -> str:
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    client = httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0",
        trust_env=False,
        headers={"Authorization": f"Bearer {os.environ['ASANA_TOKEN']}"},
    )
    try:
        provider = AsanaProvider(client, os.getenv("SWITCHSTAND_TEST_PROJECT_GID"))
        manifest = await capture_manifest(engine, provider, source_candidate)
        write_manifest(path, manifest)
        return str(manifest["sha256"])
    finally:
        await client.aclose()
        await engine.dispose()


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    capture = subparsers.add_parser("capture")
    capture.add_argument("path", type=Path)
    capture.add_argument("--source-candidate", required=True)
    compare = subparsers.add_parser("compare")
    compare.add_argument("first", type=Path)
    compare.add_argument("second", type=Path)
    arguments = parser.parse_args(argv)
    try:
        digest = (
            asyncio.run(_capture(arguments.path, arguments.source_candidate))
            if arguments.action == "capture"
            else compare_manifests(arguments.first, arguments.second)
        )
    except (KeyError, OSError, ProviderError, ValueError) as error:
        parser.exit(1, f"Stage 1 corpus capture failed: {error}\n")
    manifest = load_manifest(arguments.path if arguments.action == "capture" else arguments.first)
    print(f"corpus_sha256={digest} exception_sha256={manifest_exception_digest(manifest)}")


if __name__ == "__main__":
    run()
