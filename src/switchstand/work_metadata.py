"""Inert Stage 2 worksheet validation and atomic metadata authority flip."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncTransaction

from .contracts import Routing, WorkContext
from .discovery import ProviderSearchItem
from .state import (
    work_authority,
    work_edges,
    work_handles,
    work_index,
    work_metadata_authority,
    work_metadata_cutovers,
)
from .work_index import (
    ActivationNotCommitted,
    ActivationReceipt,
    ActivationUnknown,
    PrepareReceipt,
    WorkIndex,
    canonical_digest,
    corpus_digest,
    corpus_digest_from_connection,
    validate_prepare_receipt,
)

SCOPE = "workspace"
AUTHORITY = "POSTGRES_AUTHORITY"
UNKNOWN = "UNKNOWN"
NONE = "NONE"
MUTABLE_FIELDS: frozenset[str] = frozenset({
    "priority", "work_type", "lifecycle_state", "canonical_root", "owner_key",
    "wait_kind", "unblock_condition", "next_due", "next_action_class",
    "next_action_ref", "review_next_action",
})


async def authority_generation(engine: AsyncEngine) -> int | None:
    async with engine.connect() as connection:
        row = (await connection.execute(select(
            select(work_metadata_authority.c.state).where(
                work_metadata_authority.c.scope == SCOPE
            ).scalar_subquery(),
            select(work_metadata_authority.c.generation).where(
                work_metadata_authority.c.scope == SCOPE
            ).scalar_subquery(),
            select(work_metadata_cutovers.c.generation).where(
                work_metadata_cutovers.c.scope == SCOPE
            ).scalar_subquery(),
        ))).one()
    if row == (None, None, None):
        return None
    if row[0] != AUTHORITY or row[1] != row[2] or not isinstance(row[1], int) or row[1] < 1:
        raise ValueError("inconsistent irreversible work metadata authority state")
    return row[1]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorksheetRow(_Closed):
    work_id: UUID
    provider_work_id: str = Field(min_length=1)
    provider_revision: str = Field(min_length=1)
    priority: str | None = None
    work_type: str | None = None
    lifecycle_state: Literal["CURRENT", "WAITING", "DEFERRED", "TERMINAL", "UNKNOWN"]
    canonical_root: str = Field(min_length=1)
    owner_key: str = Field(min_length=1)
    wait_kind: str = Field(min_length=1)
    unblock_condition: str = Field(min_length=1)
    next_due: str = Field(min_length=1)
    next_action_class: str = Field(min_length=1)
    next_action_ref: str = Field(min_length=1)
    review_next_action: str | None = None
    horizon: str | None = None
    stage3_gate: str | None = None
    depends_on: tuple[UUID, ...] = ()

    def routing_projection(self) -> Routing:
        return Routing(
            priority=self.priority,
            work_type=self.work_type,
            review_next_action=self.review_next_action,
            horizon=self.horizon,
            stage3_gate=self.stage3_gate,
        )

    @model_validator(mode="after")
    def coherent(self) -> WorksheetRow:
        if len(set(self.depends_on)) != len(self.depends_on) or self.work_id in self.depends_on:
            raise ValueError("dependency WorkIds must be unique and cannot be self-references")
        if self.lifecycle_state in {"WAITING", "DEFERRED"}:
            if self.wait_kind in {UNKNOWN, NONE} or self.unblock_condition in {UNKNOWN, NONE}:
                raise ValueError("waiting/deferred work requires exact wait kind and reopen condition")
        elif self.lifecycle_state in {"CURRENT", "TERMINAL"} and (
            self.wait_kind != NONE or self.unblock_condition != NONE or self.next_due != NONE
        ):
            raise ValueError("current/terminal work cannot carry a wait")
        return self


class ImportWorksheet(_Closed):
    format_version: Literal[1] = 1
    stage1_generation: int = Field(ge=1)
    prepare_receipt_digest: str | None = None
    rows: tuple[WorksheetRow, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def complete(self) -> ImportWorksheet:
        ids = [row.work_id for row in self.rows]
        provider_ids = [row.provider_work_id for row in self.rows]
        if len(ids) != len(set(ids)) or len(provider_ids) != len(set(provider_ids)):
            raise ValueError("worksheet must contain each WorkId/provider identity exactly once")
        admitted = set(ids)
        for row in self.rows:
            if row.canonical_root not in {UNKNOWN, NONE}:
                try:
                    root = UUID(row.canonical_root)
                except ValueError as error:
                    raise ValueError("canonical_root must be UNKNOWN, NONE, or a WorkId") from error
                if root not in admitted:
                    raise ValueError("canonical_root must reference the frozen admitted corpus")
            if not set(row.depends_on) <= admitted:
                raise ValueError("dependencies must reference the frozen admitted corpus")
        return self


@dataclass(frozen=True)
class ProviderMetadataSnapshot:
    provider_work_id: str
    revision: str
    routing: Routing
    notes: str
    context: WorkContext
    dependency_provider_ids: frozenset[str]


async def generate_worksheet(engine: AsyncEngine) -> ImportWorksheet:
    """Generate an operator-editable worksheet only from the frozen Stage 1 corpus."""
    async with engine.connect() as connection:
        authority = (await connection.execute(select(
            work_authority.c.generation, work_authority.c.state,
        ).where(work_authority.c.scope == SCOPE))).one_or_none()
        if authority is None or authority[1] != AUTHORITY:
            raise RuntimeError("Stage 1 POSTGRES_AUTHORITY is required")
        rows = (await connection.execute(select(
            work_index.c.work_id,
            work_handles.c.provider_work_id,
            work_index.c.provider_revision,
            work_index.c.completed,
            work_index.c.routing,
        ).join(work_handles, work_handles.c.id == work_index.c.work_id).order_by(
            work_index.c.work_id
        ))).all()
    if not rows:
        raise RuntimeError("Stage 1 admitted corpus is empty")
    generated: list[WorksheetRow] = []
    for row in rows:
        routing = Routing.model_validate(row[4])
        terminal = bool(row[3])
        generated.append(WorksheetRow(
            work_id=row[0], provider_work_id=row[1], provider_revision=row[2],
            priority=routing.priority, work_type=routing.work_type,
            lifecycle_state="TERMINAL" if terminal else "UNKNOWN",
            canonical_root=UNKNOWN, owner_key=UNKNOWN,
            wait_kind=NONE if terminal else UNKNOWN,
            unblock_condition=NONE if terminal else UNKNOWN,
            next_due=NONE if terminal else UNKNOWN,
            next_action_class=UNKNOWN, next_action_ref=UNKNOWN,
            review_next_action=routing.review_next_action,
            horizon=routing.horizon, stage3_gate=routing.stage3_gate,
        ))
    return ImportWorksheet(stage1_generation=authority[0], rows=tuple(generated))


def prepare_receipt_digest(receipt: PrepareReceipt) -> str:
    return canonical_digest(asdict(receipt))


def generate_prepared_worksheet(
    items: tuple[ProviderSearchItem, ...], receipt: PrepareReceipt,
    expected_before: tuple[tuple[str, str], ...],
    expected_corpus_digest: str,
) -> ImportWorksheet:
    """Generate Stage 2 input from exact prepared identities before Stage 1 authority."""
    if receipt.corpus_digest != expected_corpus_digest:
        raise ValueError("preparation receipt does not bind the reviewed corpus")
    handles = validate_prepare_receipt(receipt, items, expected_before)
    rows = tuple(sorted((WorksheetRow(
        work_id=handles[item.provider_work_id].id,
        provider_work_id=item.provider_work_id, provider_revision=item.revision,
        priority=item.routing.priority, work_type=item.routing.work_type,
        lifecycle_state="TERMINAL" if item.completed else "UNKNOWN",
        canonical_root=UNKNOWN, owner_key=UNKNOWN,
        wait_kind=NONE if item.completed else UNKNOWN,
        unblock_condition=NONE if item.completed else UNKNOWN,
        next_due=NONE if item.completed else UNKNOWN,
        next_action_class=UNKNOWN, next_action_ref=UNKNOWN,
        review_next_action=item.routing.review_next_action,
        horizon=item.routing.horizon, stage3_gate=item.routing.stage3_gate,
    ) for item in items), key=lambda row: row.work_id.int))
    return ImportWorksheet(
        stage1_generation=1, prepare_receipt_digest=prepare_receipt_digest(receipt), rows=rows,
    )


def validate_snapshots(
    worksheet: ImportWorksheet,
    snapshots: tuple[ProviderMetadataSnapshot, ...],
) -> None:
    by_provider = {snapshot.provider_work_id: snapshot for snapshot in snapshots}
    if len(by_provider) != len(snapshots):
        raise ValueError("provider snapshot contains duplicate work")
    if set(by_provider) != {row.provider_work_id for row in worksheet.rows}:
        raise ValueError("provider snapshot does not exactly match worksheet corpus")
    work_to_provider = {row.work_id: row.provider_work_id for row in worksheet.rows}
    for row in worksheet.rows:
        snapshot = by_provider[row.provider_work_id]
        if snapshot.revision != row.provider_revision:
            raise ValueError("provider revision changed after worksheet generation")
        if snapshot.routing != row.routing_projection():
            raise ValueError("structured provider metadata changed after worksheet generation")
        expected_dependencies = frozenset(work_to_provider[value] for value in row.depends_on)
        if snapshot.dependency_provider_ids != expected_dependencies:
            raise ValueError("provider dependencies do not match the worksheet")


def validate_prepared_worksheet(
    worksheet: ImportWorksheet, snapshots: tuple[ProviderMetadataSnapshot, ...],
    items: tuple[ProviderSearchItem, ...], receipt: PrepareReceipt,
    expected_before: tuple[tuple[str, str], ...],
    expected_corpus_digest: str,
    expected_dependencies: dict[str, frozenset[str]],
) -> None:
    """Validate exact Stage 2 evidence without requiring Stage 1 authority."""
    if receipt.corpus_digest != expected_corpus_digest:
        raise ValueError("preparation receipt does not bind the reviewed corpus")
    handles = validate_prepare_receipt(receipt, items, expected_before)
    if worksheet.stage1_generation != 1 or (
        worksheet.prepare_receipt_digest != prepare_receipt_digest(receipt)
    ):
        raise ValueError("worksheet does not bind the exact Stage 1 preparation receipt")
    by_provider = {item.provider_work_id: item for item in items}
    if {(row.provider_work_id, row.work_id) for row in worksheet.rows} != {
        (provider, handle.id) for provider, handle in handles.items() if provider in by_provider
    }:
        raise ValueError("worksheet identities do not match the prepared Stage 1 corpus")
    for row in worksheet.rows:
        item = by_provider[row.provider_work_id]
        if row.provider_revision != item.revision:
            raise ValueError("worksheet revision does not match the reviewed Stage 1 corpus")
        if (row.lifecycle_state == "TERMINAL") != item.completed:
            raise ValueError("worksheet terminal state does not match the reviewed Stage 1 corpus")
    if {
        snapshot.provider_work_id: snapshot.dependency_provider_ids for snapshot in snapshots
    } != expected_dependencies:
        raise ValueError("provider dependencies changed from the reviewed Stage 1 corpus")
    validate_snapshots(worksheet, snapshots)


async def validate_worksheet(
    engine: AsyncEngine,
    worksheet: ImportWorksheet,
    snapshots: tuple[ProviderMetadataSnapshot, ...],
) -> None:
    validate_snapshots(worksheet, snapshots)
    ordered = sorted(worksheet.rows, key=lambda row: row.work_id.int)
    async with engine.connect() as connection:
        authority = (await connection.execute(select(
            work_authority.c.generation, work_authority.c.state,
        ).where(work_authority.c.scope == SCOPE))).one_or_none()
        frozen = (await connection.execute(select(
            work_index.c.work_id,
            work_handles.c.provider_work_id,
            work_index.c.provider_revision,
            work_index.c.completed,
        ).join(work_handles, work_handles.c.id == work_index.c.work_id).order_by(
            work_index.c.work_id
        ))).all()
    if authority != (worksheet.stage1_generation, AUTHORITY):
        raise ValueError("Stage 1 authority generation changed")
    if [(row[0], row[1]) for row in frozen] != [
        (row.work_id, row.provider_work_id) for row in ordered
    ]:
        raise ValueError("worksheet is not complete for the frozen Stage 1 corpus")
    completed = {row[0]: row[3] for row in frozen}
    for row in ordered:
        if completed[row.work_id] != (row.lifecycle_state == "TERMINAL"):
            raise ValueError("lifecycle TERMINAL must exactly match Stage 1 completion")


async def _edge_digest(connection: AsyncConnection) -> str:
    rows = (await connection.execute(select(
        work_edges.c.work_id, work_edges.c.depends_on_work_id,
    ).order_by(work_edges.c.work_id, work_edges.c.depends_on_work_id))).all()
    return canonical_digest([[str(row[0]), str(row[1])] for row in rows])


async def edge_digest(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        return await _edge_digest(connection)


async def _stage2_markers(engine: AsyncEngine) -> tuple[object, object]:
    async with engine.connect() as connection:
        authority = (await connection.execute(select(
            work_metadata_authority.c.state, work_metadata_authority.c.generation,
        ).where(work_metadata_authority.c.scope == SCOPE))).one_or_none()
        cutover = (await connection.execute(select(
            work_metadata_cutovers.c.generation,
        ).where(work_metadata_cutovers.c.scope == SCOPE))).one_or_none()
    return authority, cutover


async def _commit(transaction: AsyncTransaction) -> None:
    await transaction.commit()


async def reconcile_metadata_activation(
    engine: AsyncEngine, receipt: ActivationReceipt,
) -> ActivationReceipt:
    try:
        authority, cutover = await _stage2_markers(engine)
        observed_corpus, observed_edges = await corpus_digest(engine), await edge_digest(engine)
    except BaseException as error:
        raise ActivationUnknown("Stage 2 activation outcome UNKNOWN; keep the maintenance gate") from error
    if (
        authority == (AUTHORITY, receipt.generation) and cutover == (receipt.generation,)
        and observed_corpus == receipt.corpus_digest and observed_edges == receipt.edge_digest
    ):
        return replace(receipt, recovered_after_commit_error=True)
    if (
        authority is None and cutover is None
        and observed_corpus == receipt.pre_corpus_digest
        and observed_edges == receipt.pre_edge_digest
    ):
        raise ActivationNotCommitted("Stage 2 readback proves authority was not activated")
    raise ActivationUnknown("Stage 2 activation outcome UNKNOWN; repair forward")


async def activate_metadata(
    engine: AsyncEngine,
    worksheet: ImportWorksheet,
    snapshots: tuple[ProviderMetadataSnapshot, ...],
    before_commit: Callable[[ActivationReceipt], None] | None = None,
) -> ActivationReceipt:
    """Validate the frozen provider snapshot and atomically flip all Stage 2 authority."""
    validate_snapshots(worksheet, snapshots)
    by_provider = {snapshot.provider_work_id: snapshot for snapshot in snapshots}

    ordered = sorted(worksheet.rows, key=lambda row: row.work_id.int)
    pre_corpus, pre_edges = await corpus_digest(engine), await edge_digest(engine)
    connection = await engine.connect()
    transaction = await connection.begin()
    commit_started = False
    expected_corpus = expected_edges = ""
    receipt = ActivationReceipt(len(ordered), 1, "", pre_corpus_digest=pre_corpus, pre_edge_digest=pre_edges)
    try:
        await connection.execute(select(func.pg_advisory_xact_lock(0x53544732)))
        if (await connection.execute(select(work_metadata_authority.c.scope))).first() is not None or (
            await connection.execute(select(work_metadata_cutovers.c.scope))).first() is not None:
            raise ActivationUnknown("Stage 2 authority already exists; reconcile its receipt")
        authority = (await connection.execute(select(
            work_authority.c.generation, work_authority.c.state,
        ).where(work_authority.c.scope == SCOPE).with_for_update())).one_or_none()
        if authority != (worksheet.stage1_generation, AUTHORITY):
            raise ValueError("Stage 1 authority generation changed")
        frozen = (await connection.execute(select(
            work_index.c.work_id,
            work_handles.c.provider_work_id,
            work_index.c.provider_revision,
            work_index.c.completed,
        ).join(work_handles, work_handles.c.id == work_index.c.work_id).order_by(
            work_index.c.work_id
        ).with_for_update())).all()
        if [(row[0], row[1]) for row in frozen] != [
            (row.work_id, row.provider_work_id) for row in ordered
        ]:
            raise ValueError("worksheet is not complete for the frozen Stage 1 corpus")
        completed = {row[0]: row[3] for row in frozen}
        for row in ordered:
            if completed[row.work_id] != (row.lifecycle_state == "TERMINAL"):
                raise ValueError("lifecycle TERMINAL must exactly match Stage 1 completion")
        for row in ordered:
            snapshot = by_provider[row.provider_work_id]
            routing = Routing(
                priority=row.priority,
                work_type=row.work_type,
                lifecycle_state=row.lifecycle_state,
                canonical_root=row.canonical_root,
                owner_key=row.owner_key,
                wait_kind=row.wait_kind,
                unblock_condition=row.unblock_condition,
                next_due=row.next_due,
                next_action_class=row.next_action_class,
                next_action_ref=row.next_action_ref,
                review_next_action=row.review_next_action,
                horizon=row.horizon,
                stage3_gate=row.stage3_gate,
            )
            await connection.execute(update(work_index).where(
                work_index.c.work_id == row.work_id
            ).values(
                provider_revision=WorkIndex.content_revision(
                    snapshot.notes, snapshot.context
                ),
                context=snapshot.context.model_dump(mode="json"),
                routing=routing.model_dump(mode="json"),
                row_version=work_index.c.row_version + 1,
            ))
        edges = [
            {"work_id": row.work_id, "depends_on_work_id": dependency}
            for row in ordered for dependency in row.depends_on
        ]
        if edges:
            await connection.execute(insert(work_edges), edges)
        await connection.execute(insert(work_metadata_authority).values(
            scope=SCOPE, state=AUTHORITY, generation=1,
        ))
        await connection.execute(insert(work_metadata_cutovers).values(
            scope=SCOPE, generation=1,
        ))
        expected_corpus = await corpus_digest_from_connection(connection)
        expected_edges = await _edge_digest(connection)
        receipt = ActivationReceipt(
            len(ordered), 1, expected_corpus, expected_edges,
            pre_corpus_digest=pre_corpus, pre_edge_digest=pre_edges,
        )
        if before_commit is not None:
            before_commit(receipt)
        commit_started = True
        await _commit(transaction)
    except BaseException:
        if not commit_started:
            with suppress(BaseException):
                await transaction.rollback()
            raise
        with suppress(BaseException):
            await connection.close()
        return await reconcile_metadata_activation(engine, receipt)
    finally:
        with suppress(BaseException):
            await connection.close()
    authority, cutover = await _stage2_markers(engine)
    observed_corpus, observed_edges = await corpus_digest(engine), await edge_digest(engine)
    if (
        authority != (AUTHORITY, 1) or cutover != (1,)
        or observed_corpus != expected_corpus or observed_edges != expected_edges
    ):
        raise ActivationUnknown("Stage 2 committed readback is inconsistent; repair forward")
    return receipt
