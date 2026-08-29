BEGIN;

CREATE FUNCTION coordinator_v2.canonical_json(value jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT SET search_path = pg_catalog AS $$
DECLARE
    kind text := jsonb_typeof(value);
    result text;
BEGIN
    IF kind = 'object' THEN
        SELECT '{' || COALESCE(string_agg(to_json(key)::text || ':' ||
            coordinator_v2.canonical_json(item), ',' ORDER BY convert_to(key, 'UTF8')), '') || '}'
        INTO result FROM jsonb_each(value) AS entry(key, item);
        RETURN result;
    ELSIF kind = 'array' THEN
        SELECT '[' || COALESCE(string_agg(coordinator_v2.canonical_json(item), ',' ORDER BY ordinality), '') || ']'
        INTO result FROM jsonb_array_elements(value) WITH ORDINALITY AS entry(item, ordinality);
        RETURN result;
    END IF;
    RETURN value::text;
END;
$$;

CREATE FUNCTION coordinator_v2.sha256_text(value text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog AS $$
    SELECT encode(sha256(convert_to(value, 'UTF8')), 'hex')
$$;

CREATE FUNCTION coordinator_v2.fail(code text) RETURNS void
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = code;
END;
$$;

CREATE FUNCTION coordinator_v2.current_workspace() RETURNS uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, coordinator_v2 AS $$
DECLARE
    workspace uuid;
BEGIN
    SELECT binding.workspace_id INTO workspace
    FROM coordinator_v2.service_binding AS binding
    WHERE binding.login_name = session_user
      AND binding.login_oid = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = session_user)
      AND binding.enabled;
    IF workspace IS NULL THEN
        PERFORM coordinator_v2.fail('unauthorized');
    END IF;
    RETURN workspace;
END;
$$;

CREATE FUNCTION coordinator_v2.valid_prefixes(prefixes text[]) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog AS $$
    SELECT cardinality(prefixes) BETWEEN 1 AND 8
       AND NOT EXISTS (
           SELECT 1 FROM unnest(prefixes) AS p(value)
           WHERE octet_length(value) NOT BETWEEN 1 AND 240
              OR value <> normalize(value, NFC)
              OR value !~ '^[ -~]+$'
              OR value ~ '(^/|/$|//|(^|/)\.\.?(/|$)|\\|[[:cntrl:]])'
       )
       AND cardinality(prefixes) = (SELECT count(DISTINCT value) FROM unnest(prefixes) AS p(value))
       AND prefixes = ARRAY(SELECT value FROM unnest(prefixes) AS p(value) ORDER BY convert_to(value, 'UTF8'))
$$;

CREATE FUNCTION coordinator_v2.path_allowed(path text, prefixes text[]) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog AS $$
    SELECT octet_length(path) BETWEEN 1 AND 240
       AND path = normalize(path, NFC)
       AND path ~ '^[ -~]+$'
       AND path !~ '(^/|/$|//|(^|/)\.\.?(/|$)|\\|[[:cntrl:]])'
       AND EXISTS (
           SELECT 1 FROM unnest(prefixes) AS p(prefix)
           WHERE path = prefix OR starts_with(path, prefix || '/')
       )
$$;

CREATE FUNCTION coordinator_v2.valid_branch(value text) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog AS $$
    SELECT octet_length(value) BETWEEN 1 AND 100
       AND value !~ '^[-./]'
       AND NOT starts_with(value, 'refs/')
       AND value !~ '[./]$'
       AND strpos(value, '..') = 0
       AND strpos(value, '@{') = 0
       AND strpos(value, '//') = 0
       AND value !~ '[[:cntrl:] ~^:?*\[\\]'
       AND NOT EXISTS (
           SELECT 1 FROM unnest(string_to_array(value, '/')) AS component(part)
           WHERE starts_with(part, '.') OR right(part, 5) = '.lock'
       )
$$;

CREATE FUNCTION coordinator_v2.valid_acceptance(value jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog AS $$
    SELECT jsonb_typeof(value) = 'array'
       AND jsonb_array_length(value) BETWEEN 0 AND 16
       AND NOT EXISTS (
           SELECT 1 FROM jsonb_array_elements(value) AS item
           WHERE jsonb_typeof(item) IS DISTINCT FROM 'string'
              OR octet_length(item #>> '{}') NOT BETWEEN 1 AND 512 IS NOT FALSE
       )
$$;

CREATE FUNCTION coordinator_v2.exact_keys(value jsonb, keys text[]) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog AS $$
    SELECT jsonb_typeof(value) = 'object'
       AND ARRAY(SELECT key FROM jsonb_object_keys(value) AS key ORDER BY key)
           = ARRAY(SELECT key FROM unnest(keys) AS key ORDER BY key)
$$;

CREATE FUNCTION coordinator_v2.valid_authority(value jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog AS $$
    SELECT jsonb_typeof(value -> 'protocol') = 'string'
       AND value ->> 'protocol' = 'worker-v2'
       AND jsonb_typeof(value -> 'work_id') = 'string'
       AND value ->> 'work_id' ~ '^[A-Za-z0-9._:-]{8,80}$'
       AND jsonb_typeof(value -> 'worker_id') = 'string'
       AND value ->> 'worker_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
       AND jsonb_typeof(value -> 'instance_id') = 'string'
       AND value ->> 'instance_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
       AND jsonb_typeof(value -> 'fence') = 'number'
       AND value ->> 'fence' ~ '^[1-9][0-9]*$'
       AND (value ->> 'fence')::numeric <= 9007199254740991
       AND jsonb_typeof(value -> 'lease_token') = 'string'
       AND value ->> 'lease_token' ~ '^[A-Za-z0-9_-]{43}$'
       AND jsonb_typeof(value -> 'cancellation_version') = 'number'
       AND value ->> 'cancellation_version' ~ '^(0|[1-9][0-9]*)$'
       AND (value ->> 'cancellation_version')::numeric <= 9007199254740991
$$;

CREATE FUNCTION coordinator_v2.admit_work_v2(input jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, coordinator_v2 AS $$
DECLARE
    workspace uuid := coordinator_v2.current_workspace();
    admission_id_value text;
    work_type_value text;
    source_value text;
    acceptance_value jsonb;
    repository_value jsonb;
    repository_name text;
    base_value text;
    branch_value text;
    prefixes_value text[];
    canonical_payload jsonb;
    digest_value text;
    existing coordinator_v2.work_admission%ROWTYPE;
    generated_work_id text;
BEGIN
    IF NOT coordinator_v2.exact_keys(input,
        ARRAY['acceptance', 'admission_id', 'repository', 'source_text', 'work_type']) THEN
        PERFORM coordinator_v2.fail('invalid_request');
    END IF;
    admission_id_value := input ->> 'admission_id';
    work_type_value := input ->> 'work_type';
    source_value := input ->> 'source_text';
    acceptance_value := input -> 'acceptance';
    repository_value := input -> 'repository';
    IF jsonb_typeof(input -> 'admission_id') IS DISTINCT FROM 'string'
       OR admission_id_value IS NULL OR admission_id_value !~ '^[A-Za-z0-9._:-]{8,80}$'
       OR jsonb_typeof(input -> 'work_type') IS DISTINCT FROM 'string'
       OR work_type_value NOT IN ('implementation', 'review')
       OR jsonb_typeof(input -> 'source_text') IS DISTINCT FROM 'string'
       OR source_value IS NULL OR octet_length(source_value) > 4096
       OR acceptance_value IS NULL OR NOT coordinator_v2.valid_acceptance(acceptance_value)
       OR repository_value IS NULL OR NOT coordinator_v2.exact_keys(repository_value,
            ARRAY['allowed_path_prefixes', 'base_sha', 'candidate_branch', 'full_name']) THEN
        PERFORM coordinator_v2.fail('invalid_request');
    END IF;
    repository_name := repository_value ->> 'full_name';
    base_value := repository_value ->> 'base_sha';
    branch_value := repository_value ->> 'candidate_branch';
    IF jsonb_typeof(repository_value -> 'allowed_path_prefixes') IS DISTINCT FROM 'array' THEN
        PERFORM coordinator_v2.fail('invalid_request');
    END IF;
    IF EXISTS (SELECT 1 FROM jsonb_array_elements(repository_value -> 'allowed_path_prefixes') AS item
        WHERE jsonb_typeof(item) IS DISTINCT FROM 'string') THEN
        PERFORM coordinator_v2.fail('invalid_request');
    END IF;
    SELECT array_agg(item #>> '{}' ORDER BY ordinality) INTO prefixes_value
    FROM jsonb_array_elements(repository_value -> 'allowed_path_prefixes') WITH ORDINALITY AS p(item, ordinality);
    IF jsonb_typeof(repository_value -> 'full_name') IS DISTINCT FROM 'string'
       OR repository_name IS NULL OR octet_length(repository_name) NOT BETWEEN 3 AND 200
       OR repository_name !~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'
       OR jsonb_typeof(repository_value -> 'base_sha') IS DISTINCT FROM 'string'
       OR base_value IS NULL OR base_value !~ '^[0-9a-f]{40}$'
       OR jsonb_typeof(repository_value -> 'candidate_branch') IS DISTINCT FROM 'string'
       OR branch_value IS NULL OR NOT coordinator_v2.valid_branch(branch_value)
       OR prefixes_value IS NULL OR NOT coordinator_v2.valid_prefixes(prefixes_value) THEN
        PERFORM coordinator_v2.fail('invalid_request');
    END IF;
    IF octet_length(jsonb_build_object(
        'protocol', 'worker-v2', 'work_id', 'work:00000000-0000-4000-8000-000000000000',
        'work_type', work_type_value, 'worker_id', '00000000-0000-4000-8000-000000000000',
        'instance_id', '00000000-0000-4000-8000-000000000000', 'fence', 9007199254740991,
        'lease_token', repeat('A', 43), 'lease_expires_at', '2000-01-01T00:00:00.000000Z',
        'cancellation_version', 9007199254740991, 'admission_sha', repeat('a', 64),
        'source_text', source_value, 'acceptance', acceptance_value,
        'repository', jsonb_build_object('full_name', repository_name, 'base_sha', base_value,
            'candidate_branch', branch_value, 'allowed_path_prefixes', to_jsonb(prefixes_value)),
        'checkout_path', '/v2/work/work:00000000-0000-4000-8000-000000000000/checkout',
        'prior_checkpoint', NULL, 'codex_thread_id', NULL, 'accepted_candidate', NULL,
        'limits', jsonb_build_object('max_files', 32, 'max_file_bytes', 65536,
            'max_total_bytes', 262144, 'max_deletions', 32, 'max_json_bytes', 393216)
    )::text) > 16384 THEN
        PERFORM coordinator_v2.fail('request_too_large');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM coordinator_v2.repository_policy AS policy
        WHERE policy.workspace_id = workspace AND policy.enabled
          AND policy.repository_full_name = repository_name
          AND policy.candidate_branch = branch_value
          AND policy.allowed_path_prefixes = prefixes_value
    ) THEN
        PERFORM coordinator_v2.fail('policy_denied');
    END IF;

    canonical_payload := jsonb_build_object(
        'work_type', work_type_value,
        'source_text', source_value,
        'acceptance', acceptance_value,
        'repository', jsonb_build_object(
            'full_name', repository_name,
            'base_sha', base_value,
            'candidate_branch', branch_value,
            'allowed_path_prefixes', to_jsonb(prefixes_value)
        )
    );
    digest_value := coordinator_v2.sha256_text(coordinator_v2.canonical_json(canonical_payload));

    SELECT * INTO existing FROM coordinator_v2.work_admission
    WHERE workspace_id = workspace AND admission_id = admission_id_value;
    IF FOUND THEN
        IF existing.admission_sha <> digest_value THEN
            PERFORM coordinator_v2.fail('idempotency_conflict');
        END IF;
        RETURN jsonb_build_object('work_id', existing.work_id,
            'admission_sha', existing.admission_sha, 'created', true);
    END IF;

    generated_work_id := 'work:' || gen_random_uuid()::text;
    SET CONSTRAINTS work_admission_fk, work_admission_work_fk DEFERRED;
    INSERT INTO coordinator_v2.work (
        workspace_id, work_id, admission_sha, work_type, state
    ) VALUES (workspace, generated_work_id, digest_value, work_type_value, 'queued');
    INSERT INTO coordinator_v2.work_admission (
        workspace_id, work_id, admission_id, admission_sha, source_text, acceptance,
        repository_full_name, base_sha, candidate_branch, allowed_path_prefixes
    ) VALUES (
        workspace, generated_work_id, admission_id_value, digest_value, source_value, acceptance_value,
        repository_name, base_value, branch_value, prefixes_value
    );
    RETURN jsonb_build_object('work_id', generated_work_id,
        'admission_sha', digest_value, 'created', true);
EXCEPTION WHEN unique_violation THEN
    SELECT * INTO existing FROM coordinator_v2.work_admission
    WHERE workspace_id = workspace AND admission_id = admission_id_value;
    IF existing.admission_sha = digest_value THEN
        RETURN jsonb_build_object('work_id', existing.work_id,
            'admission_sha', existing.admission_sha, 'created', true);
    END IF;
    PERFORM coordinator_v2.fail('idempotency_conflict');
    RETURN NULL;
END;
$$;

CREATE FUNCTION coordinator_v2.new_lease_token() RETURNS text
LANGUAGE sql VOLATILE SET search_path = pg_catalog AS $$
    SELECT rtrim(translate(encode(sha256(convert_to(
        gen_random_uuid()::text || clock_timestamp()::text || random()::text, 'UTF8')), 'base64'), '+/', '-_'), '=')
$$;

CREATE FUNCTION coordinator_v2.claim_v2(worker uuid, instance uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, coordinator_v2 AS $$
DECLARE
    workspace uuid := coordinator_v2.current_workspace();
    selected coordinator_v2.work%ROWTYPE;
    admission coordinator_v2.work_admission%ROWTYPE;
    token text;
    expiry timestamptz := clock_timestamp() + interval '15 seconds';
    accepted jsonb;
BEGIN
    SELECT * INTO selected FROM coordinator_v2.work
    WHERE workspace_id = workspace
      AND (state = 'queued' OR (state IN ('leased', 'candidate_ready') AND lease_expires_at <= clock_timestamp()))
    ORDER BY created_at, work_id
    FOR UPDATE SKIP LOCKED LIMIT 1;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT * INTO admission FROM coordinator_v2.work_admission
    WHERE workspace_id = workspace AND work_id = selected.work_id AND admission_sha = selected.admission_sha;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid_admission_binding';
    END IF;
    token := coordinator_v2.new_lease_token();
    UPDATE coordinator_v2.work SET
        state = CASE WHEN state = 'queued' THEN 'leased' ELSE state END,
        fence = fence + 1, worker_id = worker, instance_id = instance,
        lease_token = token, lease_expires_at = expiry,
        revision = revision + 1, updated_at = clock_timestamp()
    WHERE workspace_id = workspace AND work_id = selected.work_id
    RETURNING * INTO selected;
    IF selected.accepted_candidate_id IS NOT NULL THEN
        SELECT jsonb_build_object('candidate_id', candidate_id, 'manifest_sha', manifest_sha)
        INTO accepted FROM coordinator_v2.candidate
        WHERE workspace_id = workspace AND work_id = selected.work_id
          AND candidate_id = selected.accepted_candidate_id;
    END IF;
    RETURN jsonb_build_object(
        'protocol', 'worker-v2', 'work_id', selected.work_id, 'work_type', selected.work_type,
        'worker_id', worker, 'instance_id', instance, 'fence', selected.fence,
        'lease_token', token, 'lease_expires_at', to_char(expiry AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        'cancellation_version', selected.cancellation_version,
        'admission_sha', admission.admission_sha, 'source_text', admission.source_text,
        'acceptance', admission.acceptance,
        'repository', jsonb_build_object('full_name', admission.repository_full_name,
            'base_sha', admission.base_sha, 'candidate_branch', admission.candidate_branch,
            'allowed_path_prefixes', to_jsonb(admission.allowed_path_prefixes)),
        'checkout_path', '/v2/work/' || selected.work_id || '/checkout',
        'prior_checkpoint', selected.checkpoint, 'codex_thread_id', selected.codex_thread_id,
        'accepted_candidate', accepted,
        'limits', jsonb_build_object('max_files', 32, 'max_file_bytes', 65536,
            'max_total_bytes', 262144, 'max_deletions', 32, 'max_json_bytes', 393216)
    );
END;
$$;

CREATE FUNCTION coordinator_v2.authority_matches(row_value coordinator_v2.work, authority jsonb) RETURNS boolean
LANGUAGE sql STABLE STRICT SET search_path = pg_catalog AS $$
    SELECT row_value.state IN ('leased', 'candidate_ready')
       AND row_value.worker_id::text = authority ->> 'worker_id'
       AND row_value.instance_id::text = authority ->> 'instance_id'
       AND row_value.fence = (authority ->> 'fence')::bigint
       AND row_value.lease_token = authority ->> 'lease_token'
       AND row_value.cancellation_version = (authority ->> 'cancellation_version')::bigint
       AND row_value.lease_expires_at > clock_timestamp()
$$;

CREATE FUNCTION coordinator_v2.renew_v2(authority jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, coordinator_v2 AS $$
DECLARE
    workspace uuid := coordinator_v2.current_workspace();
    selected coordinator_v2.work%ROWTYPE;
    expiry timestamptz := clock_timestamp() + interval '15 seconds';
BEGIN
    IF coordinator_v2.valid_authority(authority) IS DISTINCT FROM TRUE THEN
        PERFORM coordinator_v2.fail('stale_or_invalid_lease');
    END IF;
    SELECT * INTO selected FROM coordinator_v2.work
    WHERE workspace_id = workspace AND work_id = authority ->> 'work_id' FOR UPDATE;
    IF NOT FOUND OR coordinator_v2.authority_matches(selected, authority) IS DISTINCT FROM TRUE THEN
        PERFORM coordinator_v2.fail('stale_or_invalid_lease');
    END IF;
    UPDATE coordinator_v2.work SET lease_expires_at = expiry, updated_at = clock_timestamp()
    WHERE workspace_id = workspace AND work_id = selected.work_id;
    RETURN jsonb_build_object(
        'lease_expires_at', to_char(expiry AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        'renew_after_seconds', 1, 'cancellation_version', selected.cancellation_version);
END;
$$;

CREATE FUNCTION coordinator_v2.checkout_authority_v2(authority jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, coordinator_v2 AS $$
DECLARE
    workspace uuid := coordinator_v2.current_workspace();
    selected coordinator_v2.work%ROWTYPE;
    admission coordinator_v2.work_admission%ROWTYPE;
BEGIN
    SELECT * INTO selected FROM coordinator_v2.work
    WHERE workspace_id = workspace AND work_id = authority ->> 'work_id';
    IF NOT FOUND THEN PERFORM coordinator_v2.fail('not_found'); END IF;
    IF coordinator_v2.authority_matches(selected, authority) IS DISTINCT FROM TRUE THEN
        PERFORM coordinator_v2.fail('stale_or_invalid_lease');
    END IF;
    SELECT * INTO admission FROM coordinator_v2.work_admission
    WHERE workspace_id = workspace AND work_id = selected.work_id AND admission_sha = selected.admission_sha;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid_admission_binding';
    END IF;
    RETURN jsonb_build_object('repository_full_name', admission.repository_full_name,
        'base_sha', admission.base_sha);
END;
$$;

CREATE FUNCTION coordinator_v2.validate_candidate_v2(
    request_value jsonb, prefixes_value text[], base_value text
) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE STRICT
SET search_path = pg_catalog, coordinator_v2 AS $$
DECLARE
    item jsonb;
    bytes bytea;
    text_value text;
    path_value text;
    prior_path text;
    all_paths text[] := ARRAY[]::text[];
    file_paths text[] := ARRAY[]::text[];
    deletion_paths text[] := ARRAY[]::text[];
    decoded_total integer := 0;
    prior_check text;
BEGIN
    IF (NOT coordinator_v2.exact_keys(request_value, ARRAY[
        'base_sha', 'cancellation_version', 'check_summaries', 'deletions',
        'expected_branch_head', 'fence', 'files', 'instance_id', 'lease_token',
        'message', 'operation_id', 'protocol', 'request_digest', 'work_id', 'worker_id'
    ]) OR request_value ->> 'base_sha' <> base_value
       OR request_value ->> 'expected_branch_head' <> base_value
       OR jsonb_typeof(request_value -> 'base_sha') IS DISTINCT FROM 'string'
       OR jsonb_typeof(request_value -> 'expected_branch_head') IS DISTINCT FROM 'string'
       OR jsonb_typeof(request_value -> 'files') <> 'array'
       OR jsonb_typeof(request_value -> 'deletions') <> 'array'
       OR jsonb_typeof(request_value -> 'check_summaries') <> 'array'
       OR jsonb_array_length(request_value -> 'files') > 32
       OR jsonb_array_length(request_value -> 'deletions') > 32
       OR jsonb_array_length(request_value -> 'check_summaries') > 32
       OR jsonb_array_length(request_value -> 'files')
            + jsonb_array_length(request_value -> 'deletions') = 0
       OR jsonb_typeof(request_value -> 'message') IS DISTINCT FROM 'string'
       OR request_value ->> 'message' IS NULL
       OR request_value ->> 'message' <> btrim(request_value ->> 'message')
       OR request_value ->> 'message' ~ '[\r\n]'
       OR octet_length(request_value ->> 'message') NOT BETWEEN 1 AND 160
       OR jsonb_typeof(request_value -> 'request_digest') IS DISTINCT FROM 'string'
       OR request_value ->> 'request_digest' !~ '^[0-9a-f]{64}$'
       OR request_value ->> 'request_digest' <> coordinator_v2.sha256_text(
            coordinator_v2.canonical_json(request_value - 'request_digest'))) IS NOT FALSE THEN
        PERFORM coordinator_v2.fail('invalid_request');
    END IF;

    FOR item IN SELECT value FROM jsonb_array_elements(request_value -> 'files') AS entry(value)
    LOOP
        IF (NOT coordinator_v2.exact_keys(item,
            ARRAY['content_base64', 'decoded_bytes', 'mode', 'path', 'sha256', 'type'])
           OR item ->> 'mode' <> '100644' OR item ->> 'type' <> 'blob'
           OR jsonb_typeof(item -> 'mode') IS DISTINCT FROM 'string'
           OR jsonb_typeof(item -> 'type') IS DISTINCT FROM 'string'
           OR jsonb_typeof(item -> 'path') IS DISTINCT FROM 'string'
           OR jsonb_typeof(item -> 'content_base64') IS DISTINCT FROM 'string'
           OR jsonb_typeof(item -> 'decoded_bytes') <> 'number'
           OR jsonb_typeof(item -> 'sha256') IS DISTINCT FROM 'string'
           OR item ->> 'decoded_bytes' !~ '^(0|[1-9][0-9]*)$'
           OR item ->> 'sha256' !~ '^[0-9a-f]{64}$') IS NOT FALSE THEN
            PERFORM coordinator_v2.fail('invalid_request');
        END IF;
        path_value := item ->> 'path';
        IF (NOT coordinator_v2.path_allowed(path_value, prefixes_value)
           OR (prior_path IS NOT NULL AND convert_to(path_value, 'UTF8') <= convert_to(prior_path, 'UTF8')))
           IS NOT FALSE THEN
            PERFORM coordinator_v2.fail('policy_denied');
        END IF;
        BEGIN
            bytes := decode(item ->> 'content_base64', 'base64');
            text_value := convert_from(bytes, 'UTF8');
        EXCEPTION WHEN OTHERS THEN
            PERFORM coordinator_v2.fail('invalid_request');
        END;
        IF (replace(encode(bytes, 'base64'), E'\n', '') <> item ->> 'content_base64'
           OR octet_length(bytes) <> (item ->> 'decoded_bytes')::integer
           OR octet_length(bytes) > 65536
           OR encode(sha256(bytes), 'hex') <> item ->> 'sha256') IS NOT FALSE THEN
            PERFORM coordinator_v2.fail('invalid_request');
        END IF;
        decoded_total := decoded_total + octet_length(bytes);
        file_paths := array_append(file_paths, path_value);
        all_paths := array_append(all_paths, path_value);
        prior_path := path_value;
    END LOOP;
    IF decoded_total > 262144 THEN PERFORM coordinator_v2.fail('request_too_large'); END IF;

    prior_path := NULL;
    FOR item IN SELECT value FROM jsonb_array_elements(request_value -> 'deletions') AS entry(value)
    LOOP
        IF coordinator_v2.exact_keys(item, ARRAY['path']) IS DISTINCT FROM TRUE
           OR jsonb_typeof(item -> 'path') IS DISTINCT FROM 'string' THEN
            PERFORM coordinator_v2.fail('invalid_request');
        END IF;
        path_value := item ->> 'path';
        IF (NOT coordinator_v2.path_allowed(path_value, prefixes_value)
           OR (prior_path IS NOT NULL AND convert_to(path_value, 'UTF8') <= convert_to(prior_path, 'UTF8'))
           OR path_value = ANY(all_paths)) IS NOT FALSE THEN
            PERFORM coordinator_v2.fail('policy_denied');
        END IF;
        deletion_paths := array_append(deletion_paths, path_value);
        all_paths := array_append(all_paths, path_value);
        prior_path := path_value;
    END LOOP;
    IF cardinality(all_paths) <> (
        SELECT count(DISTINCT translate(value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))
        FROM unnest(all_paths) AS path(value)
    ) THEN
        PERFORM coordinator_v2.fail('policy_denied');
    END IF;

    FOR item IN SELECT value FROM jsonb_array_elements(request_value -> 'check_summaries') AS entry(value)
    LOOP
        IF (NOT coordinator_v2.exact_keys(item, ARRAY['name', 'outcome', 'summary'])
           OR jsonb_typeof(item -> 'name') IS DISTINCT FROM 'string'
           OR jsonb_typeof(item -> 'outcome') IS DISTINCT FROM 'string'
           OR jsonb_typeof(item -> 'summary') IS DISTINCT FROM 'string'
           OR octet_length(item ->> 'name') NOT BETWEEN 1 AND 80
           OR item ->> 'outcome' NOT IN ('PASS', 'FAIL')
           OR octet_length(item ->> 'summary') > 1024
           OR strpos(item ->> 'summary', '/') > 0
           OR strpos(translate(item ->> 'summary', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
                'abcdefghijklmnopqrstuvwxyz'), 'prompt') > 0
           OR strpos(translate(item ->> 'summary', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
                'abcdefghijklmnopqrstuvwxyz'), 'output') > 0
           OR strpos(translate(item ->> 'summary', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
                'abcdefghijklmnopqrstuvwxyz'), 'error') > 0
           OR (prior_check IS NOT NULL
               AND convert_to(item ->> 'name', 'UTF8') <= convert_to(prior_check, 'UTF8')))
           IS NOT FALSE THEN
            PERFORM coordinator_v2.fail('invalid_request');
        END IF;
        prior_check := item ->> 'name';
    END LOOP;
    RETURN jsonb_build_object('paths', to_jsonb(file_paths),
        'deletions', to_jsonb(deletion_paths), 'decoded_bytes', decoded_total);
END;
$$;

CREATE FUNCTION coordinator_v2.mutate_worker_v2(kind_value text, request_value jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, coordinator_v2 AS $$
DECLARE
    workspace uuid := coordinator_v2.current_workspace();
    selected coordinator_v2.work%ROWTYPE;
    operation_value text := request_value ->> 'operation_id';
    semantic jsonb := request_value - ARRAY[
        'protocol', 'work_id', 'worker_id', 'instance_id', 'fence', 'lease_token',
        'cancellation_version', 'request_digest'];
    request_sha_value text := coordinator_v2.sha256_text(coordinator_v2.canonical_json(semantic));
    original_authority jsonb := request_value - ARRAY[
        'operation_id', 'sequence', 'phase', 'codex_thread_id', 'checkpoint_state',
        'base_sha', 'expected_branch_head', 'message', 'files', 'deletions',
        'check_summaries', 'request_digest', 'status', 'candidate_id',
        'review_verdict', 'summary_code', 'checks'];
    receipt coordinator_v2.worker_receipt%ROWTYPE;
    response_value jsonb;
    candidate_value uuid;
    manifest_bytes bytea;
    manifest_sha_value text;
    paths text[];
    deletions text[];
    decoded_total integer;
    admission coordinator_v2.work_admission%ROWTYPE;
    validation jsonb;
    accepted_value jsonb;
BEGIN
    IF kind_value NOT IN ('checkpoint', 'submit_candidate', 'complete')
       OR operation_value IS NULL OR operation_value !~ '^[A-Za-z0-9._:-]{8,80}$'
       OR coordinator_v2.valid_authority(request_value) IS DISTINCT FROM TRUE THEN
        PERFORM coordinator_v2.fail('invalid_request');
    END IF;
    IF kind_value = 'submit_candidate' AND (
        jsonb_typeof(request_value -> 'request_digest') IS DISTINCT FROM 'string'
        OR request_value ->> 'request_digest' !~ '^[0-9a-f]{64}$'
        OR request_value ->> 'request_digest' IS DISTINCT FROM coordinator_v2.sha256_text(
            coordinator_v2.canonical_json(request_value - 'request_digest'))
    ) THEN
        PERFORM coordinator_v2.fail('invalid_request');
    END IF;
    SELECT * INTO selected FROM coordinator_v2.work
    WHERE workspace_id = workspace AND work_id = request_value ->> 'work_id' FOR UPDATE;
    SELECT * INTO receipt FROM coordinator_v2.worker_receipt
    WHERE workspace_id = workspace AND work_id = request_value ->> 'work_id'
      AND kind = kind_value AND operation_id = operation_value;
    IF FOUND THEN
        IF receipt.request_sha <> request_sha_value THEN
            PERFORM coordinator_v2.fail('idempotency_conflict');
        ELSIF receipt.original_authority <> original_authority THEN
            PERFORM coordinator_v2.fail('stale_or_invalid_lease');
        END IF;
        RETURN receipt.response;
    END IF;
    IF NOT FOUND AND selected.work_id IS NULL THEN PERFORM coordinator_v2.fail('not_found'); END IF;
    IF selected.state = 'terminal' THEN PERFORM coordinator_v2.fail('terminal_immutable'); END IF;
    IF coordinator_v2.authority_matches(selected, request_value) IS DISTINCT FROM TRUE THEN
        PERFORM coordinator_v2.fail('stale_or_invalid_lease');
    END IF;
    SELECT * INTO admission FROM coordinator_v2.work_admission
    WHERE workspace_id = workspace AND work_id = selected.work_id AND admission_sha = selected.admission_sha;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid_admission_binding';
    END IF;
    IF selected.accepted_candidate_id IS NOT NULL THEN
        SELECT jsonb_build_object('candidate_id', candidate_id, 'manifest_sha', manifest_sha)
        INTO accepted_value FROM coordinator_v2.candidate
        WHERE workspace_id = workspace AND work_id = selected.work_id
          AND candidate_id = selected.accepted_candidate_id;
    END IF;

    IF kind_value = 'checkpoint' THEN
        IF (NOT coordinator_v2.exact_keys(request_value, ARRAY[
            'cancellation_version', 'checkpoint_state', 'codex_thread_id', 'fence',
            'instance_id', 'lease_token', 'operation_id', 'phase', 'protocol',
            'sequence', 'work_id', 'worker_id'
        ]) OR jsonb_typeof(request_value -> 'sequence') <> 'number'
           OR request_value ->> 'sequence' !~ '^[1-9][0-9]*$'
           OR (request_value ->> 'sequence')::bigint <= selected.checkpoint_sequence
           OR request_value ->> 'phase' NOT IN (
                'checkout_ready', 'codex_started', 'working', 'testing', 'candidate_ready')
           OR jsonb_typeof(request_value -> 'checkpoint_state') IS DISTINCT FROM 'string'
           OR octet_length(COALESCE(request_value ->> 'checkpoint_state', '')) > 4096
           OR jsonb_typeof(request_value -> 'codex_thread_id') NOT IN ('null', 'string')
           OR (request_value -> 'codex_thread_id' <> 'null'::jsonb AND (
                octet_length(request_value ->> 'codex_thread_id') NOT BETWEEN 1 AND 256
                OR request_value ->> 'codex_thread_id' !~ '^[ -~]+$'))
           OR (request_value ->> 'phase' <> 'checkout_ready'
                AND request_value -> 'codex_thread_id' = 'null'::jsonb)
           OR (selected.codex_thread_id IS NOT NULL
                AND request_value ->> 'codex_thread_id' <> selected.codex_thread_id)) IS NOT FALSE THEN
            PERFORM coordinator_v2.fail('invalid_request');
        END IF;
        IF octet_length(jsonb_build_object(
            'protocol', 'worker-v2', 'work_id', selected.work_id, 'work_type', selected.work_type,
            'worker_id', selected.worker_id, 'instance_id', selected.instance_id, 'fence', selected.fence,
            'lease_token', selected.lease_token, 'lease_expires_at', to_char(
                selected.lease_expires_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
            'cancellation_version', selected.cancellation_version, 'admission_sha', selected.admission_sha,
            'source_text', admission.source_text, 'acceptance', admission.acceptance,
            'repository', jsonb_build_object('full_name', admission.repository_full_name,
                'base_sha', admission.base_sha, 'candidate_branch', admission.candidate_branch,
                'allowed_path_prefixes', to_jsonb(admission.allowed_path_prefixes)),
            'checkout_path', '/v2/work/' || selected.work_id || '/checkout',
            'prior_checkpoint', jsonb_build_object('sequence', (request_value ->> 'sequence')::bigint,
                'phase', request_value ->> 'phase', 'codex_thread_id', request_value -> 'codex_thread_id',
                'checkpoint_state', request_value ->> 'checkpoint_state'),
            'codex_thread_id', request_value -> 'codex_thread_id', 'accepted_candidate', accepted_value,
            'limits', jsonb_build_object('max_files', 32, 'max_file_bytes', 65536,
                'max_total_bytes', 262144, 'max_deletions', 32, 'max_json_bytes', 393216)
        )::text) > 16384 THEN
            PERFORM coordinator_v2.fail('request_too_large');
        END IF;
        UPDATE coordinator_v2.work SET
            checkpoint = jsonb_build_object('sequence', (request_value ->> 'sequence')::bigint,
                'phase', request_value ->> 'phase', 'codex_thread_id', request_value -> 'codex_thread_id',
                'checkpoint_state', request_value ->> 'checkpoint_state'),
            checkpoint_sequence = (request_value ->> 'sequence')::bigint,
            codex_thread_id = COALESCE(request_value ->> 'codex_thread_id', codex_thread_id),
            updated_at = clock_timestamp()
        WHERE workspace_id = workspace AND work_id = selected.work_id;
        response_value := jsonb_build_object('accepted_sequence', (request_value ->> 'sequence')::bigint,
            'lease_expires_at', to_char(selected.lease_expires_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'));
    ELSIF kind_value = 'submit_candidate' THEN
        IF selected.work_type <> 'implementation' OR selected.state <> 'leased' THEN
            PERFORM coordinator_v2.fail('work_type_forbidden');
        END IF;
        IF (request_value ->> 'base_sha' <> admission.base_sha
           OR request_value ->> 'expected_branch_head' <> admission.base_sha) IS NOT FALSE THEN
            PERFORM coordinator_v2.fail('stale_head');
        END IF;
        validation := coordinator_v2.validate_candidate_v2(
            request_value, admission.allowed_path_prefixes, admission.base_sha);
        SELECT array_agg(value #>> '{}' ORDER BY ordinality) INTO paths
        FROM jsonb_array_elements(validation -> 'paths') WITH ORDINALITY AS item(value, ordinality);
        SELECT array_agg(value #>> '{}' ORDER BY ordinality) INTO deletions
        FROM jsonb_array_elements(validation -> 'deletions') WITH ORDINALITY AS item(value, ordinality);
        decoded_total := (validation ->> 'decoded_bytes')::integer;
        manifest_bytes := convert_to(coordinator_v2.canonical_json(request_value), 'UTF8');
        IF octet_length(manifest_bytes) > 393216 THEN PERFORM coordinator_v2.fail('request_too_large'); END IF;
        manifest_sha_value := encode(sha256(manifest_bytes), 'hex');
        candidate_value := gen_random_uuid();
        INSERT INTO coordinator_v2.candidate (
            workspace_id, candidate_id, work_id, operation_id, manifest_sha, canonical_manifest,
            manifest, base_sha, expected_head, message, ordered_paths, ordered_deletions,
            file_count, deletion_count, decoded_bytes
        ) VALUES (
            workspace, candidate_value, selected.work_id, operation_value, manifest_sha_value, manifest_bytes,
            request_value, admission.base_sha, admission.base_sha, request_value ->> 'message',
            COALESCE(paths, ARRAY[]::text[]), COALESCE(deletions, ARRAY[]::text[]),
            jsonb_array_length(request_value -> 'files'),
            jsonb_array_length(request_value -> 'deletions'), decoded_total
        );
        INSERT INTO coordinator_v2.publication (
            workspace_id, publication_id, candidate_id, work_id, manifest_sha, base_sha,
            expected_head, message, canonical_manifest
        ) VALUES (
            workspace, gen_random_uuid(), candidate_value, selected.work_id, manifest_sha_value,
            admission.base_sha, admission.base_sha, request_value ->> 'message', manifest_bytes
        );
        UPDATE coordinator_v2.work SET state = 'candidate_ready', accepted_candidate_id = candidate_value,
            revision = revision + 1, updated_at = clock_timestamp()
        WHERE workspace_id = workspace AND work_id = selected.work_id;
        response_value := jsonb_build_object('candidate_id', candidate_value,
            'manifest_sha', manifest_sha_value, 'status', 'candidate_ready');
    ELSE
        IF (NOT coordinator_v2.exact_keys(request_value, ARRAY[
            'cancellation_version', 'candidate_id', 'checks', 'fence', 'instance_id',
            'lease_token', 'operation_id', 'protocol', 'review_verdict', 'status',
            'summary_code', 'work_id', 'worker_id'
        ]) OR request_value ->> 'status' NOT IN ('succeeded', 'failed', 'scope_return')
           OR jsonb_typeof(request_value -> 'status') IS DISTINCT FROM 'string'
           OR jsonb_typeof(request_value -> 'summary_code') IS DISTINCT FROM 'string'
           OR request_value ->> 'summary_code' !~ '^[a-z0-9_]{1,80}$'
           OR jsonb_typeof(request_value -> 'checks') <> 'array'
           OR jsonb_array_length(request_value -> 'checks') > 32
           OR EXISTS (
                SELECT 1 FROM jsonb_array_elements(request_value -> 'checks') AS item
                WHERE NOT coordinator_v2.exact_keys(item, ARRAY['name', 'outcome'])
                   OR jsonb_typeof(item -> 'name') IS DISTINCT FROM 'string'
                   OR jsonb_typeof(item -> 'outcome') IS DISTINCT FROM 'string'
                   OR octet_length(item ->> 'name') NOT BETWEEN 1 AND 80
                   OR item ->> 'outcome' NOT IN ('PASS', 'FAIL')
           ) OR EXISTS (
                SELECT 1 FROM (
                    SELECT item ->> 'name' AS name,
                        lag(item ->> 'name') OVER (ORDER BY ordinality) AS prior
                    FROM jsonb_array_elements(request_value -> 'checks')
                        WITH ORDINALITY AS entry(item, ordinality)
                ) AS ordered
                WHERE prior IS NOT NULL
                  AND convert_to(name, 'UTF8') <= convert_to(prior, 'UTF8')
           )) IS NOT FALSE THEN
            PERFORM coordinator_v2.fail('invalid_request');
        END IF;
        IF request_value ->> 'status' = 'succeeded' THEN
            IF selected.work_type = 'implementation' AND (
                selected.state <> 'candidate_ready'
                OR request_value ->> 'candidate_id' IS DISTINCT FROM selected.accepted_candidate_id::text
                OR request_value -> 'review_verdict' IS DISTINCT FROM 'null'::jsonb) THEN
                PERFORM coordinator_v2.fail('invalid_request');
            END IF;
            IF selected.work_type = 'review' AND (
                selected.state <> 'leased' OR request_value -> 'candidate_id' IS DISTINCT FROM 'null'::jsonb
                OR request_value ->> 'review_verdict' IS NULL
                OR request_value ->> 'review_verdict' NOT IN ('PASS', 'BLOCK')) THEN
                PERFORM coordinator_v2.fail('invalid_request');
            END IF;
        ELSIF request_value -> 'candidate_id' IS DISTINCT FROM 'null'::jsonb
           AND (selected.state <> 'candidate_ready'
                OR request_value ->> 'candidate_id' IS DISTINCT FROM selected.accepted_candidate_id::text) THEN
            PERFORM coordinator_v2.fail('invalid_request');
        ELSIF request_value -> 'review_verdict' IS DISTINCT FROM 'null'::jsonb THEN
            PERFORM coordinator_v2.fail('invalid_request');
        ELSIF request_value ->> 'status' IN ('failed', 'scope_return') AND selected.state = 'candidate_ready' THEN
            IF EXISTS (SELECT 1 FROM coordinator_v2.publication
                WHERE workspace_id = workspace AND candidate_id = selected.accepted_candidate_id
                  AND state <> 'pending') THEN
                PERFORM coordinator_v2.fail('publication_already_authorized');
            END IF;
            UPDATE coordinator_v2.publication SET state = 'failed',
                result = jsonb_build_object('code', 'worker_' || (request_value ->> 'status')),
                terminal_at = clock_timestamp(), updated_at = clock_timestamp()
            WHERE workspace_id = workspace AND candidate_id = selected.accepted_candidate_id AND state = 'pending';
        END IF;
        UPDATE coordinator_v2.work SET state = 'terminal', terminal_outcome = request_value ->> 'status',
            terminal_summary_code = request_value ->> 'summary_code', terminal_at = clock_timestamp(),
            worker_id = NULL, instance_id = NULL, lease_token = NULL, lease_expires_at = NULL,
            revision = revision + 1, updated_at = clock_timestamp()
        WHERE workspace_id = workspace AND work_id = selected.work_id;
        response_value := jsonb_build_object('work_id', selected.work_id, 'status', request_value ->> 'status',
            'completed_at', to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'));
    END IF;

    INSERT INTO coordinator_v2.worker_receipt (
        workspace_id, work_id, kind, operation_id, request_sha, original_authority,
        response_code, response, completed_at
    ) VALUES (workspace, selected.work_id, kind_value, operation_value, request_sha_value,
        original_authority, 200, response_value, clock_timestamp());
    RETURN response_value;
END;
$$;

CREATE FUNCTION coordinator_v2.cancel_v2(work_id_value text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, coordinator_v2 AS $$
DECLARE
    workspace uuid := coordinator_v2.current_workspace();
    selected coordinator_v2.work%ROWTYPE;
    publication_value coordinator_v2.publication%ROWTYPE;
    outcome text := 'cancelled';
BEGIN
    SELECT * INTO selected FROM coordinator_v2.work
    WHERE workspace_id = workspace AND work_id = work_id_value FOR UPDATE;
    IF NOT FOUND THEN PERFORM coordinator_v2.fail('not_found'); END IF;
    IF selected.accepted_candidate_id IS NOT NULL THEN
        SELECT * INTO publication_value FROM coordinator_v2.publication
        WHERE workspace_id = workspace AND candidate_id = selected.accepted_candidate_id FOR UPDATE;
    END IF;
    IF selected.state = 'terminal' THEN
        IF publication_value.state = 'pending' THEN
            UPDATE coordinator_v2.publication SET state = 'failed',
                result = jsonb_build_object('code', 'cancelled_before_authorization'),
                terminal_at = clock_timestamp(), updated_at = clock_timestamp()
            WHERE workspace_id = workspace AND publication_id = publication_value.publication_id;
            outcome := 'cancelled_before_authorization';
        ELSIF publication_value.state IN ('authorized', 'reconciling') THEN
            outcome := 'publication_already_authorized';
        ELSIF publication_value.state IN ('applied', 'stale_head', 'failed') THEN
            outcome := COALESCE(publication_value.result ->> 'code', publication_value.state);
        ELSE
            outcome := selected.terminal_outcome;
        END IF;
        RETURN jsonb_build_object('work_id', selected.work_id, 'result', outcome,
            'cancellation_version', selected.cancellation_version);
    END IF;
    IF publication_value.state IN ('authorized', 'reconciling') THEN
        outcome := 'publication_already_authorized';
    ELSIF publication_value.state IN ('applied', 'stale_head', 'failed') THEN
        outcome := COALESCE(publication_value.result ->> 'code', publication_value.state);
    ELSIF publication_value.publication_id IS NOT NULL AND publication_value.state = 'pending' THEN
        UPDATE coordinator_v2.publication SET state = 'failed',
            result = jsonb_build_object('code', 'cancelled_before_authorization'),
            terminal_at = clock_timestamp(), updated_at = clock_timestamp()
        WHERE workspace_id = workspace AND publication_id = publication_value.publication_id;
    END IF;
    UPDATE coordinator_v2.work SET state = 'terminal', terminal_outcome = 'cancelled', terminal_at = clock_timestamp(),
        cancellation_version = cancellation_version + 1, worker_id = NULL, instance_id = NULL,
        lease_token = NULL, lease_expires_at = NULL, revision = revision + 1, updated_at = clock_timestamp()
    WHERE workspace_id = workspace AND work_id = selected.work_id
    RETURNING cancellation_version INTO selected.cancellation_version;
    RETURN jsonb_build_object('work_id', selected.work_id, 'result', outcome,
        'cancellation_version', selected.cancellation_version);
END;
$$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA coordinator_v2 FROM PUBLIC;

COMMIT;
