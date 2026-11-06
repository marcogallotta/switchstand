# Agent code and test quality research

Research snapshot: 4 November 2026. Local examples refer to repository commit
`0e08116569ec7e0c391ad562e976747d00525dd0`.

Agent-written code can satisfy a narrow task and its tests while making the system harder to
understand and change. The useful cleanup target is the causal mechanism: misplaced ownership,
duplicated decisions, unnecessary machinery, or evidence that cannot distinguish correct behavior
from a plausible mistake. Adding more patches, assertions, or instructions can preserve the same
problem in a larger form.

The research findings remain separate from executable instructions. The ending preserves
comparative evidence and design provenance; the
[cleanup standard](code-quality.md#cleanup-prove-less-work-to-understand-and-change) owns the
current method and acceptance. This snapshot grants no execution authority. Local examples
establish specific limitations, not defect prevalence or authorship.

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

### Historical placement-fixture limitation — addressed

At the research snapshot, [test_canonical_work_runtime.py](../tests/test_canonical_work_runtime.py)
used identical public placement values before and after a revision change. Its get/page-retry
assertions could not distinguish stale placements paired with a fresh revision. This was an oracle
limitation, not evidence of a runtime caching defect; targeted mutation was NOT_RUN in that snapshot.

[PR1183](https://github.com/marcogallotta/switchstand/pull/1183), merged as
`efcadbb0bfc9d9fc297d236d76b581b822fcad20`, addressed this with distinct old/new area and stage
values and explicit public-placement assertions (change `e495dd9b536d099b714c6c69bf0f64738322028c`).
Verified in current base `32187cb860cdb5b5dc6324b07dd48184f4c6c8b5`, tests at lines 79–131 also
cover a stale successful page prefix. The historical example explains the failure mechanism;
it is not an unresolved current defect or a new claim that every race is covered.

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

The [canonical cleanup standard](code-quality.md#cleanup-prove-less-work-to-understand-and-change)
describes five practical outcomes, concrete before/after evidence and subsystem completion review. This research is supporting evidence, not a second strategy or approval.
Historical approval of the 5 November sequence does not resume work stopped by Marco.

### What the comparative reviews actually established

The full document review (WorkId `2396ed80-11de-587e-a596-a85d358b1455`) and code review
(`a32da801-cc4d-5568-bbca-5b850609eb30`), dated 5 November 2026, distinguish navigation gains from
simplification. The code comparison used `37f86f7defa1fe1a3cd18ef567ccb8ad426c3bfd` to
`41f84f197498726bebd20e5be29553bdb3f58b3a`; the verified bundle SHA-256 was
`93c2cbc33651cb87fd3fba867971061539380114cf3225c6100210ccb76d0b88`. Static reviews supplied no live or comparable change-time qualification.

Tracker's eleven core owners changed from about 3,300 lines/111 functions to 3,266/110; five long
critical methods retained their lengths. Mailbox's six owners grew from about 2,904 lines/117
functions to 2,998/119, plus about 200 lines of compatibility shims. Many moves had 92–99% Git
similarity. Moves improved navigation; these counts establish neither defects nor quality. Broad complexity
and speed gains remained unproved.

Substantive gains included retiring the duplicate unprotected Tracker writer/API/tests and sharing
mailbox send/replay ownership. Real PostgreSQL search, restart/ACK/result and wake-fencing tests
reject meaningful faults; this is not a suite-wide oracle audit. The document review withdrew its
uncalibrated 4/10; the code review's subjective 5.5-to-6.0 is also no measured baseline.

### Historical Dish principles and current boundaries

The source is Dish's `dish/docs/agents/design-principles.md`, inspected clean at ai-tools commit
`070e462a8037e66745755c37251490a1cdb8fbe6` ([exact source](https://github.com/marcogallotta/ai-tools/blob/070e462a8037e66745755c37251490a1cdb8fbe6/dish/docs/agents/design-principles.md)).
DP-01 reserves serialization for consequential authority/state; DP-03 rejects invented gates;
DP-04/05 reserve scarce human attention for consequential choices; DP-06 treats PR shape as judgment;
DP-08/09 preserve exact lineage and explicit decision reversals. These support challenging unnecessary
mechanisms and review burden. They are predecessor evidence, not imported Switchstand authority.

The current human-controlled `>=500` gross actual-PR HOLD remains unchanged; deletions count too.
Marco explicitly refused changing this limit. Neither predecessor principles nor strategic
candidates waive [current policy](code-quality.md#exceptional-loc-exemption-gate) or its distinct
governed-package caps.

### A concrete risk example to retain

Mailbox native wake holds a row lock during daemon RPC. Disposable-PostgreSQL feedback
`0e590127-94f4-5277-b003-0d5553dbcf48` recorded blocked takeover/ACK during synthetic waiting.
Live latency remains UNKNOWN; the lock prevents stale effects. Removal requires stale-owner,
delayed/failed RPC, takeover and ACK safety evidence. This finding proves neither a live incident
nor retirement approval.
