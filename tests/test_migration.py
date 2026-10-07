import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from switchstand.canonical_relations import (
    project_memberships,
    projects,
    work_dependencies,
    work_parents,
)
from switchstand.canonical_work import canonical_work, legacy_work_aliases
from switchstand.priority_claims import priority_claims
from switchstand.provision import require_current_schema
from switchstand.work_events import work_events


@pytest.fixture(autouse=True)
def clean_postgres_tables(database_prerequisite):
    engine = create_engine(disposable_url())
    with engine.begin() as connection:
        connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, task_control_checkpoints, mcp_operation_timings, activation_obligation_revisions, task_run_requests, agent_mailbox_transfer_requests, outcome_state_revisions, human_trajectory_revisions, agent_mailboxes, work_event_handles, lifecycle_obligations, "
            "message_projection, message_deliveries, messages, effect_intents, work_grants, work_handles, priority_claims, work_events, project_memberships, projects, work_parents, work_dependencies, "
            "legacy_work_aliases, canonical_work, failure_resolutions, failure_records, work_migration_receipts CASCADE"
        ))
    engine.dispose()


def disposable_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for the PostgreSQL migration test")
    if make_url(url).database != "switchstand_test":
        pytest.fail("migration test requires the disposable switchstand_test database")
    return url


def test_stale_schema_check_does_not_upgrade(monkeypatch, database_prerequisite):
    url = disposable_url()
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, mcp_operation_timings, agent_mailbox_transfer_requests, outcome_state_revisions, human_trajectory_revisions, agent_mailboxes, work_event_handles, lifecycle_obligations, message_projection, "
            "message_deliveries, messages, effect_intents, work_grants, work_handles CASCADE"
        ))
    with pytest.raises(
        RuntimeError,
        match="shared CONTROL schema mismatch: expected 0025_task_control_checkpoints; actual <none>",
    ):
        require_current_schema()
    assert inspect(engine).get_table_names() == []


def test_activation_continuity_downgrade_preserves_durable_truth(database_prerequisite):
    url = disposable_url()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO activation_obligation_revisions "
            "(obligation_id, operation_id, generation, record) "
            "VALUES (:obligation, :operation, 1, '{}'::jsonb)"
        ), {"obligation": uuid4(), "operation": uuid4()})

    with pytest.raises(RuntimeError, match="preserve durable activation obligations"):
        command.downgrade(config, "0022_implementation_requests")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) \
            == "0025_task_control_checkpoints"
        assert connection.scalar(text(
            "SELECT count(*) FROM activation_obligation_revisions"
        )) == 1


def test_timing_downgrade_preserves_durable_evidence(database_prerequisite):
    url = disposable_url()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO mcp_operation_timings VALUES "
            "('call','work_get',NULL,now(),1,'ok',NULL,0,0,0,0,1,'runtime',"
            "'0024_mcp_operation_timings')"
        ))

    with pytest.raises(RuntimeError, match="preserve durable MCP operation timings"):
        command.downgrade(config, "0023_activation_continuity")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) \
            == "0025_task_control_checkpoints"
        assert connection.scalar(text("SELECT count(*) FROM mcp_operation_timings")) == 1


