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
        "meaningful investigation, implementation, review, migration, activation, or operational task requires one exact WorkId",
        "live mitigation must not wait for WorkId creation or resolution",
        "acknowledge before nontrivial reasoning, tools, or waits",
        "explicit response-time budget",
        "smallest useful decision-bearing response within it",
        "clearly mark the uncertainty and continue the deeper work afterward",
        "empty acknowledgement or status-only reply does not satisfy the budget",
        "[human interaction](docs/human-interaction.md)",
        "item update only for a material semantic transition",
        "activity, elapsed time, or worker reassignment alone is not one",
        "portfolio snapshot when Marco asks, at a coordination handoff",
        "time is only a bounded silence watchdog",
        "Major completion/activation remains visible until the human explicitly acknowledges it",
        "review the idea proportionately for ambiguity, consequence, and conflict",
        "Worker or child agent must not invoke Marco's question widget or ask Marco directly",
        "question, supporting evidence, recommendation, actual blocking consequence",
        "safe option-preserving work that can continue",
        "continues that safe work while waiting",
        "only the parent/Coordinator may raise a Marco-facing question",
        "Root must use forked Workers by default",
        "safely independent substantive lanes already assigned, claimed, or in flight",
        "keeping Root focused on coordination and integration",
        "Never select or dispatch merely-ready unassigned substantive work",
        "use every safe slot, count product-gate review as product work",
        "`STOP` immediately pauses new dispatch and re-grounds",
        "terminal result as an immediate coordination interrupt",
        "Slice mutations by writable surface and semantic concern",
        "suspend the later mutation, reconcile any effects, then re-slice or serialize it",
    )

    assert [rule for rule in required_rules if rule not in agents] == []


def test_human_guidance_is_routed_from_agents_and_owned_by_docs():
    agents = (ROOT / "AGENTS.md").read_text()
    interaction = (ROOT / "docs/human-interaction.md").read_text()
    human_input = (ROOT / "docs/human-input.md").read_text()
    human_review = (ROOT / "docs/human-review.md").read_text()

    assert "[human interaction](docs/human-interaction.md)" in agents
    assert "[Human Input](docs/human-input.md)" in agents
    assert "[Human Review](docs/human-review.md)" in agents
    assert "Human Review approved. Dispatch ready: <WorkId>." in agents

    interaction_rules = (
        "Root reports the portfolio",
        "Other agents report only their assigned work and explicitly named children",
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
    assert "Raise Human Input before the next material commitment would harden a choice" in human_input
    assert "every material implementation gets final live Human Review before dispatch" in human_review


def test_situation_review_v1_contract_is_discoverable_in_shared_guidance():
    agents = (ROOT / "AGENTS.md").read_text().lower()
    usage = (ROOT / "docs/how-marco-uses-switchstand.md").read_text().lower()
    required_agents = (
        "raw confidence alone never triggers a situation review",
        "mechanically established contradiction in required currentness or an applicable invariant",
        "evidence that a consequential effect is ambiguous",
        "other conditions are advisory warnings which require owner acceptance before review",
        "explicit low confidence about a named consequential decision",
        "decision, affected path, specific uncertainty, supporting evidence and next falsifier",
        "exact workid/run/review/operation",
        "expected observable condition or next-check time",
        "evidence of last material progress and current owner",
        "exact attempts, outcomes, shared objective and recurring blocker",
        "exact current claim and revision plus the contradictory evidence and its currentness",
        "missing evidence is `unknown`/`insufficient_data`, never \"stalled\"",
        "deduplicate by exact situation identity plus evidence set",
        "reviewer has no effect authority",
        "may not request or trigger another situation review",
        "verdict, current truth labeled `current`, `historical` or `unknown`",
        "causal challenge, the smallest safe next action, the affected path",
        "remaining unknowns and the exact marco decision needed, if any",
        "owner takes an authorized action, records a material challenge with counterevidence",
        "pause only the affected consequential path",
        "live mitigation and unrelated authorized work continue",
        "never recursively triggers another situation review",
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
