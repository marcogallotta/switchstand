# Standalone Asana test project

`SWITCHSTAND_TEST_PROJECT_GID` optionally admits one exact Asana test project to
Switchstand's existing task/ancestor canonicality check. Leave it empty for the
default production-only behavior. The GID must be plain decimal and distinct
from every operational project GID. Set it in trusted host configuration used
by both the provisioner and controller; candidates do not supply it.

The adapter's eight-project `PROJECTS` tuple remains the production discovery
registry. The test GID extends only the exact task/ancestor admission set. The
same adapter serves provisioning and controller reads, so both factories pass
the trusted setting to its single validation rule. Invalid nonempty settings
fail closed; disabling the setting denies subsequent reads of a test-only
lineage. No new index, source contract, credential, grant, or authority store is
introduced. The admission rule, factory wiring, boundary proof, and operator
note form one useful code change; splitting them would leave an inert partial
capability without a reviewable configured behavior.

The hermetic HTTPX proof reads an exact fictional test-only task and an exact
wrong-project task, then asserts that discovery issues precisely the original
eight production project GETs. This catches the tempting but incorrect change
of appending the test GID to `PROJECTS`. Bounded ancestor and mixed-membership
cases limit the claim. This is landing evidence for the default-off code, not
live Asana activation evidence.

An authorized exact launcher task can be provisioned and read when its own
membership or bounded ancestor lineage reaches this project. Ordinary
`suggest_next` discovery still scans only the eight production projects. Project
membership routes admission; it does not create a WorkId or grant a provider
effect. The existing active-work and grant checks still govern writes.

Landing this default-off code does not activate a live test. Activation needs
separate authorization, an exact disposable test task, trusted configuration
readback, and a current exact-task membership/ancestor read confirming that the
fixture has no production-area membership or ancestor. A task shared with a
production area may remain production-canonical and visible in discovery after
the test setting is disabled, so it cannot prove isolation. Reread membership
before consequential test writes. The current `work_get`/source contracts do
not expose this lineage; the one-home check needs a separately authorized
trusted-host exact-task qualification read. Until that route and current grant
are available, live activation is **NOT_RUN / MISSING_CAPABILITY / UNKNOWN**.

This project separates Asana data only. It shares the existing token, database,
controller, and authorization plane. The setting creates no project or task and
does not change Asana memberships.
