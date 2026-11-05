# Agent code and test quality research

Research snapshot: 4 November 2026. Local examples refer to repository commit
`0e08116569ec7e0c391ad562e976747d00525dd0`.

Agent-written code can satisfy a narrow task and its tests while making the system harder to
understand and change. The useful cleanup target is the causal mechanism: misplaced ownership,
duplicated decisions, unnecessary machinery, or evidence that cannot distinguish correct behavior
from a plausible mistake. Adding more patches, assertions, or instructions can preserve the same
problem in a larger form.

The research findings below remain separate from executable project instructions. The practical
ending records the cleanup direction approved on 5 November 2026; it does not select a replacement
architecture or complete the broader whole-system analysis. The local examples establish specific
limitations; they do not measure how much of Switchstand is defective or establish who authored
the affected code.

## What the research supports

The strongest relevant evidence describes failure mechanisms in particular settings. It does not
support a universal percentage of bad code from current models.

| Source and date | Finding used here | Boundary on interpretation |
| --- | --- | --- |
| [Tang et al., How Coding Agents Fail Their Users](https://arxiv.org/html/2605.29442v2), revision 31 August 2026 | Across 20,574 sessions in 1,639 repositories, developer pushback exposes failures in understanding intent, following constraints, implementation, and reporting. | Observational, selected shared logs and visible corrections; silent defects and successful unchallenged behavior are not equally observable. Session findings are not code-defect prevalence. |
| [Spracklen et al., We Have a Package for You](https://www.usenix.org/conference/usenixsecurity25/presentation/spracklen), USENIX Security 2025 | An experiment with 16 models and 576,000 generated samples demonstrates invented package dependencies. | Older models and bounded Python/JavaScript generation tasks; the study's rates cannot be assigned to today's agent workflows. |
| [Perry et al., Do Users Write More Insecure Code with AI Assistants](https://arxiv.org/abs/2211.03622), CCS 2023, revised 18 December 2023 | In security tasks, participants using a codex-davinci-002 assistant produced less secure solutions and were more confident about security. | A particular older assistant and task setting; evidence for a confidence/correctness mismatch, not a current-model security ranking. |
| [Bekmyradov et al., LLMs taking shortcuts in test generation](https://arxiv.org/html/2604.14437v1), 15 April 2026 | In HANA/LevelDB test generation, compiler-repair loops produced empty tests and removed assertions. Dependency context helped. | Four models, one evaluated HANA component, two repetitions per configuration; repository familiarity and complexity are confounded. It does not prove that all repair loops behave this way. |
| [Hamidi et al., How effective are traditional test criteria](https://arxiv.org/html/2609.09315v1), 8 September 2026 | In selected difficult Python faults, generated inputs could expose divergence while assertions failed to detect it; mutation criteria improved only marginally over coverage criteria. | The final 6,066 faults were selected after removing easy faults and retaining difficult cases. Near-zero detection in that set is not a general test-suite failure rate. |
| [OpenAI, Separating signal from noise in coding evaluations](https://openai.com/index/separating-signal-from-noise-coding-evaluations/), 8 July 2026 | A SWE-Bench Pro audit found implementation-specific tests, omitted requirements, and tests that accepted incomplete fixes. | Benchmark audit, not production-code prevalence. Human inspection of an agent-flagged subset does not measure the pipeline's recall across unflagged tasks. |

The synthesis is that success signals can be wrong in both directions: a poor implementation can
pass weak tests, and a sound alternative can fail tests that encode an incidental implementation.
The opposite evidence matters too: Hamidi et al. report many easy faults before constructing their
difficult subset, and dependency context helped in the HANA study. Agents and tests are useful;
their evidence needs a claim it can actually falsify.

## How local success can produce a poor structure

The following is an engineering explanation consistent with those findings, rather than a measured
causal account of every Switchstand change. A task exposes one failing path, so an agent adds a
guard or wrapper at the nearest visible point. A test reproduces that patch's inputs and calls.
Review checks the diff inside the author's framing. Repeating the cycle can spread the same
decision across callers, grow compatibility paths that nobody needs, and make the test harness
depend on all of them. Every local change may look reasonable while the combined mechanism loses
a clear owner.

Other plausible patterns include inventing an API instead of checking the installed contract,
adding generic machinery for an imagined future case, preserving dead paths because a test uses
them, or adding a new instruction after each failure without checking whether agents follow the
existing route. These are investigation leads until tied to concrete evidence and consequences.

[Google's code review guidance](https://google.github.io/eng-practices/review/reviewer/looking-for.html)
offers a useful independent engineering standard: review system fit, unnecessary generality,
complexity, and whether tests are useful. It is practitioner guidance, not an empirical study of
agent behavior. It also explains why architectural quality cannot be reduced to reproducing a
runtime bug.

## Tests are part of the system being assessed

A test's oracle is its rule for deciding whether a result is correct. A test can execute the
right path with the wrong oracle, or never supply an input that distinguishes the faulty behavior.
Fixtures, mocks, helpers, and repair scripts all contribute code and maintenance burden.

| Failure mechanism | What makes it misleading | Useful challenge |
| --- | --- | --- |
| Weak oracle | An assertion checks success, shape, or revision while missing the behavior at stake. | Name one plausible wrong result that should fail and inspect whether the assertion rejects it. |
| Fixture sameness | Old and new states differ internally but project to the same asserted value. | Make the relevant observable values distinct. |
| Shared false model | Production and mocks agree about a provider, transaction, or subprocess behavior that the real dependency does not have. | Use a narrow real or hermetic boundary example where those semantics matter. |
| Repair gaming | A repair removes a failing assertion, broadens the expected result, or skips the hard case. | Compare the original requirement and fault detection before accepting green. |
| Implementation coupling | Tests freeze helper calls or private sequencing, rejecting behavior-preserving changes. | Assert the public contract unless the ordering itself protects an invariant. |
| Support complexity | A large fixture world recreates production choreography and makes simple changes expensive. | Compare the harness with a smaller setup preserving the same causal regression evidence. |

[Google's mock guidance](https://testing.googleblog.com/2013/05/testing-on-toilet-dont-overuse-mocks.html)
(28 May 2013) explains how mocks can leak implementation details and diverge from real dependencies.
This is a reason to examine a particular model boundary, not to declare a defect from a mock count
or replace every unit test with an integration test.

### Local example of indistinguishable placement fixtures

In [test_canonical_work_runtime.py](../tests/test_canonical_work_runtime.py), `_relations()` always
projects to area `Switchstand` and stage `Doing`. Both
`test_get_retries_when_placements_cross_a_row_version` and
`test_search_retries_whole_page_when_placements_cross_a_row_version` create old and new relations
with that helper, then compare the returned area with the new relation's name.

Those placement assertions cannot distinguish fresh placement data from cached old placement
data when the returned revision is fresh. That limitation follows directly from the identical
observable values. It does not establish a runtime caching defect: the
[runtime implementation](../src/switchstand/tracker/runtime.py) reads relations and checks
work currentness. A targeted mutation run was **NOT_RUN**. A small potential test improvement is
to give the two placements different public values and show that returning the old value fails.

### Local example of an absent hostile input

In [test_wakeful.py](../tests/test_wakeful.py),
`test_sqlite_persists_cursor_and_sanitized_outbox_across_instances` checks that SQLite bytes do not
contain `bad_refresh_token` or `authorization`. Its event factory never supplies those strings.
The test does demonstrate persistence, reopening, and delivery marking; those negative assertions
alone do not demonstrate sanitization of hostile input.

The [store](../src/switchstand/wakeful.py) serializes the supplied event. Before calling that a
security bug, establish which boundary must sanitize: the
[prototype contract](wakeful.md) excludes raw journal and credential material, while
[test_edge_monitor.py](../tests/test_edge_monitor.py) actually supplies a bad-token journal line
and checks the safe queued event. A store sanitizer cannot be inferred as the required remedy.
The real SQLite lease, stale-owner, restart, and recovery tests remain useful evidence to preserve.
The sampled tests also do not inject a failure between state writes and commit; that is a limit on
failure-path evidence, not proof that the implemented rollback is broken.

## Architecture and documentation can be examined directly

A structural concern can be established without an observed production crash. Reading ownership,
callers, state, and boundaries may show that one decision is duplicated or one ordinary change
requires coordinated edits across unrelated owners. A concrete change simulation makes the cost
reviewable: “If the authoritative status rule changes, these three paths each encode it, with no
common owner.” Verify that claim against actual callers and guards, then compare a smaller route.
Disliking a layer or a name is insufficient evidence.

Useful examination sizes differ. Tracker, Agentic Docs, documentation, test infrastructure,
observability, MCP, and Wakeful are distinct examples, not an exhaustive decomposition. A concern
may live in one function, one subsystem, or a boundary between them. A whole-system view is needed
when local fixes move responsibility elsewhere; it does not justify redesigning every subsystem
in one cleanup package.

Documentation is also a maintenance surface. At the snapshot, AGENTS.md was approximately 2,038
words with 38 links to 15 distinct local documents; code-quality.md was approximately 5,918 words.
Their combined size illustrates reading burden before task-specific material, but routing is
conditional: those figures do not mean every change requires reading every linked document.
Marco's correction that the current documents are too complicated is a usability concern, not
answered by pointing to more written checks.

[test_documentation.py](../tests/test_documentation.py) includes assertions about required headings
and routing phrases. They can detect textual drift. They cannot establish that an agent discovers,
understands, or follows the route in an actual task. Existing
[code quality guidance](code-quality.md) already distinguishes green checks from quality, and the
[usage model](how-marco-uses-switchstand.md#current-observed-use-and-continuity-limit-marco-2026-10-06)
explicitly distinguishes continuation instructions from observed adoption. Written coverage and
behavioral adoption are separate questions.

## A practical cleanup approach

Marco approved correcting this document, landing the documentation through independent review and
CI, then auditing Mailbox before Tracker with Code Cleanup and Code Cleanup 2. Both agents challenge
the resulting plan before implementation, testing, deployment, and testing the deployed behavior.
The aim is substantial simplification that makes ordinary agent changes faster and safer. Existing
[code quality guidance](code-quality.md) owns design, review, implementation and qualification;
this research adds no competing framework or new approval ceremony. Migrations and new consequential
choices return to Marco under [Human Input](human-input.md) and [Human Review](human-review.md).

### What the comparative reviews actually established

The full document review (WorkId `2396ed80-11de-587e-a596-a85d358b1455`) and code review
(`a32da801-cc4d-5568-bbca-5b850609eb30`), dated 5 November 2026, distinguish navigation gains from
simplification. The code comparison used `37f86f7defa1fe1a3cd18ef567ccb8ad426c3bfd` to
`41f84f197498726bebd20e5be29553bdb3f58b3a`; the verified bundle SHA-256 was
`93c2cbc33651cb87fd3fba867971061539380114cf3225c6100210ccb76d0b88`. These were static reviews,
with no new live qualification or comparable agent-change timing experiment.

Tracker's eleven core owners changed from about 3,300 lines/111 functions to 3,266/110; five long
critical methods retained their lengths. Mailbox's six owners grew from about 2,904 lines/117
functions to 2,998/119, plus about 200 lines of compatibility shims. Many moves had 92–99% Git
similarity. These figures direct inspection; they neither establish defects nor measure quality.
Moving files did improve owner navigation, but broad complexity reduction and developer-speed
payoff were not demonstrated.

There are substantive counterexamples: removing the unprotected Tracker writer retired a duplicate
API and stale tests; named mailbox send/replay gained a common owner. Real PostgreSQL near-match
search tests, restart/ACK/result tests and wake-fencing tests reject meaningful mistakes. Preserve
that evidence. The sampled test growth and green checks do not establish a suite-wide oracle audit.
The document review withdrew its uncalibrated global 4/10; the code review's subjective 5.5-to-6.0
comparison is also no measured baseline. Historical predecessor cleanup principles support one
owner per rule and lower change fanout, but are evidence rather than current approval.

### Start with consumers and remove a real burden

Use the current [Mailbox](mailbox.md) and [Tracker](tracker.md) owner maps and
[observed usage](how-marco-uses-switchstand.md#current-observed-use-and-continuity-limit-marco-2026-10-06)
to trace actual journeys and consumers. Mailbox comes first: follow named send, replay, receipt,
restart, result and ACK through their current owners. Then examine Tracker read/search and protected
writes. Confirm required behavior and non-goals against the approved work; do not infer that a
feature is dispensable from low observed use or invent scale to justify a framework.

For a candidate, show a concrete wrong result or ordinary maintenance change that exposes the cost:
the same decision implemented twice, needless coordinated edits, an obsolete compatibility caller,
or scaffolding that costs more than the regression it protects. Inspect real callers, guards and
contracts before choosing KEEP, CUT, MERGE or RETIRE. Compare keeping the current mechanism with the
strongest smaller route. Name the production, tests and documentation that disappear or become
simpler, and the remaining authoritative owner. A forwarding layer moved to a new folder is not
that outcome. A long function alone is not a reason to split one transaction or authority decision.

Rank candidates by useful burden removed, confidence and risk rather than lines deleted. Existing
mailbox import shims, for example, remain needed while current consumers use them; retirement needs
a bounded consumer migration or proof that the last supported consumer is gone. A tiny assertion
repair can improve evidence, but does not substitute for the requested substantive simplification.
Do not add a scheduler, state store, general framework or broad documentation bundle to conduct
this audit. Keep findings, alternatives and dispositions in the existing work and plan.

### Evaluate risk and what tests can mitigate

For each proposed cut or merger, explain the plausible failure, affected users/data/effects and
impact; the evidence that reduces that risk; and the residual unknowns. Name the first safe
verification and how to revert or recover if it fails. Distinguish a reversible code change from
an irreversible external effect: rolling back code does not undo a sent message or corrupted state.
Tests reduce uncertainty about the cases and boundaries they exercise; they cannot settle intended
product scope, prove absent consumers or make an untested recovery path safe.

Treat tests and their support code as cleanup subjects. Name a plausible faulty implementation
and show the oracle rejects it, using distinct old/new values or hostile input where relevant.
A targeted counterexample or mutation is useful when it changes the decision; broad mutation quotas
and coverage totals do not follow. Preserve demonstrated causal regressions while cutting duplicate
or implementation-coupled scaffolding. Use real or hermetic dependency boundaries when mocks cannot
represent transaction, restart, provider or subprocess semantics. Static caller/ownership evidence
can justify structural simplification without manufacturing a runtime defect.

For example, the identical placement fixtures described above leave stale-placement risk untested:
distinct public values and an old-value counterexample can improve that oracle, without proving all
currentness races safe. Mailbox's optional native wake holds a database row lock during daemon RPC;
a disposable-PostgreSQL experiment recorded in feedback WorkId
`0e590127-94f4-5277-b003-0d5553dbcf48` blocked takeover and ACK during a synthetic wait. Live activation
and latency remain UNKNOWN, and the lock also prevents stale effects. Removing it needs evidence
that stale owners cannot act and that delayed/failed RPC, takeover and ACK remain safe. This is a
risk to investigate, not an approved one-line deletion or a demonstrated live incident.

Keep authority, identity/currentness, idempotency/replay, truthful unknown outcomes, transaction
atomicity and recovery intact. A smaller implementation that weakens those guarantees is not a
successful cleanup. Review and test the exact changed candidate through the existing quality route;
verify deployed behavior separately before claiming operational success.

### Make the 7–8 ambition defensible

Marco's desired 7–8/10 is an ambition for a lean, understandable system with meaningful evidence,
not a measured score or a new scoring scheme. Assess each dimension in plain language with before
and after examples; do not average away a serious weakness in a critical function or journey.

| Dimension | Evidence that would support the target |
| --- | --- |
| Useful scope | Retained mechanisms have required consumers; an actual unnecessary path and its tests/docs are retired, or a justified KEEP explains why cutting would lose needed behavior. |
| Ownership and complexity | A rule has one authoritative implementation; a representative change needs fewer coordinated owners/edits, and inspecting the difficult methods confirms the burden fell rather than moved. |
| Tests and maintenance | Plausible faults are rejected at the relevant boundary; valuable regressions survive with less duplicate setup, coupling or runtime. Record support cost as well as production cost. |
| Agent work | Comparable ordinary changes show time to find the owner, reach the first valid test and deliver a correct change, with rework and Marco interruptions recorded. Distinguish measured improvement from a hypothesis and explain task differences. |
| Documentation | One maintained authority per subject leads a fresh agent to the right owner; obsolete or duplicate guidance disappears, with an actual navigation example rather than a new checklist. |

Use the audit to choose the smallest useful vertical change that removes real burden, challenge it
with both Cleanup agents, then implement and compare the resulting evidence with the baseline.
Report weak dimensions, untested cases and residual risks explicitly. If agent-speed trials or
important boundary evidence are missing, improvement can be reported narrowly; claiming the target
has been achieved remains unsupported. Research, file moves, passing CI and a successful deployment
each establish different things. None alone completes the cleanup goal.
