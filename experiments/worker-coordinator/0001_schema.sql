BEGIN;

CREATE SCHEMA coordinator_v2;
REVOKE ALL ON SCHEMA coordinator_v2 FROM PUBLIC;

CREATE TABLE coordinator_v2.service_binding (
    login_name name NOT NULL,
    login_oid oid NOT NULL,
    workspace_id uuid NOT NULL,
    credential_epoch bigint NOT NULL DEFAULT 1 CHECK (credential_epoch > 0),
    enabled boolean NOT NULL DEFAULT true,
    PRIMARY KEY (login_name, login_oid, workspace_id)
);
CREATE UNIQUE INDEX service_binding_one_enabled_login
    ON coordinator_v2.service_binding (login_name, login_oid) WHERE enabled;

CREATE TABLE coordinator_v2.repository_policy (
    workspace_id uuid NOT NULL,
    repository_full_name text NOT NULL,
    candidate_branch text NOT NULL,
    allowed_path_prefixes text[] NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    PRIMARY KEY (workspace_id, repository_full_name, candidate_branch)
);

CREATE TABLE coordinator_v2.work (
    workspace_id uuid NOT NULL,
    work_id text NOT NULL CHECK (work_id ~ '^[A-Za-z0-9._:-]{8,80}$'),
    admission_sha text NOT NULL CHECK (admission_sha ~ '^[0-9a-f]{64}$'),
    work_type text NOT NULL CHECK (work_type IN ('implementation', 'review')),
    state text NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'leased', 'candidate_ready', 'terminal')),
    fence bigint NOT NULL DEFAULT 0 CHECK (fence >= 0 AND fence <= 9007199254740991),
    worker_id uuid,
    instance_id uuid,
    lease_token text CHECK (lease_token IS NULL OR lease_token ~ '^[A-Za-z0-9_-]{43}$'),
    lease_expires_at timestamptz,
    cancellation_version bigint NOT NULL DEFAULT 0
        CHECK (cancellation_version >= 0 AND cancellation_version <= 9007199254740991),
    checkpoint jsonb,
    checkpoint_sequence bigint NOT NULL DEFAULT 0,
    codex_thread_id text CHECK (
        codex_thread_id IS NULL OR
        (octet_length(codex_thread_id) BETWEEN 1 AND 256 AND codex_thread_id ~ '^[ -~]+$')
    ),
    accepted_candidate_id uuid,
    terminal_outcome text CHECK (
        terminal_outcome IS NULL OR terminal_outcome IN ('succeeded', 'failed', 'scope_return', 'cancelled')
    ),
    terminal_summary_code text,
    terminal_at timestamptz,
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (workspace_id, work_id),
    UNIQUE (workspace_id, work_id, admission_sha),
    CHECK (
        (state = 'queued' AND worker_id IS NULL AND instance_id IS NULL AND lease_token IS NULL
            AND lease_expires_at IS NULL AND terminal_outcome IS NULL)
        OR (state IN ('leased', 'candidate_ready') AND worker_id IS NOT NULL AND instance_id IS NOT NULL
            AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL AND terminal_outcome IS NULL)
        OR (state = 'terminal' AND worker_id IS NULL AND instance_id IS NULL AND lease_token IS NULL
            AND lease_expires_at IS NULL AND terminal_outcome IS NOT NULL AND terminal_at IS NOT NULL)
    ),
    CHECK (work_type = 'implementation' OR accepted_candidate_id IS NULL)
);

