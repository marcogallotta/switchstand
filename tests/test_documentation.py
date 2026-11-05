import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).parents[1]
REQUIRED_ENTRY_POINTS = (
    "AGENTS.md",
    "CLAUDE.md",
    "docs/architecture.md",
    "docs/code-quality.md",
    "docs/coordinator-tracker-contract.md",
    "docs/development.md",
    "docs/how-marco-uses-switchstand.md",
    "docs/human-input.md",
    "docs/human-interaction.md",
    "docs/human-review.md",
    "docs/north-star.md",
    "docs/research-sources.md",
    "docs/root-cause-analysis.md",
)
INLINE_DESTINATION = re.compile(r"!?\[[^]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))")


def read(path: str) -> str:
    return (ROOT / path).read_text()


def normalized(path: str) -> str:
    return " ".join(read(path).split())


def section(document: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^{re.escape(heading)}\s*$.*?(?=^#{{1,{heading.count('#')}}} |\Z)",
        document,
    )
    assert match, f"missing section {heading}"
    return match.group()


def markdown_links(document: str) -> set[str]:
    return {
        match.group(1) or match.group(2)
        for match in INLINE_DESTINATION.finditer(document)
    }


def broken_relative_destinations(root: Path, documents: list[Path]) -> list[str]:
    broken = []
    for document in documents:
        for destination in markdown_links(document.read_text()):
            parsed = urlsplit(destination)
            if parsed.scheme or parsed.netloc or destination.startswith("/") or not parsed.path:
                continue
            target = (document.parent / unquote(parsed.path)).resolve()
            if not target.is_relative_to(root) or not target.exists():
                broken.append(f"{document.relative_to(root)} -> {destination}")
    return broken


def test_repository_documentation_graph_is_resolvable():
    documents = [
        *(path for path in ROOT.glob("*.md") if path.name != "friction.md"),
        *(ROOT / "docs").rglob("*.md"),
    ]
    assert broken_relative_destinations(ROOT, documents) == []
    assert [path for path in REQUIRED_ENTRY_POINTS if not (ROOT / path).is_file()] == []


def test_relative_link_check_rejects_escape_and_missing_targets(tmp_path):
    document = tmp_path / "guide.md"
    document.write_text("[escape](../outside.md)\n[missing](missing.md)\n")

    assert set(broken_relative_destinations(tmp_path, [document])) == {
        "guide.md -> ../outside.md",
        "guide.md -> missing.md",
    }


def test_bootstrap_has_a_small_stable_section_shape():
    agents = read("AGENTS.md")
    headings = re.findall(r"(?m)^## .+$", agents)

    assert headings == [
        "## First minute: identity, work and authority",
        "## Keep the solution small, and preserve human judgment",
        "## Effects, continuity and feedback",
        "## When something is live or unsafe",
        "## Client-specific execution — do not universalize",
        "## Routes: load when the trigger is reached",
    ]
    size = len(agents.encode())
    if size > 15_000:
        print(
            f"WARNING: AGENTS.md is {size} bytes; review bootstrap ownership before adding prose"
        )
    assert read("CLAUDE.md").splitlines().count("@AGENTS.md") == 1


def test_tracker_recovery_is_bounded_classified_and_non_authoritative():
    recovery = section(
        read("docs/coordinator-tracker-contract.md"),
        "## Recover incomplete coverage without adopting history",
    )
    classifications = {
        "PROPOSED_OWNED_TASK", "REVIEW_OCCURRENCE", "DEPENDENCY_REFERENCE",
        "EVIDENCE_CONTEXT", "INSPECTED_NOT_OWNED", "SUPERSEDED_HISTORY",
        "MISSING_ID", "UNCERTAIN",
    }
    compact = " ".join(recovery.split())
    assert classifications <= set(re.findall(r"`([A-Z_]+)`", recovery))
    assert recovery.index("current attributable direction") < recovery.index("bounded history")
    assert "only while coverage remains incomplete" in compact
    assert "never assign work or transfer ownership" in compact
    assert "Adoption is proposal-first" in recovery
    assert "until Marco directly confirms that exact task" in compact
    assert "count for every recovery classification including zero" in compact
    assert "not a workspace-wide inventory" in compact


def test_tracker_decomposition_requires_outcomes_and_control_benefit():
    contract = " ".join(section(
        read("docs/coordinator-tracker-contract.md"),
        "## Split outcomes only when control improves",
    ).split())
    assert "at least two distinct outcomes" in contract
    assert "different owner, blocker or dependency, review path, priority" in contract
    assert "ability to make independent progress" in contract
    assert "100% of the original outcome" in contract
    assert "Preserve every existing WorkId" in contract
    assert "Do not split technical steps, tightly coupled work" in contract
    assert "numeric size, count, age, or line threshold" in contract
    assert "scheduler, ownership registry, or automatic splitter" in contract


def test_role_router_has_exact_function_routes_and_owner_links():
    agents = read("AGENTS.md")
    router = section(agents, "## Routes: load when the trigger is reached")
    routes = {
        label: body
        for label, body in re.findall(r"(?m)^\| ([^|]+) \| ([^|]+) \|$", router)
        if label not in {"Trigger", "---"}
    }

    assert set(routes) == {
        "Bootstrap, identity, task creation, activation/topology, host mechanics",
        "Research, design, spec, code, testing, independent review or landing",
        "Human decision, Human Review, explanation or on-demand status",
        "Work state, messages, history, uncertain effects, feedback",
        "Root or delegated coordination, supervision, handoff",
        "Live incident and consequential RCA",
        "Repository authoring, execution, CI, qualification, activation",
    }
    assert markdown_links(routes["Root or delegated coordination, supervision, handoff"]) == {
        "docs/coordinator-tracker-contract.md",
    }
    assert "docs/human-interaction.md" in markdown_links(router)
    assert {"docs/code-quality.md", "docs/research-sources.md"} <= markdown_links(
        routes["Research, design, spec, code, testing, independent review or landing"]
    )
    assert "docs/code-quality.md" in markdown_links(
        routes["Repository authoring, execution, CI, qualification, activation"]
    )
    assert "Research also follows [research-sources.md](docs/research-sources.md)." in agents


def test_role_contract_preserves_boundaries_without_execution_detail():
    agents = read("AGENTS.md")
    roles = section(agents, "## Client-specific execution — do not universalize")
    tracker = normalized("docs/coordinator-tracker-contract.md")

    assert "**Codex roles today:** Coordinator and Worker." in roles
    assert "A Worker owns its bounded assignment and does not self-expand" in roles
    assert "**Codex Coordinator only:** within its assigned coordination scope" in roles
    assert "A Worker executes its own bounded assignment directly" in roles
    assert "Root owns overall integration" in roles
    assert "delegated Coordinators own only their assigned lane" in roles
    assert "Role, tool, login, canary, review, document or mailbox registration grants nothing" in agents
    assert "Independence depends on actual participation, not role label" in roles
    assert "one writer per shared surface" in roles
    assert "Root owns only its assigned portfolio" in tracker
    assert "Root owns tracking, steering, result absorption, integration" in tracker
    assert len(roles.encode()) <= 4_000
    assert "Target advancement alone does not require rebasing" not in roles
    assert "Activating any client-visible ChatGPT MCP schema" not in roles


def test_code_quality_owns_implementation_and_review_execution_guidance():
    agents = read("AGENTS.md")
    quality = read("docs/code-quality.md")

    assert {
        "## Author / implementer",
        "## Test quality and qualification",
        "## Code Review",
        "## Findings, remedies and challenge",
        "## Refresh check",
    }.issubset(set(re.findall(r"(?m)^## .+$", quality)))
    assert "movement alone does not require rebasing" in quality
    assert "refresh on entry/re-entry/context replacement" in quality
    assert "before verdict/handoff" in quality
    assert "before verdict/handoff" not in agents


def test_process_owners_preserve_lean_delivery_challenge_and_activation_fences():
    agents = normalized("AGENTS.md")
    quality = normalized("docs/code-quality.md")
    human_input = normalized("docs/human-input.md")
    human_review = normalized("docs/human-review.md")

    assert "smallest safe vertical end-to-end stage" in quality
    assert "exclude speculative later-stage machinery" in quality
    assert "ACCEPT_DEFECT_REJECT_REMEDY" in quality
    assert "exact disputed blocker" in quality
    assert "There is no automatic third reviewer" in quality
    assert "run in parallel unless a genuine dependency makes them serial" in quality
    assert "only for an actual integration/composition claim" in quality
    assert "Marco's current direct assignment" in agents
    assert "or a `CURRENT` grant controls scope and effects" in agents
    assert "intended operating scale" in human_input
    assert "deliberate V1 deferrals" in human_review
    assert "Never present default-off or inert readiness as ordinary-use product completion" in human_review
    assert "Before asking Marco, establish whether a genuinely new material human-owned decision remains" in human_review
    assert "A roadmap or outcome agreement is not authorization for a new material solution" in human_review
    assert "A completed or deployed change is reported as status" in human_review
    assert "a compulsory four-line opening" in human_review
    assert "WHAT YOU ASKED —" not in human_review
    assert "CONFORMS_TO_APPROVED_BASELINE" in human_review
    assert "does not become a portfolio hold" in human_review
    assert "Do not ask for repeat approval of an unchanged already-approved solution" in agents
    assert "merely because implementation/landing is ready" in agents
    assert "remain distinct and require their own current authority and quality gates" in agents
    assert "an unchanged approved solution needs status, not another permission request" in human_review
    assert "Approved-baseline conformance" in quality


def test_aggregate_extension_and_per_pr_exception_have_distinct_authority():
    document = read("docs/code-quality.md")
    quality = " ".join(
        section(document, "### Canonical exception challenge").split()
    )
    extension = " ".join(section(document, "### Coordinator extension review").split())
    per_pr = " ".join(section(document, "### Exceptional LOC exemption gate").split())

    assert "do not quote, summarize, or rewrite it" in quality
    assert "without accepting candidate-created invariants as requirements" in quality
    assert "review agreement does not grant it" in quality
    assert "strictly below twice its corresponding ORIGINAL approved cap" in extension
    assert "At 2x or above in ANY breached dimension" in extension
    assert "original baseline, never the last extension" in extension
    assert "Challenge scope creep at any multiplier" in extension
    assert "An aggregate package above 500 lines is not itself a `>=500` PR exception" in per_pr
    assert "Agents split independently useful, reviewable PRs without asking Marco" in per_pr
    assert "`>=500` actual PR/diff remains on **HOLD** absent an exact Marco exception" in per_pr


def test_human_and_incident_triggers_remain_always_loaded():
    agents = read("AGENTS.md")
    triggers = section(agents, "## Keep the solution small, and preserve human judgment")
    incident = section(agents, "## When something is live or unsafe")
    usage = normalized("docs/how-marco-uses-switchstand.md")

    assert markdown_links(triggers) >= {"docs/human-input.md", "docs/human-review.md"}
    assert "before that choice hardens" in triggers
    assert "Do not ask for repeat approval of an unchanged already-approved solution" in triggers
    assert "docs/how-marco-uses-switchstand.md" in markdown_links(agents)
    assert "mechanically established required-currentness/applicable-invariant contradiction" in usage
    assert markdown_links(incident) >= {
        "docs/operations-live-incident.md",
        "docs/root-cause-analysis.md",
    }
    assert {"`CURRENT`", "`HISTORICAL`", "`UNKNOWN`"} <= set(re.findall(r"`[^`]+`", incident))


def test_authority_and_work_routing_keep_their_causal_fences():
    agents = read("AGENTS.md")
    authority = section(agents, "## First minute: identity, work and authority")
    usage = normalized("docs/how-marco-uses-switchstand.md")
    quality = normalized("docs/code-quality.md")

    assert "Role, tool, login, canary, review, document or mailbox registration grants nothing" in authority
    assert "Otherwise use the exact assigned WorkId" in authority
    assert "Never infer assignment from a readable record, role, mailbox, previous agent or search result" in authority
    assert "Do not create tasks for questions, status, corrections, approvals" in authority
    assert "docs/code-quality.md" in markdown_links(agents)
    assert "independent Code Review, CI, exact-head/composition, landing" in quality
    assert "## Explicit human fasttrack" in read("docs/code-quality.md")
    assert "work_type=\"Task\"" in usage
    assert 'owner_key=\"SELF\"' in usage
    assert "Use one stable OperationId" in usage
    assert "replay only the identical OperationId and arguments" in usage
    assert "it cannot assign work, select focus, transfer ownership, or grant an effect" in usage


def test_repository_safety_routes_detail_to_the_operational_owner():
    agents = read("AGENTS.md")
    safety = section(agents, "## When something is live or unsafe")
    usage = normalized("docs/how-marco-uses-switchstand.md")

    assert "Shared canonical `main` is read-only for edits" in safety
    assert "Use an authorized linked writer for source changes" in safety
    assert "do not reset/clean/switch primary `main`" in safety
    assert "docs/how-marco-uses-switchstand.md" in markdown_links(agents)
    assert "docs/development.md" in markdown_links(agents)
    assert "friction register" in usage
    assert "attempted claim, observed result, state-change truth, and smallest clearing action" in usage
    assert "`repository_bundle_get`, accepts only `current`" in usage
    assert "Durable worktrees, clones, evidence, and handoff artifacts live under" in usage
    assert "`~/.local/state/switchstand`" in usage
    assert "`~/.cache/switchstand`" in usage
    assert "do not use `/tmp` for durable work" in usage
    assert "Before any replacement write, reread the exact target" in usage
    assert "Stock Codex has no implicit linked writer, launch manifest, or launcher state" in usage
    assert "never synthesize missing launcher state" in usage
    assert "`~/.config/switchstand/.env` only when current authority permits" in usage
    assert "Auxiliary Coordinator bookkeeping failure degrades only the smallest affected capability" in usage
    assert "do not make Marco the routine repair operator" in usage
    assert "Codex and Claude sessions with a repository use Git" in usage
    assert "ordinary ChatGPT without a normal checkout uses `repository_bundle_get`" in usage


def test_stock_codex_mailbox_and_unbound_grounding_have_one_consistent_owner():
    agents = read("AGENTS.md")
    architecture = normalized("docs/architecture.md")
    usage = normalized("docs/how-marco-uses-switchstand.md")

    codex = section(agents, "## Client-specific execution — do not universalize")
    assert "docs/how-marco-uses-switchstand.md" in markdown_links(codex)
    assert "Coordinator mailbox identity and any takeover require Marco's separate current direction" in usage
    assert "Mailbox registration or Marco-confirmed `Codex Coordinator` takeover restores only the durable messaging address" in usage
    assert "Neither creates a writer, manifest, Wakeful supervisor, or launch-bound WorkId" in usage
    assert "The scripts bypass any `codex` command earlier on `PATH`" in usage
    assert "Resume the exact session through the same role entry" in usage
    assert "A repository hook re-injects the existing Feedback V1 obligation" in usage
    assert "Worker configuration registers no PostToolUse reminder or standing-continuity hooks" in architecture
    assert "codex-stop-mailbox --record-stop" in architecture
    assert "`/root` remains a temporary inbound alias" in usage
    assert "stops with `takeover_confirmation_required`" in usage
    assert "never retakes a lost name without a new direct instruction" in usage
    assert "checks pending mail at the start and end of each Marco turn" in usage
    assert "whenever Marco says `check mail`" in usage
    assert "A fresh `scripts/launch-coordinator` run is independent" in usage
    assert "Automatic transfer is disabled until a separate explicit addressed claim" in architecture
    assert "Bare `work_get` remains the launch-bound managed-worker behavior" in usage


def test_human_guidance_is_linked_not_duplicated_in_bootstrap():
    agents = read("AGENTS.md")
    interaction = read("docs/human-interaction.md")

    assert "docs/human-interaction.md" in markdown_links(agents)
    assert "docs/human-input.md" in markdown_links(agents)
    assert "docs/human-review.md" in markdown_links(agents)
    assert (
        "Before any status or plan update—or any portfolio, attention, completion, or activation message—follow "
        "[human interaction](docs/human-interaction.md)."
    ) in agents
    assert {"## Item update", "## Portfolio snapshot", "## Human attention"}.issubset(
        set(re.findall(r"(?m)^## .+$", interaction))
    )
    assert "exact change, consequence, size, and recommendation" not in agents


def test_performance_feedback_and_definitive_receipt_rules_stay_bounded():
    agents = normalized("AGENTS.md")
    interaction = normalized("docs/human-interaction.md")
    source = normalized("docs/source-history-feedback.md")

    assert "docs/source-history-feedback.md#feedback-capture-and-triage" in agents
    assert "before unrelated work" in source
    assert "Sample ordinary work that produced no report" in source
    assert "four `mcp__switchstand__file_*`" not in agents
    assert "a definitive semantic receipt normally suffices as readback" in agents
    assert "Stop once the requested postcondition is established" in interaction
    assert "No speculative second read is required" in agents
    assert "server's own readback remains mandatory" in source
    assert "different targets may be issued together" in source
    assert "same-target effects" in source


def test_chatgpt_connection_url_is_copy_safe():
    agents = read("AGENTS.md")
    usage = read("docs/how-marco-uses-switchstand.md")

    assert "docs/how-marco-uses-switchstand.md" in markdown_links(agents)
    assert "Give the new chat exactly one trusted WorkId separately" in usage
    assert "https://laptop.tail46f0b9.ts.net/switchstand/mcp" in usage
    assert "on its own line with no trailing punctuation" in usage


def test_polling_trigger_is_small_and_detail_lives_with_message_owners():
    agents = normalized("AGENTS.md")
    usage = normalized("docs/how-marco-uses-switchstand.md")
    source = normalized("docs/source-history-feedback.md")

    assert "`poll` means continue the exact live watch until terminal, user stop or real execution blocker" in agents
    assert "`poll` or `keep polling`" in source
    assert "Interactive polling." in usage
    assert "Empty reads and arbitrary check counts are nonterminal" in source
    assert "Scheduled task or condition watch is a different future-running product" in source


def test_root_delegation_detail_lives_in_tracker_owner():
    agents = read("AGENTS.md")
    tracker = normalized("docs/coordinator-tracker-contract.md")

    assert "docs/coordinator-tracker-contract.md" in markdown_links(agents)
    assert "All execution belongs in context-free native forks" in tracker
    assert "use safe available slots without filler" in tracker
    assert "serialize heavy shared writers" in tracker
    assert "The child may answer `ACCEPT_FIX`" in tracker
    assert "Disposition each Worker terminal result immediately" in tracker
    assert "lower-priority question never interrupts higher-priority executable work" in tracker
    assert "When Marco says `STOP`, pause new dispatch" in tracker


def test_material_design_review_and_root_agents_admission_live_in_code_quality():
    agents = read("AGENTS.md")
    quality = normalized("docs/code-quality.md")
    router = section(agents, "## Routes: load when the trigger is reached")

    assert "docs/code-quality.md" in markdown_links(router)
    assert "Root `AGENTS.md` is a bootstrap/router, not a process manual" in quality
    assert "essentially every fresh agent needs it before role/function selection" in quality
    assert "invariant, first-minute trigger, or routing pointer rather than procedure" in quality
    assert "Do not use a total byte/line cap as the semantic gate" in quality
    assert "Material design review before Human Review" in quality
    assert "one fresh context-free reviewer before Human Review" in quality


def test_always_loaded_bootstrap_anchors_remain_explicit():
    raw_agents = read("AGENTS.md")
    agents = normalized("AGENTS.md")
    triggers = section(raw_agents, "## Keep the solution small, and preserve human judgment")
    incident = " ".join(section(raw_agents, "## When something is live or unsafe").split())
    usage = normalized("docs/how-marco-uses-switchstand.md")

    assert "Default to the smallest useful V1" in triggers
    assert "new table, feature, tool, role, persistent state, cross-track mechanism or out-of-scope review suggestion" in triggers
    assert "**separate add-on** with estimated size and reason it is needed now" in triggers
    assert "Challenge the package before setting a size envelope" in triggers
    assert "Never make Marco accept or reject a large all-or-nothing bundle" in triggers
    assert "Marco's current direct assignment" in agents
    assert "verified relay from exact mailbox sender `claude-coordinator` within recipient Work" in agents
    assert "or a `CURRENT` grant controls scope and effects" in agents
    assert "challenges seriously destructive or irreversible action" in usage
    assert all(label in incident for label in ("`CURRENT`", "`HISTORICAL`", "`UNKNOWN`"))
    assert "prefer the smallest safe" in incident and "reversible mitigation before RCA" in incident
    assert "Do not delay mitigation to synchronize a stale repository" in incident
    assert "before that choice hardens" in triggers
    assert {"docs/human-input.md", "docs/human-review.md"} <= markdown_links(triggers)
    assert "docs/how-marco-uses-switchstand.md" in markdown_links(raw_agents)
    assert "mechanically established required-currentness/applicable-invariant contradiction" in usage
    assert "docs/agent-resource-admission.md" in markdown_links(raw_agents)
    assert "Shared canonical `main` is read-only" in agents


def test_research_and_rca_procedures_have_discoverable_entry_contracts():
    research = read("docs/research-sources.md")
    rca = read("docs/root-cause-analysis.md")

    assert "Search broad and short first, then narrow on the strongest leads" in research
    assert "request a deeper search by Claude Code" not in research
    assert {
        "## Bounded triggers",
        "## Investigation method",
        "## Corrections and product ownership",
        "## Validation and closure",
    }.issubset(set(re.findall(r"(?m)^## .+$", rca)))
    assert "RCA-PROCEDURE-V1" in rca


def test_incident_records_separate_evidence_from_causal_confidence():
    runbook = normalized("docs/operations-live-incident.md")
    record = normalized("docs/operations-incident-record.md")

    for document in (runbook, record):
        assert "Causal hypothesis / confidence" in document
        assert "supporting and contradicting evidence" in document
        assert "credible alternatives" in document
        assert "falsifier" in document
    assert "Causal confidence is advisory inference only" in runbook
    assert "raw self-confidence cannot change them" in record


def test_managed_history_is_exceptional_across_current_docs():
    development = normalized("docs/development.md")
    source_history = normalized("docs/source-history-feedback.md")

    assert "do not enumerate `work_history`" in development
    assert "explicit bounded investigation or recovery" in development
    assert "page bound `work_history`" not in development
    assert "History and exact-event reads are exceptional" in source_history
    assert "not for normal grounding" in source_history
    assert "routine polling" in source_history
