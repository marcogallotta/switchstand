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
    documents = [*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md")]
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
        "## Authority and grounding",
        "## Working with Marco",
        "## Repository bootstrap and safety",
        "## Work, messages, and routing",
        "## Codex roles and shared engineering process",
    ]
    assert len(agents.encode()) <= 17_000
    assert read("CLAUDE.md").splitlines().count("@AGENTS.md") == 1


def test_role_router_has_exact_function_routes_and_owner_links():
    router = section(read("AGENTS.md"), "### Route only the current function")
    routes = {
        label: body
        for label, body in re.findall(r"(?m)^- \*\*(.+?):\*\* (.+)$", router)
    }

    assert set(routes) == {
        "Root",
        "Delegated Coordinator",
        "Research/design Worker",
        "Implementation Worker",
        "Review Worker / eligible Coordinator reviewer",
        "Integration/landing Coordinator",
        "Incident / activation / Human Input / Human Review",
    }
    assert markdown_links(routes["Root"]) == {
        "docs/coordinator-tracker-contract.md",
        "docs/human-interaction.md",
    }
    assert "docs/code-quality.md" in markdown_links(routes["Implementation Worker"])
    assert "docs/code-quality.md" in markdown_links(
        routes["Review Worker / eligible Coordinator reviewer"]
    )
    assert "docs/code-quality.md" in markdown_links(routes["Integration/landing Coordinator"])
    assert "docs/research-sources.md" in markdown_links(routes["Research/design Worker"])


def test_role_contract_preserves_boundaries_without_execution_detail():
    roles = section(read("AGENTS.md"), "## Codex roles and shared engineering process")
    before_router = roles.split("### Route only the current function", 1)[0]

    assert "exactly two roles: **Coordinator** and **Worker**" in before_router
    assert "Root owns the overall assigned portfolio/integration" in before_router
    assert "delegated Coordinator owns only its lane" in before_router
    assert "Role/function never grants authority" in before_router
    assert "Review independence depends on actual" in before_router
    assert "one mutation owner per writable surface" in before_router
    assert "one integration owner for shared surfaces" in before_router
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
    assert "Only current direct assignment or a `CURRENT` grant authorizes" in agents
    assert "intended operating scale" in human_input
    assert "deliberate V1 deferrals" in human_review
    assert "Never present default-off or inert readiness as ordinary-use product completion" in human_review


def test_human_and_incident_triggers_remain_always_loaded():
    agents = read("AGENTS.md")
    triggers = section(agents, "### Always-loaded triggers")
    incident = section(agents, "### Live incidents")

    assert markdown_links(triggers) >= {"docs/human-input.md", "docs/human-review.md"}
    assert "Every material implementation gets final live" in triggers
    assert "mechanically established required-currentness/applicable-invariant contradiction" in triggers
    assert markdown_links(incident) >= {
        "docs/operations-live-incident.md",
        "docs/root-cause-analysis.md",
    }
    assert "CURRENT` / `HISTORICAL` / `UNKNOWN" in incident


def test_authority_and_work_routing_keep_their_causal_fences():
    agents = read("AGENTS.md")
    authority = section(agents, "## Authority and grounding")
    routing = " ".join(section(agents, "## Work, messages, and routing").split())

    assert "Role, docs, placement, readability, login/tools and procedures never grant authority" in authority
    assert "ordinary unbound Coordinator never uses bare `work_get` to infer focus" in authority
    assert "mailbox registration or takeover does not supply work identity or authority" in authority
    assert "Landing requires current independent review and exact-head/composition gates" in authority
    assert "exact WorkId/message identity, owner/purpose, state, next check, and terminal condition" in routing
    assert "inactive sessions do not poll or wake" in routing
    assert "never supplies assignment, focus, ownership, or effect authority" in routing


def test_repository_safety_routes_detail_to_the_operational_owner():
    safety = section(read("AGENTS.md"), "## Repository bootstrap and safety")
    usage = normalized("docs/how-marco-uses-switchstand.md")

    assert "Shared canonical `main` is read-only except Git reads/fetches" in safety
    assert "Source edits, staging, commits, and worktree mutation stay in that writer" in safety
    assert "repo-local Git-ignored `friction.md`" in safety
    assert "attempted claim, observed result, state-change truth, and smallest clearing action" in usage
    assert "`repository_bundle_get`, accepts only `current`" in usage


def test_handoff_and_unbound_grounding_have_one_consistent_owner():
    agents = normalized("AGENTS.md")
    architecture = normalized("docs/architecture.md")
    usage = normalized("docs/how-marco-uses-switchstand.md")

    assert "plain raw `codex` launch always creates an independent session and never claims" in agents
    assert "Automatic transfer is disabled until a separate explicit addressed claim" in architecture
    assert "registration and a later plain launch are not pickup" in usage
    assert "missing trusted identity is `COVERAGE_GAP / UNKNOWN`" in architecture
    assert "Bare `work_get` remains the launch-bound managed-worker behavior" in usage


def test_human_guidance_is_linked_not_duplicated_in_bootstrap():
    agents = read("AGENTS.md")
    interaction = read("docs/human-interaction.md")

    assert "docs/human-interaction.md" in markdown_links(agents)
    assert "docs/human-input.md" in markdown_links(agents)
    assert "docs/human-review.md" in markdown_links(agents)
    assert {"## Item update", "## Portfolio snapshot", "## Human attention"}.issubset(
        set(re.findall(r"(?m)^## .+$", interaction))
    )
    assert "exact change, consequence, size, and recommendation" not in agents


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