def test_empty_database_migrates_to_lifecycle_head(monkeypatch, database_prerequisite):
    url = disposable_url()
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, mcp_operation_timings, agent_mailbox_transfer_requests, outcome_state_revisions, human_trajectory_revisions, agent_mailboxes, work_event_handles, lifecycle_obligations, message_projection, "
            "message_deliveries, messages, effect_intents, work_grants, work_handles CASCADE"
        ))
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0021_human_reviews")
    request_id, work_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO work_handles (id, provider, provider_work_id) "
            "VALUES (:id, 'local', :provider_id)"
        ), {"id": work_id, "provider_id": str(work_id)})
        connection.execute(text(
            "INSERT INTO task_run_requests (request_id, operation_id, requester_work_id, "
            "execution_work_id, observed_revision, task_kind, continuation, objective, "
            "result_contract, content_digest) VALUES (:id, :operation, :work, :work, "
            "'pg_existing', 'VALIDATION', 'START', 'existing request', '{}'::jsonb, :digest)"
        ), {"id": request_id, "operation": uuid4(), "work": work_id, "digest": "a" * 64})
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT task_kind, authorization_ref, send_authority_ref FROM task_run_requests"
        )).one() == ("VALIDATION", None, None)
    command.downgrade(config, "0021_human_reviews")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT task_kind FROM task_run_requests")) == "VALIDATION"
    command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE task_run_requests SET task_kind = 'IMPLEMENTATION', "
            "authorization_ref = 'review/1', send_authority_ref = 'grant/1'"
        ))
    with pytest.raises(RuntimeError, match="preserve durable implementation requests"):
        command.downgrade(config, "0021_human_reviews")
    assert set(inspect(engine).get_table_names()) == {
        "human_trajectory_revisions", "agent_mailboxes", "agent_mailbox_transfer_requests",
        "work_event_handles", "lifecycle_obligations", "alembic_version", "work_handles",
        "work_grants", "effect_intents", "messages", "message_deliveries", "message_projection",
        "outcome_state_revisions",
        "canonical_work", "legacy_work_aliases", "work_dependencies", "work_parents",
        "projects", "project_memberships", "work_events", "priority_claims",
        "work_migration_receipts", "failure_records", "failure_resolutions",
        "task_run_requests", "task_run_executions", "task_run_results",
            "human_review_consequences", "activation_obligation_revisions",
            "mcp_operation_timings", "task_control_checkpoints",
        }
    assert {column["name"] for column in inspect(engine).get_columns("work_handles")} == {"id", "provider", "provider_work_id"}
    database = inspect(engine)
    for table in (
        canonical_work, legacy_work_aliases, work_dependencies, work_parents,
        projects, project_memberships, work_events, priority_claims,
    ):
        assert {column["name"] for column in database.get_columns(table.name)} == {
            column.name for column in table.columns
        }
        assert {(index["name"], index["unique"]) for index in database.get_indexes(table.name)} \
            >= {(index.name, index.unique) for index in table.indexes}
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM task_run_requests WHERE request_id = :id"), {"id": request_id})
        connection.execute(text(
            "INSERT INTO agent_mailbox_transfer_requests "
            "(request_id, name_key, endpoint_id, expected_generation, source_principal_key, "
            "source_session_key, destination_principal_key, destination_session_key, status, "
            "created_at) VALUES (:request_id, 'root', :endpoint_id, 1, 'old', 'old-session', "
            "'new', 'new-session', 'PENDING', now())"
        ), {"request_id": uuid4(), "endpoint_id": uuid4()})

    with pytest.raises(RuntimeError, match="preserve mailbox transfer audit evidence"):
        command.downgrade(config, "0015_work_admission_time")


def test_priority_claim_migration_refuses_destructive_downgrade(database_prerequisite):
    url = disposable_url()
    engine = create_engine(url)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    claim_id = uuid4()
    command.upgrade(config, "0017_priority_claims")
    with engine.begin() as connection:
        priority_claims.create(connection, checkfirst=True)
        connection.execute(text(
            "INSERT INTO priority_claims "
            "(claim_id, claim_kind, subject_kind, subject_id, relation_kind, band, "
            "rationale, source_label, source_ref, state) VALUES "
            "(:claim_id, 'HUMAN_PRIORITY', 'WORK', :claim_id, 'BAND', 'HIGH', "
            "'human direction', 'Marco', 'message:1', 'CURRENT')"
        ), {"claim_id": claim_id})
    with pytest.raises(RuntimeError, match="preserve durable priority claims"):
        command.downgrade(config, "0016_agent_mailbox_transfers")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM priority_claims")) == 1


