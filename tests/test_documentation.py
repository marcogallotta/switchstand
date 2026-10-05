import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).parents[1]
REQUIRED_ENTRY_POINTS = (
    "AGENTS.md",
    "CLAUDE.md",
    "docs/architecture.md",
    "docs/code-quality.md",
    "docs/development.md",
    "docs/how-marco-uses-switchstand.md",
    "docs/north-star.md",
    "docs/roadmap.md",
    "docs/research-sources.md",
    "docs/root-cause-analysis.md",
)
INLINE_DESTINATION = re.compile(r"!?\[[^]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))")


def broken_relative_destinations(root: Path, documents: list[Path]) -> list[str]:
    broken = []
    for document in documents:
        for match in INLINE_DESTINATION.finditer(document.read_text()):
            destination = match.group(1) or match.group(2)
            parsed = urlsplit(destination)
            if parsed.scheme or parsed.netloc or destination.startswith("/") or not parsed.path:
                continue
            target = (document.parent / unquote(parsed.path)).resolve()
            if not target.is_relative_to(root) or not target.exists():
                broken.append(f"{document.relative_to(root)} -> {destination}")
    return broken


def test_repository_relative_markdown_destinations_exist():
    documents = [*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md")]
    assert broken_relative_destinations(ROOT, documents) == []


def test_relative_markdown_check_rejects_a_missing_destination(tmp_path):
    (tmp_path / "existing file.md").write_text("")
    document = tmp_path / "guide.md"
    document.write_text(
        "[existing](existing%20file.md?view=1#section)\n"
        "[external](https://example.com/missing.md)\n"
        "[absolute](/not-a-repository-path.md)\n"
        "[fragment](#local-heading)\n"
        "[broken](missing.md#section)\n"
    )

    assert broken_relative_destinations(tmp_path, [document]) == ["guide.md -> missing.md#section"]


def test_required_documentation_entry_points_exist():
    missing = [path for path in REQUIRED_ENTRY_POINTS if not (ROOT / path).is_file()]
    assert missing == []


def test_architecture_separates_managed_worker_and_root_compact_controls():
    architecture = " ".join((ROOT / "docs/architecture.md").read_text().split())

    assert "Managed Codex carries an always-on developer-instruction contract" in architecture
    assert "only its current role/phase package" in architecture
    assert "does not reuse Root Coordinator manifest, currentness, tracker, or compact-hook controls" in architecture


def test_claude_bootstrap_imports_canonical_agents_file():
    imports = {
        match.group("path")
        for line in (ROOT / "CLAUDE.md").read_text().splitlines()
        if (match := re.fullmatch(r"@(?P<path>[^\s]+)", line.strip()))
    }

    assert "AGENTS.md" in imports


def test_agents_coordination_contract_is_discoverable():
    agents = (ROOT / "AGENTS.md").read_text()

    required_rules = (
        "Meaningful work needs an exact WorkId",
        "live mitigation attaches it when safe",
        "Acknowledge before nontrivial reasoning, tools, or waits",
        "explicit response-time budget",
        "smallest useful decision-bearing response within it",
        "clearly mark uncertainty and continue",
        "empty acknowledgement or status-only reply does not satisfy the budget",
        "[human interaction](docs/human-interaction.md)",
        "review ambiguity, consequence, and conflict",
        "Worker or child sends its parent the question, evidence, recommendation, actual blocking consequence",
        "safe option-preserving work",
        "continues that work while waiting",
        "only the parent/Coordinator asks Marco",
        "Root coordinates and integrates",
        "Proactively fork bounded, safely independent substantive work already assigned, claimed, or in flight",
        "keep tracking and integration at Root",
        "Never dispatch unassigned work or filler",
        "use every safe slot, count product-gate review as product work",
        "`STOP` pauses new dispatch and re-grounds",
        "Disposition each Worker terminal result immediately",
        "A lower-priority question never interrupts higher-priority work",
        "rejecting a question does not reject the work",
        "Continue executable authorized work",
        "Keep one mutation owner per writable surface and one integration owner for shared surfaces",
        "Every child/delegated obligation is terminal, stopped, or transferred with attributable pickup",
    )

    assert [rule for rule in required_rules if rule not in agents] == []

    working_with_marco = agents.split("## Working with Marco", 1)[1].split(
        "\n## Repository bootstrap", 1
    )[0]
    assert len(working_with_marco.encode()) <= 2500


def test_agents_routes_only_current_role_function_context():
    agents = (ROOT / "AGENTS.md").read_text()
    roles = agents.split("## Codex roles and shared engineering process", 1)[1]

    required = (
        "Codex has exactly two roles: **Coordinator** and **Worker**",
        "Root is the Coordinator for the overall assigned portfolio/integration",
        "Coordinator in a delegated lane owns only that lane",
        "**Implementation Worker:** exact task + current Implementation Specification + current Design Specification",
        "**Review Worker / eligible Coordinator reviewer:** exact review occurrence/candidate",
        "**Integration/landing Coordinator:** exact candidate/current target",
        "Coordinator challenge of scope growth, review overreach, test/qualification ratcheting",
        "Managed Workers use only procedures/references carried by their bound current package",
        "Missing required guidance makes only that action `UNKNOWN`/unavailable",
        "Every material implementation gets final live [Human Review](docs/human-review.md) before dispatch",
        "mechanically established required-currentness/applicable-invariant contradiction",
    )
    moved_detail = (
        "missed expected checkpoint",
        "explicit low confidence about a named consequential decision",
        "Target advancement alone does not require rebasing",
        "Activating any client-visible ChatGPT MCP schema",
        "When changing canary behavior/lifecycle",
    )

    assert [rule for rule in required if rule not in roles] == []
    assert [rule for rule in moved_detail if rule in roles] == []
    assert len(roles.encode()) <= 4000


def test_code_quality_owner_carries_role_specific_refresh_cadence():
    agents = (ROOT / "AGENTS.md").read_text()
    quality = " ".join((ROOT / "docs/code-quality.md").read_text().split())

    implementer = (
        "refresh on entry/re-entry/context replacement",
        "before the first material commitment",
        "between distinct material slices",
        "after failed hypotheses/material failures/findings/accepted corrections",
        "before material shape changes",
        "before readiness/handoff/completion claims",
    )
    reviewer = (
        "before substantive review",
        "after candidate/evidence/currentness or focused-correction changes",
        "before verdict/handoff",
    )

    assert [rule for rule in implementer if rule not in quality] == []
    assert [rule for rule in reviewer if rule not in quality] == []
    assert "between distinct material slices" not in agents
    assert "before verdict/handoff" not in agents


def test_research_escalation_uses_current_capability_without_provider_routing():
    sources = " ".join((ROOT / "docs/research-sources.md").read_text().split())

    required = (
        "deepen the bounded search with",
        "current web-research capability",
        "directly or through an authorized Worker",
        "Keep the source dispositions",
        "do not involve Marco merely to choose a research provider",
    )
    stale = (
        "request a deeper search by Claude Code",
        "agent-to-Claude-Code route",
    )

    assert [rule for rule in required if rule not in sources] == []
    assert [rule for rule in stale if rule in sources] == []


def test_research_owner_preserves_the_decision_bound_loop():
    agents = " ".join((ROOT / "AGENTS.md").read_text().split())
    sources = " ".join((ROOT / "docs/research-sources.md").read_text().split())

    required = (
        "state the question, desired outcome, timeframe, decision it will inform, and evidence standard",
        "Search broad and short first, then narrow on the strongest leads",
        "Follow a lead only while it can change the answer",
        "set an explicit stop condition and stop when it is met",
        "every material citation is current, reachable, and supports the claim",
        "surface meaningful disagreement rather than hiding or averaging it",
        "Workers return compact cited evidence and source dispositions; Root synthesizes the decision",
    )

    assert [rule for rule in required if rule not in sources] == []
    assert [rule for rule in required if rule in agents] == []


def test_authority_bootstrap_preserves_effect_and_grounding_boundaries():
    agents = (ROOT / "AGENTS.md").read_text()
    authority = agents.split("## Authority and grounding", 1)[1].split(
        "\n## Working with Marco", 1
    )[0]
    normalized = " ".join(authority.split())

    required = (
        "Marco's direct active assignment or an explicit `CURRENT` grant bound to exact WorkId, writable surface and effect",
        "current human-reviewed position controls until superseded",
        "steering authorizes only its exact package/revision/effect",
        "Role, docs, placement, readability, login/tools and procedures never grant authority",
        "needs an exact WorkId; live mitigation attaches it when safe",
        "start/re-enter with `work_get(api_version=\"1\")` without a WorkId",
        "verify the exact green repository SHA",
        "write only the active WorkId",
        "references are read-only",
        "unavailable managed `work_get` does not block ordinary editing",
        "Preserve role/work identity across re-entry",
        "messages, adjacent reads, context, capability and placement do not reassign them",
        "current routed procedure/Contract/Plan and applicable canary",
        "Canary never expands authority; RED suspends only its experimental delta",
        "blocks only its path; unrelated authorized work continues",
        "implementation assignment conditionally authorizes commit/branch/PR",
        "Landing requires current independent review and exact-head/composition gates",
        "deployment, activation, migration, credentials and provider-production effects are separate",
        "reread exact current work/grant and reconcile direction",
        "`STALE/DENIED/UNKNOWN/NOT_RUN/SKIP/MISSING_CAPABILITY`",
        "never blindly retry ambiguity",
        "full `CallToolResult`: `isError` and content before `structuredContent`",
        "Retain exact arguments/preimage for ambiguous/replacement writes",
        "never send diagnostic payloads or reuse an OperationId with changed arguments",
        "Never infer success from readability, status or partial output",
        "Check authorized capabilities before declaring a blocker",
        "Surface disproportion early",
    )

    assert [rule for rule in required if rule not in normalized] == []


def test_agents_keeps_first_minute_incident_contract_before_routed_detail():
    agents = (ROOT / "AGENTS.md").read_text()
    incident = agents.split("### Live incidents", 1)[1].split("\n- ", 1)[0]
    normalized = " ".join(incident.split())

    required = (
        "credible live-user failure enters incident mode; `CODE RED` is optional",
        "Immediately acknowledge, use one operator, inspect current service state and newest logs",
        "`CURRENT` / `HISTORICAL` / `UNKNOWN` truth before broad delegation",
        "smallest safe reversible mitigation before RCA",
        "without bypassing authority or ambiguous-effect safeguards",
        "If local `main` cannot be proved current",
        "always-loaded rules in this **Live incidents** subsection remain the minimum incident procedure",
        "do not delay mitigation for repository synchronization",
        "do not trust a possibly stale routed copy",
        "[live-incident operations](docs/operations-live-incident.md)",
        "[root-cause analysis](docs/root-cause-analysis.md) and never delays incident mitigation",
    )
    assert [rule for rule in required if rule not in normalized] == []
    assert "known-edge-maintenance-window" not in incident
    assert "Close the incident only after" not in incident


def test_agents_keeps_exact_watch_and_authenticated_entry_invariants():
    agents = (ROOT / "AGENTS.md").read_text()
    routing = agents.split("## Work, messages, and routing", 1)[1].split(
        "\n## Codex roles", 1
    )[0]
    normalized = " ".join(routing.split())

    required = (
        "Current meaning lives on the exact WorkId/current notes",
        "between bounded work batches, after blocking calls, on re-entry, and before consequential effects or completion",
        "terminal result, explicit transfer with attributable pickup, human stop/pause/reassignment, or a real access/execution blocker",
        "exact WorkId/message identity, owner/purpose, state, next check, and terminal condition",
        "`poll` or `keep polling` means continue that exact watch in the current active session",
        "Do not convert active polling into a ChatGPT Scheduled task or condition watch",
        "Send/registration is not pickup or completion",
        "inactive sessions do not poll or wake",
        "ambiguous effects remain `UNKNOWN` until reconciled",
        "`START HERE` `1218327002478382`",
        "authenticated provider-neutral Switchstand HTTP/OAuth MCP",
        "Project Settings may point there but is not parallel authority",
    )
    assert [rule for rule in required if rule not in normalized] == []


def test_repository_safety_routes_detail_without_dropping_bootstrap_invariants():
    agents = (ROOT / "AGENTS.md").read_text()
    usage = (ROOT / "docs/how-marco-uses-switchstand.md").read_text()
    safety = agents.split("## Repository bootstrap and safety", 1)[1].split(
        "\n## Work, messages, and routing", 1
    )[0]
    usage = " ".join(usage.split())

    direct = (
        "Shared canonical `main` is read-only except Git reads/fetches",
        "Source edits, staging, commits, and worktree mutation stay in that writer",
        "never reset, clean, or switch primary to fit a task",
        "Tool/login/capability/readable credentials never grant authority",
        "Ordinary ChatGPT without a checkout uses `repository_bundle_get`; Codex/Claude with a repository use Git",
        "Do not inject raw provider/database credentials or broaden permissions",
        "`~/.local/state/switchstand`",
        "`~/.cache/switchstand`",
        "not `/tmp`",
        "Before replacement writes, reread the exact target and preserve user changes",
        "repo-local Git-ignored `friction.md`",
        "Missing feedback capability does not block assigned work",
    )
    friction_owner = (
        "attempted claim, observed result, state-change truth, and smallest clearing action",
        "current remaining-priority view without erasing append-only evidence",
        "Before Coordinator handoff, reconcile that view against current truth",
        "preserve it intact in a dated sibling beside the durable local-state backing file",
        "restart `friction.md` with the current priorities",
        "If the path is not writable",
        "authorized work result or handoff",
        "Missing feedback capability does not block assigned work",
        "Before any replacement write",
        "reread the exact target and preserve user changes",
    )
    bundle_owner = (
        "`repository_bundle_get`, accepts only `current`",
        "verifies the advertised SHA-256",
        "retries `refresh_pending` and never substitutes stale cache",
        "Use bundled current `main`, or prove and check out the exact requested SHA for review",
    )

    assert [rule for rule in direct if rule not in safety] == []
    assert [rule for rule in friction_owner if rule not in usage] == []
    assert [rule for rule in bundle_owner if rule not in usage] == []


def test_coordinator_handoff_docs_preserve_no_claim_semantics():
    agents = " ".join((ROOT / "AGENTS.md").read_text().split())
    architecture = " ".join((ROOT / "docs/architecture.md").read_text().split())
    usage = " ".join((ROOT / "docs/how-marco-uses-switchstand.md").read_text().split())

    assert "plain raw `codex` launch always creates an independent session and never claims" in agents
    assert "Plain Coordinator launches remain independent and never claim it" in architecture
    assert "Plain raw `codex` launches remain independent and never claim it" in usage
    assert "Automatic transfer is disabled until a separate explicit addressed-claim" in agents
    assert "Automatic transfer is disabled until a separate explicit addressed claim" in architecture
    assert "Automatic transfer is disabled until a separate explicit addressed-claim" in usage
    assert "outgoing generation retains its obligations" in agents
    assert "old generation does not retire before the successor acknowledges" in architecture
    assert "obligations have not transferred and the outgoing Coordinator must not retire" in usage
    assert "registration and a later plain launch are not pickup" in usage
    assert "must update `AGENTS.md`, `docs/architecture.md`, and this owner together" in usage


def test_unbound_coordinator_grounding_never_derives_work_from_mailbox_identity():
    agents = " ".join((ROOT / "AGENTS.md").read_text().split())
    architecture = " ".join((ROOT / "docs/architecture.md").read_text().split())
    usage = " ".join((ROOT / "docs/how-marco-uses-switchstand.md").read_text().split())

    assert "ordinary unbound Coordinator never uses bare `work_get` to infer focus" in agents
    assert "mailbox registration or takeover does not supply work identity or authority" in agents
    assert "missing trusted identity is `COVERAGE_GAP / UNKNOWN`" in architecture
    assert (
        "Mailbox registration or same-principal `/root` takeover restores only the durable "
        "messaging address"
    ) in usage
    assert "It must not call bare `work_get` to guess a current focus" in usage
    assert "Bare `work_get` remains the launch-bound managed-worker behavior" in usage


def test_start_here_navigates_without_assigning_unbound_coordinator_focus():
    agents = " ".join((ROOT / "AGENTS.md").read_text().split())
    usage = " ".join((ROOT / "docs/how-marco-uses-switchstand.md").read_text().split())

    assert "sole unscoped navigation route" in agents
    assert (
        "It may identify current routes or candidate WorkIds, but it never supplies assignment, "
        "focus, ownership, or effect authority"
    ) in agents
    assert "for navigation only" in usage
    assert "it cannot assign work, select focus, transfer ownership, or grant an effect" in usage
    assert (
        "navigation without that trusted binding remains `COVERAGE_GAP / UNKNOWN` rather than "
        "recovered focus"
    ) in usage
    assert "sole unscoped route" not in agents
    assert "and follow its current routes" not in usage


def test_human_guidance_is_routed_from_agents_and_owned_by_docs():
    agents = (ROOT / "AGENTS.md").read_text()
    interaction = " ".join((ROOT / "docs/human-interaction.md").read_text().split())
    human_input = (ROOT / "docs/human-input.md").read_text()
    human_review = (ROOT / "docs/human-review.md").read_text()

    assert "[human interaction](docs/human-interaction.md)" in agents
    assert "[Human Input](docs/human-input.md)" in agents
    assert "[Human Review](docs/human-review.md)" in agents
    assert "Human Review approved. Dispatch ready: <WorkId>." in agents

    interaction_rules = (
        "Root reports the portfolio",
        "Other agents report only their assigned work and explicitly named children",
        "state in plain language the outcome, exact change, consequence, size, and recommendation",
        "<Work> — <state>",
        "Changed: <material change>",
        "Constraint: <gate/blocker/none>",
        "Next: <next observable checkpoint>",
        "Effect: none / not sent / applied / unknown",
        "ACTION REQUIRED — human action is required.",
        "HELP COULD UNBLOCK — help could materially accelerate recovery.",
        "INPUT VALUABLE — judgment is needed before a consequential choice hardens.",
        "<LABEL> — <subject>",
        "Ask: <exact action/decision>",
        "Why now: <why it matters now>",
        "Blocked: <what cannot proceed, or none>",
        "Continuing: <what still proceeds>",
        "If answered: <what changes>",
        "Raise human attention immediately, outside the normal status cadence",
        "repeat it at a frequency proportionate to urgency while other work continues",
        "until the human acknowledges or defers it",
        "ownership is explicitly transferred with pickup",
        "Silence and unrelated progress do not clear or reset it",
        "stable milestone denominator and relevant calibration evidence",
        "otherwise report known milestones and uncertainty",
        "Major completion/activation notices are stricter",
        "until the human explicitly acknowledges them",
        "Silence, deferral, transfer, unrelated progress, or ordinary clearing of an attention condition does not acknowledge",
        "Wakeful may own timers and deduplicated delivery for these rules",
        "owns neither status truth nor authority",
        "creates no parallel work-management store",
    )
    assert [rule for rule in interaction_rules if rule not in interaction] == []
    assert "exact change, consequence, size, and recommendation" not in agents
    assert "Raise Human Input before the next material commitment would harden a choice" in human_input
    assert "every material implementation gets final live Human Review before dispatch" in human_review


def test_situation_review_v1_contract_is_discoverable_in_shared_guidance():
    agents = (ROOT / "AGENTS.md").read_text().lower()
    usage = (ROOT / "docs/how-marco-uses-switchstand.md").read_text().lower()
    required_agents = (
        "mechanically established required-currentness/applicable-invariant contradiction",
        "evidence of an ambiguous consequential effect",
        "requires one bounded situation review under the current review owner",
        "low confidence alone does not",
    )
    required_usage = (
        "exactly one independent bounded situation review",
        "mechanically established required-currentness/applicable-invariant contradiction",
        "evidence of an ambiguous consequential effect",
        "owner accepts an advisory warning",
        "named consequential decision, affected path, uncertainty, evidence and next falsifier",
        "exact workid/run/review/operation, expected observable or next-check",
        "last-progress evidence and owner",
        "exact attempts, outcomes, shared objective and recurring blocker",
        "exact current plan/status claim and revision plus contradictory evidence and currentness",
        "missing evidence is `unknown`/`insufficient_data`, not \"stalled\"",
        "deduplicate by exact situation identity plus evidence set",
        "`current`/`historical`/`unknown` truth labels",
        "causal challenge, smallest safe next action, affected path, remaining unknowns",
        "exact marco decision if any",
        "reviewer has no effect authority and cannot request another situation review",
        "owner acts, materially challenges with counterevidence, or pauses/escalates",
        "pause only the affected path",
        "live mitigation and unrelated work continue",
        "never trigger reviews recursively",
    )

    assert [rule for rule in required_agents if rule not in agents] == []
    assert [rule for rule in required_usage if rule not in usage] == []


def test_root_cause_analysis_contract_and_entry_links_are_discoverable():
    procedure = (ROOT / "docs/root-cause-analysis.md").read_text()
    required_contract = (
        "RCA-PROCEDURE-V1",
        "## Bounded triggers",
        "one sanitized RCA parent WorkId",
        "PRELIMINARY —",
        "UNKNOWN —",
        "Confidence applies only to a specific causal hypothesis",
        "supporting and contradicting evidence",
        "credible alternatives, and next falsifier",
        "Raw self-confidence cannot establish observed fact or effect truth",
        "low confidence alone is not a trigger",
        "proportional non-interfering evidence lanes",
        "Prior RCAs and recurrences",
        "failure mechanism",
        "credible alternatives and falsifiers",
        "counterfactual control",
        "Immediate stopgap",
        "Existing product requirement/test",
        "Genuinely new product",
        "Process/documentation",
        "CORRECTIONS_OWNED",
        "safe controlled replay",
        "next eligible natural matching event",
        "not an inactive wake or background daemon",
        "current routed product work, which owns current status and priority",
        "historical roadmap as evidence only",
        "active follow-up sweep at startup, on re-entry, at every bounded work-batch boundary",
        "before handoff, and when a correction due or unblock event occurs",
        "Documentation reachability tests prove only",
    )
    normalized = " ".join(procedure.lower().split())

    assert [
        rule
        for rule in required_contract
        if rule.lower() not in normalized
    ] == []

    entry_links = {
        "AGENTS.md": "docs/root-cause-analysis.md",
        "docs/operations-live-incident.md": "root-cause-analysis.md",
        "docs/operations-incident-record.md": "root-cause-analysis.md",
        "docs/how-marco-uses-switchstand.md": "root-cause-analysis.md",
    }
    assert {
        path: target
        for path, target in entry_links.items()
        if target not in (ROOT / path).read_text()
    } == {}

    agents = (ROOT / "AGENTS.md").read_text()
    working_with_marco = agents.split("## Working with Marco", 1)[1].split("\n## ", 1)[0]
    assert "[root-cause analysis procedure](docs/root-cause-analysis.md)" in working_with_marco
    assert "whether or not it arises from a live incident" in working_with_marco


def test_incident_status_separates_causal_confidence_from_deterministic_truth():
    runbook = (ROOT / "docs/operations-live-incident.md").read_text()
    record = (ROOT / "docs/operations-incident-record.md").read_text()
    runbook_normalized = " ".join(runbook.split())
    record_normalized = " ".join(record.split())

    for document in (runbook_normalized, record_normalized):
        assert "Causal hypothesis / confidence" in document
        assert "supporting and contradicting evidence" in document
        assert "credible alternatives" in document
        assert "falsifier" in document
        assert "Difficulty / prognosis" in document

    assert "Causal confidence is advisory inference only" in runbook_normalized
    assert "confidence cannot replace or change them" in runbook_normalized
    assert "Confidence describes only the named causal hypothesis" in record_normalized
    assert "raw self-confidence cannot change them" in record_normalized
    assert (
        "Difficulty / prognosis: <trivial|contained|architectural|unknown; confidence"
        not in record_normalized
    )
