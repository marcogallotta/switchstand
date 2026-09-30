import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).parents[1]
INLINE_DESTINATION = re.compile(
    r"!?\[[^]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))"
)


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

    assert broken_relative_destinations(tmp_path, [document]) == [
        "guide.md -> missing.md#section"
    ]


def test_codex_role_model_and_shared_process_contract_are_explicit():
    agents = (ROOT / "AGENTS.md").read_text()
    usage = (ROOT / "docs/how-marco-uses-switchstand.md").read_text()

    assert "Codex has exactly two roles: **Coordinator** and **Worker**." in agents
    assert "- **Researcher:**" not in agents
    assert "- **Implementer:**" not in agents
    assert "- **Reviewer:**" not in agents
    assert "Human Input before hardening" in agents
    assert "the exact source work owner/requester owns the review outcome watch" in agents
    assert "Reviewer remedies are advisory" in agents
    assert "Durable continuity" in agents
    assert "Supervisory proportionality" in agents

    assert "Codex has two roles: Coordinator and Worker." in usage
    assert "Shared process semantics across hosts" in usage
    assert "Shared semantics do not imply identical host storage, tools, roles, or orchestration." in usage


def test_code_red_bootstrap_preserves_trigger_stop_and_recovery_contract():
    agents = (ROOT / "AGENTS.md").read_text()

    assert "A credible live-user failure enters incident mode" in agents
    assert "an explicit `CODE RED` also enters it" in agents
    assert "`STOP` means pause new action, listen, and re-ground" in agents
    assert "`CANCEL` or `STOP WORK` means terminate" in agents
    assert "test the shared ingress before resetting product-local OAuth" in agents
    assert "continue cleanup, RCA,\nmonitoring, and owned follow-ups without waiting" in agents


def test_code_red_runbook_keeps_one_record_and_checkable_closeout():
    runbook = (ROOT / "docs/operations-live-incident.md").read_text()
    record = (ROOT / "docs/operations-incident-record.md").read_text()

    assert "Open or reuse one private incident record" in runbook
    assert "Do **not** terminate workers" in runbook
    assert "## Shared-ingress recovery" in runbook
    assert "A user report such as `it worked`" in runbook
    assert "Do not wait for Marco to ask again" in runbook

    for required_field in (
        "Impact / understood scope",
        "Difficulty / prognosis",
        "Human / agent action",
        "Service posture",
        "Current action / next checkpoint",
        "Coordination changes",
        "Dependency boundary",
        "Residual truth",
    ):
        assert required_field in record

    assert "ROLL_FORWARD/REDEPLOY/RETIRE_EXPLICITLY" in record
    assert "product-local state was not reset without\n  causal evidence" in record
    assert "Recovery does not\ncreate a pause, a second incident, or a need for another prompt." in record


def test_code_red_current_pointer_has_atomic_private_status_first():
    runbook = (ROOT / "docs/operations-live-incident.md").read_text()

    status_publish = 'mv -f -- "$incident_status_tmp" "$incident_dir/status.md"'
    pointer_publish = 'mv -Tf "$incident_pointer" "$incident_root/current"'
    initialization = runbook[
        runbook.index('incident_status_tmp="$(mktemp'):runbook.index(pointer_publish)
    ]

    assert 'chmod 0600 "$incident_status_tmp"' in runbook
    assert 'test "$(stat -c \'%a\' "$incident_dir/status.md")" = 600' in runbook
    assert runbook.index(status_publish) < runbook.index(pointer_publish)
    for required_field in (
        "State",
        "Operator",
        "Updated",
        "Impact / understood scope",
        "Difficulty / prognosis",
        "Human / agent action",
        "Service posture",
        "Current action / next checkpoint",
        "Coordination changes",
        "Dependency boundary",
        "Evidence",
        "Residual truth",
    ):
        assert f"- {required_field}:" in initialization


def test_code_red_authenticated_boundary_proof_cannot_be_residualized():
    runbook = (ROOT / "docs/operations-live-incident.md").read_text()
    record = (ROOT / "docs/operations-incident-record.md").read_text()

    assert "authenticated affected-path recovery proof is not deferrable" in runbook
    assert "entire stated watch window must complete without recurrence before\n`CLOSED`" in runbook
    assert "This gate may not be `NOT_RUN`, `MISSING_CAPABILITY`, `UNKNOWN`,\nHuman Input, or owned residual work." in record
    assert "relevant restart/refresh boundary and the entire stated monitoring/watch window" in record