def test_routing_migration_preserves_legacy_nulls_and_refuses_destructive_downgrade(
    database_prerequisite,
):
    url = disposable_url()
    engine = create_engine(url)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    command.downgrade(config, "0013_failure_journal")
    work_id = uuid4()
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO canonical_work "
            "(work_id, title, normalized_title, completed, notes, row_version) "
            "VALUES (:work_id, 'Legacy', 'legacy', false, '', 1)"
        ), {"work_id": work_id})

    command.upgrade(config, "head")

    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT canonical_root, owner_key, next_action_class, next_action_ref, admitted_at "
            "FROM canonical_work WHERE work_id = :work_id"
        ), {"work_id": work_id}).one() == (None, None, None, None, None)
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE canonical_work SET canonical_root = 'NONE', owner_key = 'coordinator', "
            "next_action_class = 'OWNER_CAN_DO', next_action_ref = 'implement' "
            "WHERE work_id = :work_id"
        ), {"work_id": work_id})

    with pytest.raises(RuntimeError, match="preserve canonical routing state"):
        command.downgrade(config, "0013_failure_journal")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) \
            == "0025_task_control_checkpoints"
        assert connection.scalar(text(
            "SELECT owner_key FROM canonical_work WHERE work_id = :work_id"
        ), {"work_id": work_id}) == "coordinator"
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE canonical_work SET admitted_at = now() WHERE work_id = :work_id"
        ), {"work_id": work_id})
    with pytest.raises(RuntimeError, match="preserve canonical work admission evidence"):
        command.downgrade(config, "0014_canonical_routing")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) \
            == "0025_task_control_checkpoints"


def test_migration_receipt_blocks_downgrade_and_preserves_fence(database_prerequisite):
    url = disposable_url()
    engine = create_engine(url)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    with engine.begin() as connection:
        connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, mcp_operation_timings, agent_mailbox_transfer_requests, outcome_state_revisions, "
            "human_trajectory_revisions, agent_mailboxes, work_event_handles, "
            "lifecycle_obligations, message_projection, message_deliveries, messages, "
            "effect_intents, work_grants, work_handles CASCADE"
        ))
    command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO work_migration_receipts (name, source_digest) "
            "VALUES ('work-identity-migration-complete-v1', :digest)"
        ), {"digest": "a" * 64})

    with pytest.raises(RuntimeError, match="preserve durable failure or migration evidence"):
        command.downgrade(config, "0012_outcome_state")

    assert {
        "work_migration_receipts", "failure_records", "failure_resolutions",
    } <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) \
            == "0025_task_control_checkpoints"
        assert connection.execute(text(
            "SELECT name, source_digest FROM work_migration_receipts"
        )).one() == ("work-identity-migration-complete-v1", "a" * 64)


def test_agent_identity_migration_preserves_endpoint_and_delivery(
    monkeypatch, database_prerequisite,
):
    url = disposable_url()
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    with engine.begin() as connection:
        connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, mcp_operation_timings, agent_mailbox_transfer_requests, outcome_state_revisions, human_trajectory_revisions, agent_mailboxes, work_event_handles, "
            "lifecycle_obligations, message_projection, message_deliveries, messages, "
            "effect_intents, work_grants, work_handles CASCADE"
        ))
    command.upgrade(config, "0006_agent_mailboxes")
    endpoint_id, message_id, delivery_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO work_handles (id, provider, provider_work_id) "
            "VALUES (:id, 'agent-mailbox', 'legacy')"
        ), {"id": endpoint_id})
        connection.execute(text(
            "INSERT INTO agent_mailboxes "
            "(name_key, display_name, work_id, principal_key, generation) "
            "VALUES ('legacy', 'Legacy', :id, 'owner', 1)"
        ), {"id": endpoint_id})
        connection.execute(text(
            "INSERT INTO messages (sender_work_id, message_id, route_ref, kind, payload, digest) "
            "VALUES (:id, :message, 'agent.legacy', 'request', '{}'::jsonb, 'digest')"
        ), {"id": endpoint_id, "message": message_id})
        connection.execute(text(
            "INSERT INTO message_deliveries "
            "(delivery_id, sender_work_id, message_id, recipient_work_id, "
            "recipient_grant_version) VALUES (:delivery, :id, :message, :id, 1)"
        ), {"delivery": delivery_id, "id": endpoint_id, "message": message_id})

    command.upgrade(config, "head")
    with engine.connect() as connection:
        endpoint = connection.execute(text(
            "SELECT name_key, display_name, endpoint_id, principal_key, session_key, "
            "generation FROM agent_mailboxes"
        )).one()
        message = connection.execute(text(
            "SELECT sender_work_id, message_id, route_ref, kind, payload, digest "
            "FROM messages"
        )).one()
        delivery = connection.execute(text(
            "SELECT delivery_id, sender_work_id, message_id, recipient_work_id, "
            "recipient_grant_version, state FROM message_deliveries"
        )).one()
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) \
            == "0025_task_control_checkpoints"
    assert endpoint == ("legacy", "Legacy", endpoint_id, "owner", "legacy:legacy", 1)
    assert message == (endpoint_id, message_id, "agent.legacy", "request", {}, "digest")
    assert delivery == (delivery_id, endpoint_id, message_id, endpoint_id, 1, "AVAILABLE")
    foreign_keys = inspect(engine).get_foreign_keys("agent_mailboxes")
    assert foreign_keys == []
    unique_columns = {
        tuple(constraint["column_names"])
        for constraint in inspect(engine).get_unique_constraints("agent_mailboxes")
    }
    assert ("endpoint_id",) in unique_columns
    assert ("principal_key", "session_key") in unique_columns
    assert ("principal_key",) not in unique_columns


