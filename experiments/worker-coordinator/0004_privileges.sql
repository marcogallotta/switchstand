BEGIN;

CREATE ROLE switchstand_coordinator_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE switchstand_coordinator_migrator NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE switchstand_coordinator_runtime LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT switchstand_coordinator_owner TO switchstand_coordinator_migrator;

ALTER SCHEMA coordinator_v2 OWNER TO switchstand_coordinator_owner;
DO $$
DECLARE item record;
BEGIN
    FOR item IN SELECT c.oid::regclass AS identity
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'coordinator_v2' AND c.relkind IN ('r', 'S')
    LOOP
        EXECUTE format('ALTER TABLE %s OWNER TO switchstand_coordinator_owner', item.identity);
    END LOOP;
    FOR item IN SELECT p.oid::regprocedure AS identity
        FROM pg_catalog.pg_proc AS p
        JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
        WHERE n.nspname = 'coordinator_v2'
    LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO switchstand_coordinator_owner', item.identity);
    END LOOP;
END;
$$;

REVOKE ALL ON SCHEMA coordinator_v2 FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA coordinator_v2 FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA coordinator_v2 FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA coordinator_v2 FROM PUBLIC;

GRANT USAGE ON SCHEMA coordinator_v2 TO switchstand_coordinator_runtime;
GRANT EXECUTE ON FUNCTION coordinator_v2.admit_work_v2(jsonb) TO switchstand_coordinator_runtime;
GRANT EXECUTE ON FUNCTION coordinator_v2.claim_v2(uuid, uuid) TO switchstand_coordinator_runtime;
GRANT EXECUTE ON FUNCTION coordinator_v2.renew_v2(jsonb) TO switchstand_coordinator_runtime;
GRANT EXECUTE ON FUNCTION coordinator_v2.checkout_authority_v2(jsonb) TO switchstand_coordinator_runtime;
GRANT EXECUTE ON FUNCTION coordinator_v2.mutate_worker_v2(text, jsonb) TO switchstand_coordinator_runtime;
GRANT EXECUTE ON FUNCTION coordinator_v2.cancel_v2(text) TO switchstand_coordinator_runtime;
GRANT EXECUTE ON FUNCTION coordinator_v2.authorize_publication_v2(text) TO switchstand_coordinator_runtime;
GRANT EXECUTE ON FUNCTION coordinator_v2.claim_publication_v2(uuid) TO switchstand_coordinator_runtime;
GRANT EXECUTE ON FUNCTION coordinator_v2.record_objects_v2(uuid, bigint, uuid, text, text, text)
    TO switchstand_coordinator_runtime;
GRANT EXECUTE ON FUNCTION coordinator_v2.fail_publication_v2(uuid, bigint, uuid, text)
    TO switchstand_coordinator_runtime;
GRANT EXECUTE ON FUNCTION coordinator_v2.observe_publication_v2(uuid, bigint, uuid, text, text, text, boolean)
    TO switchstand_coordinator_runtime;
GRANT EXECUTE ON FUNCTION coordinator_v2.export_work_v2(text, boolean) TO switchstand_coordinator_runtime;

COMMIT;