CREATE TABLE coordinator_v2.work_admission (
    workspace_id uuid NOT NULL,
    work_id text NOT NULL,
    admission_id text NOT NULL CHECK (admission_id ~ '^[A-Za-z0-9._:-]{8,80}$'),
    admission_sha text NOT NULL CHECK (admission_sha ~ '^[0-9a-f]{64}$'),
    source_text text NOT NULL CHECK (octet_length(source_text) BETWEEN 0 AND 4096),
    acceptance jsonb NOT NULL CHECK (jsonb_typeof(acceptance) = 'array'),
    repository_full_name text NOT NULL CHECK (
        octet_length(repository_full_name) BETWEEN 3 AND 200 AND
        repository_full_name ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'
    ),
    base_sha text NOT NULL CHECK (base_sha ~ '^[0-9a-f]{40}$'),
    candidate_branch text NOT NULL CHECK (
        octet_length(candidate_branch) BETWEEN 1 AND 100 AND
        candidate_branch !~ '(^/|/$|//|\.\.|@\{|\\|[[:cntrl:] ~^:?*\[])'
    ),
    allowed_path_prefixes text[] NOT NULL CHECK (cardinality(allowed_path_prefixes) BETWEEN 1 AND 8),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (workspace_id, work_id),
    UNIQUE (workspace_id, admission_id),
    UNIQUE (workspace_id, work_id, admission_sha),
    CONSTRAINT work_admission_work_fk FOREIGN KEY (workspace_id, work_id, admission_sha)
        REFERENCES coordinator_v2.work (workspace_id, work_id, admission_sha)
        DEFERRABLE INITIALLY DEFERRED
);
ALTER TABLE coordinator_v2.work ADD CONSTRAINT work_admission_fk
    FOREIGN KEY (workspace_id, work_id, admission_sha)
    REFERENCES coordinator_v2.work_admission (workspace_id, work_id, admission_sha)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE coordinator_v2.worker_receipt (
    workspace_id uuid NOT NULL,
    work_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('checkpoint', 'submit_candidate', 'complete')),
    operation_id text NOT NULL CHECK (operation_id ~ '^[A-Za-z0-9._:-]{8,80}$'),
    request_sha text NOT NULL CHECK (request_sha ~ '^[0-9a-f]{64}$'),
    original_authority jsonb NOT NULL,
    response_code integer,
    response jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    PRIMARY KEY (workspace_id, work_id, kind, operation_id),
    FOREIGN KEY (workspace_id, work_id) REFERENCES coordinator_v2.work (workspace_id, work_id)
);

CREATE TABLE coordinator_v2.candidate (
    workspace_id uuid NOT NULL,
    candidate_id uuid NOT NULL,
    work_id text NOT NULL,
    operation_id text NOT NULL,
    manifest_sha text NOT NULL CHECK (manifest_sha ~ '^[0-9a-f]{64}$'),
    canonical_manifest bytea NOT NULL CHECK (octet_length(canonical_manifest) <= 393216),
    manifest jsonb NOT NULL,
    base_sha text NOT NULL CHECK (base_sha ~ '^[0-9a-f]{40}$'),
    expected_head text NOT NULL CHECK (expected_head ~ '^[0-9a-f]{40}$'),
    message text NOT NULL CHECK (octet_length(message) BETWEEN 1 AND 160),
    ordered_paths text[] NOT NULL,
    ordered_deletions text[] NOT NULL,
    file_count integer NOT NULL CHECK (file_count BETWEEN 0 AND 32),
    deletion_count integer NOT NULL CHECK (deletion_count BETWEEN 0 AND 32),
    decoded_bytes integer NOT NULL CHECK (decoded_bytes BETWEEN 0 AND 262144),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (workspace_id, candidate_id),
    UNIQUE (workspace_id, work_id, operation_id),
    UNIQUE (workspace_id, work_id, candidate_id),
    FOREIGN KEY (workspace_id, work_id) REFERENCES coordinator_v2.work (workspace_id, work_id),
    CHECK (base_sha = expected_head),
    CHECK (file_count + deletion_count > 0)
);
ALTER TABLE coordinator_v2.work ADD CONSTRAINT work_candidate_fk
    FOREIGN KEY (workspace_id, work_id, accepted_candidate_id)
    REFERENCES coordinator_v2.candidate (workspace_id, work_id, candidate_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE coordinator_v2.publication (
    workspace_id uuid NOT NULL,
    publication_id uuid NOT NULL,
    candidate_id uuid NOT NULL,
    work_id text NOT NULL,
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'authorized', 'reconciling', 'applied', 'stale_head', 'failed')),
    publication_fence bigint NOT NULL DEFAULT 0 CHECK (publication_fence >= 0),
    plan_sha text,
    manifest_sha text NOT NULL CHECK (manifest_sha ~ '^[0-9a-f]{64}$'),
    repository_full_name text,
    candidate_branch text,
    base_sha text NOT NULL CHECK (base_sha ~ '^[0-9a-f]{40}$'),
    expected_head text NOT NULL CHECK (expected_head ~ '^[0-9a-f]{40}$'),
    message text NOT NULL,
    canonical_manifest bytea NOT NULL,
    author_name text,
    author_email text,
    authored_at timestamptz,
    marker_ref text,
    desired_tree_sha text,
    desired_commit_sha text,
    seal_reason text CHECK (seal_reason IS NULL OR seal_reason IN ('stale_head', 'permanent_failure')),
    seal_observed_ref text,
    result jsonb,
    authorized_at timestamptz,
    terminal_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (workspace_id, publication_id),
    UNIQUE (workspace_id, candidate_id),
    UNIQUE (workspace_id, marker_ref),
    FOREIGN KEY (workspace_id, work_id, candidate_id)
        REFERENCES coordinator_v2.candidate (workspace_id, work_id, candidate_id),
    CHECK (base_sha = expected_head),
    CHECK (
        state = 'pending' OR (state = 'failed' AND plan_sha IS NULL) OR
        (plan_sha ~ '^[0-9a-f]{64}$' AND repository_full_name IS NOT NULL
            AND candidate_branch IS NOT NULL AND marker_ref IS NOT NULL
            AND author_name IS NOT NULL AND author_email IS NOT NULL AND authored_at IS NOT NULL)
    )
);