def test_populated_agent_identity_downgrade_preserves_current_schema_and_data(
    monkeypatch, database_prerequisite,
):
    url = disposable_url()
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    with engine.begin() as connection:
        connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, mcp_operation_timings, agent_mailbox_transfer_requests, outcome_state_revisions, human_trajectory_revisions, agent_mailboxes, work_event_handles, "
            "lifecycle_obligations, message_projection, message_deliveries, messages, "
            "effect_intents, work_grants, work_handles CASCADE"
        ))
    command.upgrade(config, "head")
    endpoint_id, message_id, delivery_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO agent_mailboxes "
            "(name_key, display_name, endpoint_id, principal_key, session_key, generation) "
            "VALUES ('agent', 'Agent', :endpoint, 'owner', 'session', 4)"
        ), {"endpoint": endpoint_id})
        connection.execute(text(
            "INSERT INTO messages (sender_work_id, message_id, route_ref, kind, payload, digest) "
            "VALUES (:endpoint, :message, 'agent.agent', 'request', '{}'::jsonb, 'digest')"
        ), {"endpoint": endpoint_id, "message": message_id})
        connection.execute(text(
            "INSERT INTO message_deliveries "
            "(delivery_id, sender_work_id, message_id, recipient_work_id, "
            "recipient_grant_version, state, receiving_generation) "
            "VALUES (:delivery, :endpoint, :message, :endpoint, 4, 'RECEIVED', '4')"
        ), {"delivery": delivery_id, "endpoint": endpoint_id, "message": message_id})

    with pytest.raises(RuntimeError, match="preserve durable agent endpoint bindings"):
        command.downgrade(config, "0006_agent_mailboxes")

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) \
            == "0025_task_control_checkpoints"
        assert connection.execute(text(
            "SELECT endpoint_id, principal_key, session_key, generation FROM agent_mailboxes"
        )).one() == (endpoint_id, "owner", "session", 4)
        assert connection.execute(text(
            "SELECT sender_work_id, message_id FROM messages"
        )).one() == (endpoint_id, message_id)
        assert connection.execute(text(
            "SELECT delivery_id, recipient_work_id, state, receiving_generation "
            "FROM message_deliveries"
        )).one() == (delivery_id, endpoint_id, "RECEIVED", "4")


def test_empty_agent_identity_downgrade_and_reupgrade_reaches_exact_head(
    monkeypatch, database_prerequisite,
):
    url = disposable_url()
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    with engine.begin() as connection:
        connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, mcp_operation_timings, agent_mailbox_transfer_requests, outcome_state_revisions, human_trajectory_revisions, agent_mailboxes, work_event_handles, "
            "lifecycle_obligations, message_projection, message_deliveries, messages, "
            "effect_intents, work_grants, work_handles CASCADE"
        ))
    command.upgrade(config, "head")

    command.downgrade(config, "0006_agent_mailboxes")
    assert {column["name"] for column in inspect(engine).get_columns("agent_mailboxes")} \
        >= {"work_id", "principal_key", "generation"}
    command.upgrade(config, "head")

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) \
            == "0025_task_control_checkpoints"
    assert {column["name"] for column in inspect(engine).get_columns("agent_mailboxes")} \
        >= {"endpoint_id", "principal_key", "session_key", "generation"}


