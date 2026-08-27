# Native selection v1

`native-selection-v1` is the frozen pure boundary between the B1 observation owner and later
native action, current-target, browser-journey, and HTTP-composition lanes. The production
resolver is `switchstand.native_selection.resolve_native_selection`; it performs no I/O, clock
read, acquisition, caching, persistence, fallback, or production composition.

The caller supplies an exact selection pair (`observationRunRef`, `agentRef`), the current B1
observation, an explicit current time, and an explicit maximum complete-pass age. Both references
must be opaque, ephemeral, run-local values allocated outside native App Server identity. A B1
process restart must allocate a new observation-run reference even if an agent reference string
is reused. The observation's `agentRecords` registry distinguishes a never-issued reference from
a known agent that is no longer present.

Freshness uses only `latestCompletePassCompletedAt`. A future, missing, invalid, or older-than-
maximum completion time is stale; equality with the maximum is fresh. Native update/activity,
turn, transcript, and inferred-liveness timestamps are ignored. Optional display input has the
shape `{"value": "...", "provenance": "explicitly-safe"}`. Only exact `name` and
`agentNickname` values with that provenance are projected. Current B1 may continue to omit both,
and `preview` is never eligible.

Success is the closed DTO containing only `version`, the exact reference pair, `connected`,
`present`, and optional safe display fields. Failure is only one fixed code/message pair, in this
precedence: `INVALID_AGENT_REF`, `APP_SERVER_DISCONNECTED`, `OBSERVATION_STALE`, then
`AGENT_NOT_PRESENT`. Unknown and forbidden observation fields are not copied. The companion
validator rejects missing, unknown, and forbidden DTO fields.

This foundation exclusively owns the resolver, DTO validator, shared golden fixtures, and their
Python/Node contract and privacy tests. Follow-up lanes may own native exact-identity actions,
board/current-target projection and persistence, browser/live journeys, or HTTP composition
against fakes. They consume this contract without duplicating it. Only the designated integrator
edits this module, the shared fixtures, production composition/application wiring, shared test
configuration, or other cross-lane hotspots.
