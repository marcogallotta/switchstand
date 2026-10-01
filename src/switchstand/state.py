from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Row,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncEngine

from .core import Handle

metadata = MetaData()
work_handles = Table(
    "work_handles",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True),
    Column("provider", Text, nullable=False), Column("provider_work_id", Text, nullable=False),
    UniqueConstraint("provider", "provider_work_id"),
)
work_event_handles = Table(
    "work_event_handles",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True),
    Column("work_id", PGUUID(as_uuid=True), ForeignKey("work_handles.id", ondelete="RESTRICT"),
           nullable=False, index=True),
    Column("provider", Text, nullable=False),
    Column("provider_work_id", Text, nullable=False),
    Column("provider_event_id", Text, nullable=False),
    UniqueConstraint("work_id", "provider", "provider_work_id", "provider_event_id"),
)
work_authority = Table(
    "work_authority", metadata,
    Column("scope", Text, primary_key=True),
    Column("state", Text, nullable=False),
    Column("generation", BigInteger, nullable=False),
    Column("cutover_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    CheckConstraint("scope = 'workspace'", name="ck_work_authority_scope"),
    CheckConstraint("state = 'POSTGRES_AUTHORITY'", name="ck_work_authority_state"),
    CheckConstraint("generation >= 1", name="ck_work_authority_generation"),
)
work_authority_cutovers = Table(
    "work_authority_cutovers", metadata,
    Column("scope", Text, primary_key=True),
    Column("generation", BigInteger, nullable=False),
    Column("cutover_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    CheckConstraint("scope = 'workspace'", name="ck_work_cutover_scope"),
    CheckConstraint("generation >= 1", name="ck_work_cutover_generation"),
)
work_index = Table(
    "work_index", metadata,
    Column(
        "work_id", PGUUID(as_uuid=True), ForeignKey("work_handles.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("title", Text, nullable=False),
    Column("normalized_title", Text, nullable=False),
    Column("completed", Boolean, nullable=False),
    Column("provider_revision", Text, nullable=False),
    Column("row_version", BigInteger, nullable=False),
    Column("routing", JSONB, nullable=False),
    Column("context", JSONB, nullable=False),
    CheckConstraint("title <> ''", name="ck_work_index_title"),
    CheckConstraint("normalized_title <> ''", name="ck_work_index_normalized_title"),
    CheckConstraint("provider_revision <> ''", name="ck_work_index_provider_revision"),
    CheckConstraint("row_version >= 1", name="ck_work_index_row_version"),
)
work_metadata_authority = Table(
    "work_metadata_authority", metadata,
    Column("scope", Text, primary_key=True),
    Column("state", Text, nullable=False),
    Column("generation", BigInteger, nullable=False),
    Column("cutover_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    CheckConstraint("scope = 'workspace'", name="ck_work_metadata_authority_scope"),
    CheckConstraint(
        "state = 'POSTGRES_AUTHORITY'", name="ck_work_metadata_authority_state"
    ),
    CheckConstraint("generation >= 1", name="ck_work_metadata_authority_generation"),
)
work_metadata_cutovers = Table(
    "work_metadata_cutovers", metadata,
    Column("scope", Text, primary_key=True),
    Column("generation", BigInteger, nullable=False),
    Column("cutover_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    CheckConstraint("scope = 'workspace'", name="ck_work_metadata_cutover_scope"),
    CheckConstraint("generation >= 1", name="ck_work_metadata_cutover_generation"),
)
work_edges = Table(
    "work_edges", metadata,
    Column(
        "work_id", PGUUID(as_uuid=True), ForeignKey("work_index.work_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column(
        "depends_on_work_id", PGUUID(as_uuid=True),
        ForeignKey("work_index.work_id", ondelete="RESTRICT"), primary_key=True,
    ),
    CheckConstraint("work_id <> depends_on_work_id", name="ck_work_edge_not_self"),
)
workset_authority = Table(
    "workset_authority", metadata, Column("scope", Text, primary_key=True),
    Column("state", Text, nullable=False), Column("generation", BigInteger, nullable=False),
    Column("cutover_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    CheckConstraint("scope = 'workspace'", name="ck_workset_authority_scope"),
    CheckConstraint("state = 'POSTGRES_AUTHORITY'", name="ck_workset_authority_state"),
    CheckConstraint("generation >= 1", name="ck_workset_authority_generation"),
)
workset_cutovers = Table(
    "workset_cutovers", metadata, Column("scope", Text, primary_key=True),
    Column("generation", BigInteger, nullable=False),
    Column("cutover_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    CheckConstraint("scope = 'workspace'", name="ck_workset_cutover_scope"),
    CheckConstraint("generation >= 1", name="ck_workset_cutover_generation"),
)
worksets = Table(
    "worksets", metadata, Column("workset_id", PGUUID(as_uuid=True), primary_key=True),
    Column("workset_key", Text, nullable=False, unique=True), Column("name", Text, nullable=False),
    Column("kind", Text, nullable=False), Column("role_identity", Text, unique=True),
    Column("state", Text, nullable=False), Column("row_version", BigInteger, nullable=False),
    CheckConstraint("workset_key <> ''", name="ck_workset_key"),
    CheckConstraint("name <> ''", name="ck_workset_name"),
    CheckConstraint("kind <> ''", name="ck_workset_kind"),
    CheckConstraint("role_identity IS NULL OR role_identity <> ''", name="ck_workset_role"),
    CheckConstraint("state IN ('ACTIVE', 'RETIRED')", name="ck_workset_state"),
    CheckConstraint("row_version >= 1", name="ck_workset_version"),
)
workset_memberships = Table(
    "workset_memberships", metadata,
    Column("workset_id", PGUUID(as_uuid=True), ForeignKey("worksets.workset_id", ondelete="RESTRICT"), primary_key=True),
    Column("work_id", PGUUID(as_uuid=True), ForeignKey("work_index.work_id", ondelete="RESTRICT"), primary_key=True),
    Column("semantics", Text, nullable=False), Column("member_role", Text, nullable=False),
    Column("row_version", BigInteger, nullable=False),
    CheckConstraint("semantics IN ('AUTHORITATIVE', 'RELATED')", name="ck_workset_membership_semantics"),
    CheckConstraint("member_role IN ('MASTER', 'MEMBER')", name="ck_workset_membership_role"),
    CheckConstraint("member_role <> 'MASTER' OR semantics = 'AUTHORITATIVE'", name="ck_workset_master_authoritative"),
    CheckConstraint("row_version >= 1", name="ck_workset_membership_version"),
)
Index("uq_workset_authoritative_work", workset_memberships.c.work_id, unique=True,
      postgresql_where=workset_memberships.c.semantics == "AUTHORITATIVE")
Index("uq_workset_master", workset_memberships.c.workset_id, unique=True,
      postgresql_where=workset_memberships.c.member_role == "MASTER")
work_parent_edges = Table(
    "work_parent_edges", metadata,
    Column("child_work_id", PGUUID(as_uuid=True), ForeignKey("work_index.work_id", ondelete="RESTRICT"), primary_key=True),
    Column("parent_work_id", PGUUID(as_uuid=True), ForeignKey("work_index.work_id", ondelete="RESTRICT"), nullable=False),
    Column("row_version", BigInteger, nullable=False),
    CheckConstraint("child_work_id <> parent_work_id", name="ck_work_parent_not_self"),
    CheckConstraint("row_version >= 1", name="ck_work_parent_version"),
)
human_trajectory_revisions = Table(
    "human_trajectory_revisions", metadata,
    Column("trajectory_id", PGUUID(as_uuid=True), primary_key=True),
    Column("append_request_id", PGUUID(as_uuid=True), nullable=False, unique=True),
    Column(
        "work_id_ref", PGUUID(as_uuid=True),
        ForeignKey("work_handles.id", ondelete="RESTRICT"), nullable=False,
    ),
    Column("generation", BigInteger, nullable=False),
    Column(
        "predecessor_id", PGUUID(as_uuid=True),
        ForeignKey("human_trajectory_revisions.trajectory_id", ondelete="RESTRICT"),
    ),
    Column("source_kind", Text, nullable=False),
    Column("source_ref", Text, nullable=False),
    Column("source_revision", Text),
    Column("content_digest", Text, nullable=False),
    Column("trajectory_data", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    UniqueConstraint("work_id_ref", "generation"),
    CheckConstraint("generation >= 1", name="ck_human_trajectory_generation"),
    CheckConstraint(
        "source_kind IN ('HUMAN_INPUT', 'HUMAN_REVIEW', 'HUMAN_STEERING')",
        name="ck_human_trajectory_source_kind",
    ),
    CheckConstraint("length(source_ref) BETWEEN 1 AND 512", name="ck_human_trajectory_source_ref"),
    CheckConstraint(
        "source_revision IS NULL OR length(source_revision) BETWEEN 1 AND 512",
        name="ck_human_trajectory_source_revision",
    ),
    CheckConstraint("length(content_digest) = 64", name="ck_human_trajectory_digest"),
)
outcome_state_revisions = Table(
    "outcome_state_revisions", metadata,
    Column("state_id", PGUUID(as_uuid=True), primary_key=True),
    Column("operation_id", PGUUID(as_uuid=True), nullable=False, unique=True),
    Column(
        "owner_work_id", PGUUID(as_uuid=True),
        ForeignKey("work_handles.id", ondelete="RESTRICT"), nullable=False,
    ),
    Column("generation", BigInteger, nullable=False),
    Column(
        "predecessor_id", PGUUID(as_uuid=True),
        ForeignKey("outcome_state_revisions.state_id", ondelete="RESTRICT"),
    ),
    Column("schema_version", BigInteger, nullable=False),
    Column("items", JSONB, nullable=False),
    Column("owner_currentness_token", Text, nullable=False),
    Column("content_digest", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    UniqueConstraint("owner_work_id", "generation"),
    CheckConstraint("generation >= 1", name="ck_outcome_state_generation"),
    CheckConstraint("schema_version = 1", name="ck_outcome_state_schema"),
    CheckConstraint("owner_currentness_token <> ''", name="ck_outcome_state_currentness"),
    CheckConstraint("length(content_digest) = 64", name="ck_outcome_state_digest"),
)
Index(
    "ix_work_index_title_search", text("to_tsvector('simple', normalized_title)"),
    postgresql_using="gin",
)
Index("ix_work_index_page", work_index.c.normalized_title, work_index.c.work_id)
columns = (work_handles.c.id, work_handles.c.provider, work_handles.c.provider_work_id)
event_columns = (
    work_event_handles.c.id, work_event_handles.c.work_id, work_event_handles.c.provider,
    work_event_handles.c.provider_work_id, work_event_handles.c.provider_event_id,
)


@dataclass(frozen=True)
class EventHandle:
    id: UUID
    work_id: UUID
    provider: str
    provider_work_id: str
    provider_event_id: str

def _handle(row: Row[tuple[UUID, str, str]] | None) -> Handle | None:
    return None if row is None else Handle(row[0], row[1], row[2])


class PostgresState:
    def __init__(self, engine: AsyncEngine):
        from .outcome_state import OutcomeStateStore
        from .work_index import WorkIndex
        from .worksets import WorksetReader

        self.engine = engine
        self.outcomes = OutcomeStateStore(engine)
        self.work_index = WorkIndex(engine)
        self.worksets = WorksetReader(engine)
    async def get(self, work_id: UUID) -> Handle | None:
        async with self.engine.connect() as connection:
            query = select(*columns).where(work_handles.c.id == work_id)
            row = (await connection.execute(query)).one_or_none()
        return _handle(row)

    async def get_by_provider(self, provider: str, provider_work_id: str) -> Handle | None:
        async with self.engine.connect() as connection:
            query = select(*columns).where(
                (work_handles.c.provider == provider)
                & (work_handles.c.provider_work_id == provider_work_id)
            )
            row = (await connection.execute(query)).one_or_none()
        return _handle(row)


    async def bind(self, provider: str, provider_work_id: str) -> Handle:
        return await self.bind_reserved(uuid4(), provider, provider_work_id, allow_existing=True)

    async def bind_many(
        self, provider: str, provider_work_ids: tuple[str, ...]
    ) -> tuple[Handle, ...]:
        handles: list[Handle] = []
        async with self.engine.begin() as connection:
            for provider_work_id in provider_work_ids:
                work_id = uuid4()
                values = {
                    "id": work_id, "provider": provider,
                    "provider_work_id": provider_work_id,
                }
                row = (await connection.execute(
                    insert(work_handles).values(values).on_conflict_do_nothing().returning(*columns)
                )).one_or_none()
                if row is not None:
                    handles.append(Handle(row[0], row[1], row[2]))
                    continue
                existing = (await connection.execute(select(*columns).where(
                    (work_handles.c.id == work_id)
                    | ((work_handles.c.provider == provider)
                       & (work_handles.c.provider_work_id == provider_work_id))
                ))).all()
                if len(existing) != 1:
                    raise ValueError("work binding conflict")
                handle = Handle(existing[0][0], existing[0][1], existing[0][2])
                if handle.provider != provider or handle.provider_work_id != provider_work_id:
                    raise ValueError("work binding conflict")
                handles.append(handle)
        return tuple(handles)

    async def bind_reserved(
        self, work_id: UUID, provider: str, provider_work_id: str, *, allow_existing: bool = False,
    ) -> Handle:
        values = {"id": work_id, "provider": provider, "provider_work_id": provider_work_id}
        statement = insert(work_handles).values(values).on_conflict_do_nothing().returning(*columns)
        async with self.engine.begin() as connection:
            row = (await connection.execute(statement)).one_or_none()
            if row is not None:
                return Handle(row[0], row[1], row[2])
            existing = (await connection.execute(select(*columns).where(
                (work_handles.c.id == work_id)
                | ((work_handles.c.provider == provider)
                   & (work_handles.c.provider_work_id == provider_work_id))
            ))).all()
        if len(existing) != 1:
            raise ValueError("work binding conflict")
        handle = Handle(existing[0][0], existing[0][1], existing[0][2])
        if handle.provider != provider or handle.provider_work_id != provider_work_id:
            raise ValueError("work binding conflict")
        if handle.id != work_id and not allow_existing:
            raise ValueError("reserved WorkId already bound differently")
        return handle

    async def bind_event(
        self, work_id: UUID, provider: str, provider_work_id: str, provider_event_id: str,
    ) -> EventHandle:
        if not provider or not provider_work_id or not provider_event_id:
            raise ValueError("event binding values must be non-empty")
        values = {
            "id": uuid4(), "work_id": work_id, "provider": provider,
            "provider_work_id": provider_work_id, "provider_event_id": provider_event_id,
        }
        async with self.engine.begin() as connection:
            bound = (await connection.execute(select(*columns).where(
                work_handles.c.id == work_id
            ).with_for_update())).one_or_none()
            handle = _handle(bound)
            if (handle is None or handle.provider != provider
                    or handle.provider_work_id != provider_work_id):
                raise ValueError("event target does not match work binding")
            row = (await connection.execute(insert(work_event_handles).values(
                values
            ).on_conflict_do_nothing().returning(*event_columns))).one_or_none()
            if row is None:
                row = (await connection.execute(select(*event_columns).where(
                    (work_event_handles.c.work_id == work_id)
                    & (work_event_handles.c.provider == provider)
                    & (work_event_handles.c.provider_work_id == provider_work_id)
                    & (work_event_handles.c.provider_event_id == provider_event_id)
                ))).one()
        return EventHandle(row[0], row[1], row[2], row[3], row[4])

    async def get_event(self, work_id: UUID, event_id: UUID) -> EventHandle | None:
        async with self.engine.connect() as connection:
            row = (await connection.execute(select(*event_columns).where(
                (work_event_handles.c.id == event_id)
                & (work_event_handles.c.work_id == work_id)
            ))).one_or_none()
        return None if row is None else EventHandle(row[0], row[1], row[2], row[3], row[4])

    @asynccontextmanager
    async def locked(self, work_id: UUID) -> AsyncGenerator[Handle | None]:
        async with self.engine.begin() as connection:
            statement = select(*columns).where(work_handles.c.id == work_id).with_for_update()
            yield _handle((await connection.execute(statement)).one_or_none())