def test_message_downgrade_refuses_to_destroy_durable_truth(
    monkeypatch, database_prerequisite,
):
    url = disposable_url()
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    with engine.begin() as connection:
        connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, mcp_operation_timings, agent_mailbox_transfer_requests, outcome_state_revisions, human_trajectory_revisions, agent_mailboxes, work_event_handles, lifecycle_obligations, message_projection, "
            "message_deliveries, messages, effect_intents, work_grants, work_handles CASCADE"
        ))
    command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO messages "
            "(sender_work_id, message_id, route_ref, kind, payload, digest) "
            "VALUES (:sender, :message, 'review', 'request', '{}'::jsonb, 'digest')"
        ), {"sender": str(uuid4()), "message": str(uuid4())})
    with pytest.raises(RuntimeError, match="preserve durable message truth"):
        command.downgrade(config, "0002_grants_and_effects")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM messages")) == 1
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) \
            == "0025_task_control_checkpoints"


def test_lifecycle_downgrade_refuses_to_discard_obligation(database_prerequisite):
    url = disposable_url()
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, mcp_operation_timings, agent_mailbox_transfer_requests, outcome_state_revisions, human_trajectory_revisions, agent_mailboxes, work_event_handles, lifecycle_obligations, message_projection, "
            "message_deliveries, messages, effect_intents, work_grants, work_handles CASCADE"
        ))
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    work_id, obligation_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO work_handles (id, provider, provider_work_id) "
                "VALUES (:id, 'migration-test', :provider_id)"
            ),
            {"id": work_id, "provider_id": str(work_id)},
        )
        connection.execute(
            text(
                "INSERT INTO lifecycle_obligations "
                "(obligation_id, profile_type, profile_version, work_id_ref, "
                "currentness_token, state, row_version) VALUES "
                "(:id, 'REQUIRED_RESULT_PERSISTENCE', 1, :work_id, "
                "'revision:1', 'PENDING_RESULT', 1)"
            ),
            {"id": obligation_id, "work_id": work_id},
        )

    with pytest.raises(RuntimeError, match="preserve durable lifecycle obligation truth"):
        command.downgrade(config, "0003_messages_and_deliveries")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM lifecycle_obligations")) == 1


def test_empty_lifecycle_downgrade_and_reupgrade_recovers_schema(database_prerequisite):
    url = disposable_url()
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, mcp_operation_timings, agent_mailbox_transfer_requests, outcome_state_revisions, human_trajectory_revisions, agent_mailboxes, work_event_handles, lifecycle_obligations, message_projection, "
            "message_deliveries, messages, effect_intents, work_grants, work_handles CASCADE"
        ))
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    command.downgrade(config, "0003_messages_and_deliveries")
    assert "lifecycle_obligations" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    assert "lifecycle_obligations" in inspect(engine).get_table_names()


def test_event_identity_downgrade_refuses_to_discard_mapping(database_prerequisite):
    url = disposable_url()
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(
            "DROP TABLE IF EXISTS alembic_version, mcp_operation_timings, agent_mailbox_transfer_requests, outcome_state_revisions, human_trajectory_revisions, agent_mailboxes, work_event_handles, lifecycle_obligations, "
            "message_projection, message_deliveries, messages, effect_intents, work_grants, "
            "work_handles CASCADE"
        ))
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    work_id, event_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO work_handles (id, provider, provider_work_id) "
            "VALUES (:id, 'asana', 'task-1')"
        ), {"id": work_id})
        connection.execute(text(
            "INSERT INTO work_event_handles "
            "(id, work_id, provider, provider_work_id, provider_event_id) "
            "VALUES (:id, :work_id, 'asana', 'task-1', 'story-1')"
        ), {"id": event_id, "work_id": work_id})

    with pytest.raises(RuntimeError, match="preserve durable opaque work-event identities"):
        command.downgrade(config, "0004_required_result_persistence")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM work_event_handles")) == 1
