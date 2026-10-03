# Historical standalone Asana test project

> Historical provider-qualification record. The current managed controller and
> ordinary ChatGPT edge are PostgreSQL-only and do not read `ASANA_TOKEN` or
> `SWITCHSTAND_TEST_PROJECT_GID`. Do not put either value in their runtime
> configuration.

The variables remain only for the separately scoped native-certification harness
and opt-in provider contract probe while those provider-era qualification paths
await retirement. They do not qualify the current PostgreSQL runtime or authorize
an Asana effect.

The opt-in provider contract probe reuses the pinned certification project,
ownership marker, pre-clean, and post-clean boundary:

```
PYTHONPATH=src python tests/asana_provider_contract_probe.py
```

It also requires `ASANA_TOKEN`, `SWITCHSTAND_CERTIFICATION_REPO`, and the exact
clean candidate in `SWITCHSTAND_CERTIFICATION_RUNTIME_SHA`. Missing or
non-isolated prerequisites produce `NOT_RUN` without contacting Asana. With
authorized prerequisites, the probe takes the same exclusive supervisor lock,
creates only marker-owned disposable tasks, and exercises exact task parsing,
forced pagination through project search, one scalar write/readback, and
parent/dependency mutation/readback. The isolated fixture has no declared known
custom-field identity/value, so the probe makes no live custom-field claim.
Cleanup refuses an unowned project inventory and runs before and after the probe.
The probe does not inject an ambiguous live write; malformed-response,
fail-closed, idempotency, and UNKNOWN recovery remain hermetic unit-test claims.
Passing this probe establishes only the provider contract for its exact candidate
and isolated project, not production behavior, activation, or effect authority.

This certification fixture separates Asana data only. The setting creates no
project or task and does not change Asana memberships.