CREATE TABLE coordinator_v2.publication_attempt (
    workspace_id uuid NOT NULL,
    publication_id uuid NOT NULL,
    attempt bigint NOT NULL CHECK (attempt > 0),
    publisher_instance uuid NOT NULL,
    publisher_token text NOT NULL CHECK (publisher_token ~ '^[A-Za-z0-9_-]{43}$'),
    plan_sha text NOT NULL CHECK (plan_sha ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (workspace_id, publication_id, attempt),
    FOREIGN KEY (workspace_id, publication_id)
        REFERENCES coordinator_v2.publication (workspace_id, publication_id)
);

CREATE TABLE coordinator_v2.publication_observation (
    workspace_id uuid NOT NULL,
    publication_id uuid NOT NULL,
    attempt bigint NOT NULL,
    sequence bigint NOT NULL CHECK (sequence > 0),
    phase text NOT NULL,
    observed_ref text,
    observed_marker text,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (workspace_id, publication_id, attempt, sequence),
    FOREIGN KEY (workspace_id, publication_id, attempt)
        REFERENCES coordinator_v2.publication_attempt (workspace_id, publication_id, attempt)
);

CREATE INDEX work_claim_order ON coordinator_v2.work (workspace_id, state, lease_expires_at, created_at, work_id);
CREATE INDEX publication_claim_order ON coordinator_v2.publication (workspace_id, state, created_at, publication_id);

CREATE FUNCTION coordinator_v2.reject_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'immutable_record';
END;
$$;
CREATE TRIGGER work_admission_immutable BEFORE UPDATE OR DELETE ON coordinator_v2.work_admission
    FOR EACH ROW EXECUTE FUNCTION coordinator_v2.reject_immutable();
CREATE TRIGGER worker_receipt_immutable BEFORE UPDATE OR DELETE ON coordinator_v2.worker_receipt
    FOR EACH ROW WHEN (OLD.completed_at IS NOT NULL) EXECUTE FUNCTION coordinator_v2.reject_immutable();
CREATE TRIGGER candidate_immutable BEFORE UPDATE OR DELETE ON coordinator_v2.candidate
    FOR EACH ROW EXECUTE FUNCTION coordinator_v2.reject_immutable();
CREATE TRIGGER publication_attempt_immutable BEFORE UPDATE OR DELETE ON coordinator_v2.publication_attempt
    FOR EACH ROW EXECUTE FUNCTION coordinator_v2.reject_immutable();
CREATE TRIGGER publication_observation_immutable BEFORE UPDATE OR DELETE ON coordinator_v2.publication_observation
    FOR EACH ROW EXECUTE FUNCTION coordinator_v2.reject_immutable();

REVOKE ALL ON ALL TABLES IN SCHEMA coordinator_v2 FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA coordinator_v2 FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA coordinator_v2 FROM PUBLIC;

COMMIT;
