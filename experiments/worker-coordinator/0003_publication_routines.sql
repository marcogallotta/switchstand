BEGIN;

CREATE FUNCTION coordinator_v2.authorize_publication_v2(work_id_value text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, coordinator_v2 AS $$
DECLARE
    workspace uuid := coordinator_v2.current_workspace();
    selected coordinator_v2.work%ROWTYPE;
    admission coordinator_v2.work_admission%ROWTYPE;
    candidate_value coordinator_v2.candidate%ROWTYPE;
    publication_value coordinator_v2.publication%ROWTYPE;
    authored timestamptz := date_trunc('second', clock_timestamp());
    marker text;
    plan jsonb;
    digest_value text;
BEGIN
    SELECT * INTO selected FROM coordinator_v2.work
    WHERE workspace_id = workspace AND work_id = work_id_value FOR UPDATE;
    IF NOT FOUND THEN PERFORM coordinator_v2.fail('not_found'); END IF;
    IF selected.accepted_candidate_id IS NULL OR selected.state NOT IN ('candidate_ready', 'terminal')
       OR selected.terminal_outcome IN ('cancelled', 'failed', 'scope_return') THEN
        PERFORM coordinator_v2.fail('invalid_request');
    END IF;
    SELECT * INTO admission FROM coordinator_v2.work_admission
    WHERE workspace_id = workspace AND work_id = selected.work_id AND admission_sha = selected.admission_sha;
    SELECT * INTO candidate_value FROM coordinator_v2.candidate
    WHERE workspace_id = workspace AND work_id = selected.work_id
      AND candidate_id = selected.accepted_candidate_id FOR UPDATE;
    SELECT * INTO publication_value FROM coordinator_v2.publication
    WHERE workspace_id = workspace AND candidate_id = candidate_value.candidate_id FOR UPDATE;
    IF publication_value.state <> 'pending' THEN
        IF publication_value.state IN ('authorized', 'reconciling', 'applied') THEN
            RETURN jsonb_build_object('publication_id', publication_value.publication_id,
                'state', publication_value.state, 'plan_sha', publication_value.plan_sha);
        END IF;
        PERFORM coordinator_v2.fail('terminal_immutable');
    END IF;
    IF candidate_value.base_sha <> admission.base_sha OR candidate_value.expected_head <> admission.base_sha
       OR candidate_value.manifest_sha <> publication_value.manifest_sha
       OR candidate_value.canonical_manifest <> publication_value.canonical_manifest THEN
        PERFORM coordinator_v2.fail('policy_denied');
    END IF;
    marker := 'refs/tags/switchstand-publications/' || publication_value.publication_id::text;
    plan := jsonb_build_object(
        'publication_id', publication_value.publication_id,
        'manifest_sha', candidate_value.manifest_sha,
        'repository_full_name', admission.repository_full_name,
        'candidate_branch', admission.candidate_branch,
        'base_sha', admission.base_sha,
        'expected_head', admission.base_sha,
        'message', candidate_value.message,
        'marker_ref', marker,
        'author', jsonb_build_object('name', 'Switchstand Coordinator',
            'email', 'switchstand-coordinator@users.noreply.github.com',
            'date', to_char(authored AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'))
    );
    digest_value := coordinator_v2.sha256_text(coordinator_v2.canonical_json(plan));
    UPDATE coordinator_v2.publication SET
        state = 'authorized', plan_sha = digest_value,
        repository_full_name = admission.repository_full_name,
        candidate_branch = admission.candidate_branch,
        author_name = 'Switchstand Coordinator',
        author_email = 'switchstand-coordinator@users.noreply.github.com',
        authored_at = authored, marker_ref = marker,
        authorized_at = clock_timestamp(), updated_at = clock_timestamp()
    WHERE workspace_id = workspace AND publication_id = publication_value.publication_id
    RETURNING * INTO publication_value;
    RETURN jsonb_build_object('publication_id', publication_value.publication_id,
        'state', publication_value.state, 'plan_sha', publication_value.plan_sha,
        'marker_ref', publication_value.marker_ref);
END;
$$;

CREATE FUNCTION coordinator_v2.publication_plan(publication_value coordinator_v2.publication) RETURNS jsonb
LANGUAGE sql STABLE STRICT SET search_path = pg_catalog AS $$
    SELECT jsonb_build_object(
        'publication_id', publication_value.publication_id,
        'state', publication_value.state,
        'publication_fence', publication_value.publication_fence,
        'plan_sha', publication_value.plan_sha,
        'manifest_sha', publication_value.manifest_sha,
        'repository_full_name', publication_value.repository_full_name,
        'candidate_branch', publication_value.candidate_branch,
        'base_sha', publication_value.base_sha,
        'expected_head', publication_value.expected_head,
        'message', publication_value.message,
        'canonical_manifest_base64', encode(publication_value.canonical_manifest, 'base64'),
        'author', jsonb_build_object('name', publication_value.author_name,
            'email', publication_value.author_email,
            'date', to_char(publication_value.authored_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
        'marker_ref', publication_value.marker_ref,
        'desired_tree_sha', publication_value.desired_tree_sha,
        'desired_commit_sha', publication_value.desired_commit_sha,
        'seal_reason', publication_value.seal_reason,
        'seal_observed_ref', publication_value.seal_observed_ref
    )
$$;

CREATE FUNCTION coordinator_v2.claim_publication_v2(publisher_instance_value uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, coordinator_v2 AS $$
DECLARE
    workspace uuid := coordinator_v2.current_workspace();
    selected coordinator_v2.publication%ROWTYPE;
    token text := coordinator_v2.new_lease_token();
BEGIN
    IF publisher_instance_value IS NULL THEN PERFORM coordinator_v2.fail('invalid_request'); END IF;
    SELECT * INTO selected FROM coordinator_v2.publication
    WHERE workspace_id = workspace AND state IN ('authorized', 'reconciling')
    ORDER BY created_at, publication_id FOR UPDATE SKIP LOCKED LIMIT 1;
    IF NOT FOUND THEN RETURN NULL; END IF;
    UPDATE coordinator_v2.publication SET publication_fence = publication_fence + 1,
        updated_at = clock_timestamp()
    WHERE workspace_id = workspace AND publication_id = selected.publication_id
    RETURNING * INTO selected;
    INSERT INTO coordinator_v2.publication_attempt (
        workspace_id, publication_id, attempt, publisher_instance, publisher_token, plan_sha
    ) VALUES (workspace, selected.publication_id, selected.publication_fence,
        publisher_instance_value, token, selected.plan_sha);
    INSERT INTO coordinator_v2.publication_observation (
        workspace_id, publication_id, attempt, sequence, phase
    ) VALUES (workspace, selected.publication_id, selected.publication_fence, 1, 'claimed');
    RETURN coordinator_v2.publication_plan(selected) || jsonb_build_object(
        'publisher_instance', publisher_instance_value, 'publisher_token', token,
        'attempt', selected.publication_fence);
END;
$$;

CREATE FUNCTION coordinator_v2.load_attempt(
    publication_id_value uuid, attempt_value bigint,
    publisher_instance_value uuid, publisher_token_value text
) RETURNS coordinator_v2.publication_attempt
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, coordinator_v2 AS $$
DECLARE
    workspace uuid := coordinator_v2.current_workspace();
    selected coordinator_v2.publication_attempt%ROWTYPE;
BEGIN
    SELECT * INTO selected FROM coordinator_v2.publication_attempt
    WHERE workspace_id = workspace AND publication_id = publication_id_value
      AND attempt = attempt_value AND publisher_instance = publisher_instance_value
      AND publisher_token = publisher_token_value;
    IF NOT FOUND THEN PERFORM coordinator_v2.fail('stale_or_invalid_lease'); END IF;
    RETURN selected;
END;
$$;

CREATE FUNCTION coordinator_v2.record_objects_v2(
    publication_id_value uuid, attempt_value bigint,
    publisher_instance_value uuid, publisher_token_value text,
    tree_sha_value text, commit_sha_value text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, coordinator_v2 AS $$
DECLARE
    workspace uuid := coordinator_v2.current_workspace();
    attempt_value_row coordinator_v2.publication_attempt%ROWTYPE;
    selected coordinator_v2.publication%ROWTYPE;
    next_sequence bigint;
BEGIN
    attempt_value_row := coordinator_v2.load_attempt(publication_id_value, attempt_value,
        publisher_instance_value, publisher_token_value);
    IF tree_sha_value IS NULL OR tree_sha_value !~ '^[0-9a-f]{40}$'
       OR commit_sha_value IS NULL OR commit_sha_value !~ '^[0-9a-f]{40}$' THEN
        PERFORM coordinator_v2.fail('invalid_request');
    END IF;
    SELECT * INTO selected FROM coordinator_v2.publication
    WHERE workspace_id = workspace AND publication_id = publication_id_value FOR UPDATE;
    IF selected.state NOT IN ('authorized', 'reconciling') OR selected.plan_sha <> attempt_value_row.plan_sha THEN
        PERFORM coordinator_v2.fail('terminal_immutable');
    END IF;
    IF (selected.desired_tree_sha IS NOT NULL AND selected.desired_tree_sha <> tree_sha_value)
       OR (selected.desired_commit_sha IS NOT NULL AND selected.desired_commit_sha <> commit_sha_value) THEN
        UPDATE coordinator_v2.publication SET state = 'failed',
            result = jsonb_build_object('code', 'determinism_violation'),
            terminal_at = clock_timestamp(), updated_at = clock_timestamp()
        WHERE workspace_id = workspace AND publication_id = publication_id_value;
        SELECT * INTO selected FROM coordinator_v2.publication
        WHERE workspace_id = workspace AND publication_id = publication_id_value;
        RETURN coordinator_v2.publication_plan(selected) || jsonb_build_object('error', 'determinism_violation');
    END IF;
    UPDATE coordinator_v2.publication SET state = 'reconciling', desired_tree_sha = tree_sha_value,
        desired_commit_sha = commit_sha_value, updated_at = clock_timestamp()
    WHERE workspace_id = workspace AND publication_id = publication_id_value
    RETURNING * INTO selected;
    SELECT COALESCE(max(sequence), 0) + 1 INTO next_sequence
    FROM coordinator_v2.publication_observation
    WHERE workspace_id = workspace AND publication_id = publication_id_value AND attempt = attempt_value;
    INSERT INTO coordinator_v2.publication_observation (
        workspace_id, publication_id, attempt, sequence, phase
    ) VALUES (workspace, publication_id_value, attempt_value, next_sequence, 'objects_recorded');
    RETURN coordinator_v2.publication_plan(selected);
END;
$$;

CREATE FUNCTION coordinator_v2.fail_publication_v2(
    publication_id_value uuid, attempt_value bigint,
    publisher_instance_value uuid, publisher_token_value text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, coordinator_v2 AS $$
DECLARE
    workspace uuid := coordinator_v2.current_workspace();
    attempt_value_row coordinator_v2.publication_attempt%ROWTYPE;
    selected coordinator_v2.publication%ROWTYPE;
    next_sequence bigint;
BEGIN
    attempt_value_row := coordinator_v2.load_attempt(publication_id_value, attempt_value,
        publisher_instance_value, publisher_token_value);
    SELECT * INTO selected FROM coordinator_v2.publication
    WHERE workspace_id = workspace AND publication_id = publication_id_value FOR UPDATE;
    IF selected.state <> 'authorized' OR selected.plan_sha <> attempt_value_row.plan_sha
       OR selected.desired_tree_sha IS NOT NULL OR selected.desired_commit_sha IS NOT NULL THEN
        PERFORM coordinator_v2.fail('terminal_immutable');
    END IF;
    SELECT COALESCE(max(sequence), 0) + 1 INTO next_sequence
    FROM coordinator_v2.publication_observation
    WHERE workspace_id = workspace AND publication_id = publication_id_value AND attempt = attempt_value;
    INSERT INTO coordinator_v2.publication_observation (
        workspace_id, publication_id, attempt, sequence, phase, error_code
    ) VALUES (workspace, publication_id_value, attempt_value, next_sequence,
        'provider_error', 'provider_rejected_before_ref');
    UPDATE coordinator_v2.publication SET state = 'failed',
        result = jsonb_build_object('code', 'provider_rejected_before_ref'),
        terminal_at = clock_timestamp(), updated_at = clock_timestamp()
    WHERE workspace_id = workspace AND publication_id = publication_id_value
    RETURNING * INTO selected;
    RETURN coordinator_v2.publication_plan(selected);
END;
$$;

CREATE FUNCTION coordinator_v2.observe_publication_v2(
    publication_id_value uuid, attempt_value bigint,
    publisher_instance_value uuid, publisher_token_value text,
    marker_sha_value text, target_sha_value text, permanent_error_value boolean DEFAULT false
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, coordinator_v2 AS $$
DECLARE
    workspace uuid := coordinator_v2.current_workspace();
    attempt_value_row coordinator_v2.publication_attempt%ROWTYPE;
    selected coordinator_v2.publication%ROWTYPE;
    next_sequence bigint;
    directive text := 'query';
BEGIN
    attempt_value_row := coordinator_v2.load_attempt(publication_id_value, attempt_value,
        publisher_instance_value, publisher_token_value);
    SELECT * INTO selected FROM coordinator_v2.publication
    WHERE workspace_id = workspace AND publication_id = publication_id_value FOR UPDATE;
    IF selected.plan_sha <> attempt_value_row.plan_sha THEN
        PERFORM coordinator_v2.fail('stale_or_invalid_lease');
    END IF;
    IF permanent_error_value IS NULL OR selected.desired_tree_sha IS NULL
       OR selected.desired_commit_sha IS NULL THEN
        PERFORM coordinator_v2.fail('invalid_request');
    END IF;
    IF marker_sha_value IS NOT NULL AND marker_sha_value !~ '^[0-9a-f]{40}$' THEN
        PERFORM coordinator_v2.fail('invalid_request');
    END IF;
    IF target_sha_value IS NOT NULL AND target_sha_value !~ '^[0-9a-f]{40}$' THEN
        PERFORM coordinator_v2.fail('invalid_request');
    END IF;
    SELECT COALESCE(max(sequence), 0) + 1 INTO next_sequence
    FROM coordinator_v2.publication_observation
    WHERE workspace_id = workspace AND publication_id = publication_id_value AND attempt = attempt_value;
    INSERT INTO coordinator_v2.publication_observation (
        workspace_id, publication_id, attempt, sequence, phase, observed_ref, observed_marker,
        error_code
    ) VALUES (workspace, publication_id_value, attempt_value, next_sequence, 'provider_readback',
        target_sha_value, marker_sha_value, CASE WHEN permanent_error_value THEN 'permanent_failure' END);

    IF selected.state IN ('applied', 'stale_head', 'failed') THEN
        directive := 'terminal';
    ELSIF marker_sha_value = selected.desired_commit_sha THEN
        UPDATE coordinator_v2.publication SET state = 'applied',
            result = jsonb_build_object('code', 'applied', 'commit_sha', desired_commit_sha),
            terminal_at = clock_timestamp(), updated_at = clock_timestamp()
        WHERE workspace_id = workspace AND publication_id = publication_id_value;
        directive := 'terminal';
    ELSIF marker_sha_value = selected.expected_head AND selected.seal_reason IS NOT NULL THEN
        UPDATE coordinator_v2.publication SET
            state = CASE WHEN seal_reason = 'stale_head' THEN 'stale_head' ELSE 'failed' END,
            result = jsonb_build_object('code', seal_reason, 'observed_ref', seal_observed_ref),
            terminal_at = clock_timestamp(), updated_at = clock_timestamp()
        WHERE workspace_id = workspace AND publication_id = publication_id_value;
        directive := 'terminal';
    ELSIF marker_sha_value IS NOT NULL THEN
        UPDATE coordinator_v2.publication SET state = 'failed',
            result = jsonb_build_object('code', 'marker_conflict'), terminal_at = clock_timestamp(),
            updated_at = clock_timestamp()
        WHERE workspace_id = workspace AND publication_id = publication_id_value;
        directive := 'terminal';
    ELSIF selected.seal_reason IS NOT NULL THEN
        directive := 'close_expected';
    ELSIF target_sha_value = selected.desired_commit_sha THEN
        directive := 'close_desired';
    ELSIF target_sha_value = selected.expected_head AND permanent_error_value THEN
        UPDATE coordinator_v2.publication SET seal_reason = 'permanent_failure',
            seal_observed_ref = target_sha_value, updated_at = clock_timestamp()
        WHERE workspace_id = workspace AND publication_id = publication_id_value;
        directive := 'close_expected';
    ELSIF target_sha_value = selected.expected_head THEN
        directive := 'publish';
    ELSE
        UPDATE coordinator_v2.publication SET seal_reason = 'stale_head',
            seal_observed_ref = target_sha_value, updated_at = clock_timestamp()
        WHERE workspace_id = workspace AND publication_id = publication_id_value;
        directive := 'close_expected';
    END IF;
    SELECT * INTO selected FROM coordinator_v2.publication
    WHERE workspace_id = workspace AND publication_id = publication_id_value;
    RETURN coordinator_v2.publication_plan(selected) || jsonb_build_object(
        'directive', directive,
        'close_target_sha', CASE WHEN directive IN ('close_desired', 'close_expected')
            THEN target_sha_value ELSE NULL END);
END;
$$;

CREATE FUNCTION coordinator_v2.export_work_v2(work_id_value text, include_source boolean DEFAULT false) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, coordinator_v2 AS $$
DECLARE
    workspace uuid := coordinator_v2.current_workspace();
    selected coordinator_v2.work%ROWTYPE;
    admission coordinator_v2.work_admission%ROWTYPE;
    candidate_value coordinator_v2.candidate%ROWTYPE;
    publication_value coordinator_v2.publication%ROWTYPE;
BEGIN
    SELECT * INTO selected FROM coordinator_v2.work
    WHERE workspace_id = workspace AND work_id = work_id_value;
    IF NOT FOUND THEN PERFORM coordinator_v2.fail('not_found'); END IF;
    SELECT * INTO admission FROM coordinator_v2.work_admission
    WHERE workspace_id = workspace AND work_id = selected.work_id AND admission_sha = selected.admission_sha;
    IF selected.accepted_candidate_id IS NOT NULL THEN
        SELECT * INTO candidate_value FROM coordinator_v2.candidate
        WHERE workspace_id = workspace AND candidate_id = selected.accepted_candidate_id;
        SELECT * INTO publication_value FROM coordinator_v2.publication
        WHERE workspace_id = workspace AND candidate_id = selected.accepted_candidate_id;
    END IF;
    RETURN jsonb_build_object(
        'work_id', selected.work_id, 'work_type', selected.work_type, 'state', selected.state,
        'admission_sha', selected.admission_sha,
        'source_text', CASE WHEN include_source THEN admission.source_text ELSE NULL END,
        'checkpoint', selected.checkpoint, 'codex_thread_id', selected.codex_thread_id,
        'accepted_candidate', CASE WHEN candidate_value.candidate_id IS NULL THEN NULL ELSE
            jsonb_build_object('candidate_id', candidate_value.candidate_id,
                'manifest_sha', candidate_value.manifest_sha,
                'canonical_manifest_base64', encode(candidate_value.canonical_manifest, 'base64')) END,
        'publication', CASE WHEN publication_value.publication_id IS NULL THEN NULL ELSE
            coordinator_v2.publication_plan(publication_value) END,
        'terminal_outcome', selected.terminal_outcome);
END;
$$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA coordinator_v2 FROM PUBLIC;

COMMIT;
