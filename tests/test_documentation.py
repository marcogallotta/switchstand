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
        "review the idea proportionately for ambiguity, consequence, and conflict",
        "Keep every safely usable built-in Worker slot on the highest-priority executable product slices",
        "a product-gate review is product work",
        "Start a requested review immediately with a free Worker",
        "built-in slots are full and additional already-authorized independent slices remain",
        "Do not invent filler work",
        "Leave capacity idle only when no executable independent slice exists",
        "required Human Input or HOLD blocks every remaining slice",
        "applicable resource limit is reached; state the reason",
        "terminal result as an immediate coordination interrupt",
        "Slice mutations by writable surface and semantic concern",
        "suspend the later mutation, reconcile any effects, then re-slice or serialize it",
    )

    assert [rule for rule in required_rules if rule not in agents] == []


def test_root_cause_analysis_contract_and_entry_links_are_discoverable():
    procedure = (ROOT / "docs/root-cause-analysis.md").read_text()
    required_contract = (
        "RCA-PROCEDURE-V1",
        "## Bounded triggers",
        "one sanitized RCA parent WorkId",
        "PRELIMINARY —",
        "UNKNOWN —",
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
