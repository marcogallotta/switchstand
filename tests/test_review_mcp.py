from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from chatgpt_fixture import ACTIVE, PRINCIPAL, service

from switchstand.agent_mailboxes import AgentMailbox, AgentMailboxResult
from switchstand.chatgpt_mcp import build_chatgpt_server, build_ordinary_tools
from switchstand.reviews import (
    BaselineAssessment,
    BaselineCheck,
    ReviewFinding,
    ReviewRequest,
    ReviewResult,
    ReviewSubmit,
)


class Messages:
    engine = object()


class Mailboxes:
    def __init__(self, result: AgentMailboxResult):
        self.result = result
        self.seen: tuple[str, str] | None = None

    async def for_actor(self, principal_key: str, chat_session: str) -> AgentMailboxResult:
        self.seen = principal_key, chat_session
        return self.result


def mailbox() -> AgentMailbox:
    return AgentMailbox(
        name="Requester", name_key="requester", endpoint_id=uuid4(),
        principal_key=PRINCIPAL.key, session_key="session-key", generation=2,
    )


async def test_review_request_delegates_exact_baseline_check(monkeypatch):
    subject, bound = service(), mailbox()
    lookup = Mailboxes(AgentMailboxResult(status="ok", mailbox=bound))
    monkeypatch.setattr("switchstand.chatgpt_mcp.AgentMailboxState", lambda _engine: lookup)
    subject.messages, subject.reviews = Messages(), AsyncMock()  # type: ignore[assignment]
    work_id, review_id = uuid4(), uuid4()
    expected = ReviewResult(
        status="WAITING_REVIEWER", review_id=review_id, reason="reviewer_not_available",
    )
    subject.reviews.request.return_value = expected
    check = BaselineCheck(
        baseline_id=uuid4(), phase="PRE_IMPLEMENTATION",
        owner_disposition="BASELINE_PRESERVED", comparison="Plan preserves baseline",
        evidence_refs=("git:plan",),
    )
    tool = dict(build_ordinary_tools(subject, agent_identity=lambda: "chat"))["review_request"]
    assert await tool("1", work_id, "r1", "CODE", baseline_check=check) == expected
    subject.reviews.request.assert_awaited_once_with(ReviewRequest(
        subject_work_id=work_id, observed_revision="r1", review_kind="CODE",
        baseline_check=check,
    ), bound)


async def test_review_tools_are_default_off_and_closed(monkeypatch):
    subject = service()
    review_tools = {"review_request", "review_get", "review_recover", "review_submit"}
    assert not review_tools & set(dict(build_ordinary_tools(subject)))

    bound = mailbox()
    lookup = Mailboxes(AgentMailboxResult(status="ok", mailbox=bound))
    monkeypatch.setattr(
        "switchstand.chatgpt_mcp.AgentMailboxState", lambda _engine: lookup,
    )
    subject.messages = Messages()  # type: ignore[assignment]
    subject.reviews = AsyncMock()
    tools = await build_chatgpt_server(subject).list_tools()
    names = {tool.name for tool in tools}
    assert names & (review_tools | {"review_recover"}) == review_tools
    submit_schema = next(tool.input_schema for tool in tools if tool.name == "review_submit")
    assert {
        "api_version", "review_id", "verdict", "context_provenance", "findings",
        "evidence_refs", "baseline_assessment",
    } == set(submit_schema["properties"])
    assert not {
        "reviewer", "reviewer_endpoint_id", "principal", "grant_id", "generation",
    } & set(submit_schema["properties"])
    request_schema = next(tool.input_schema for tool in tools if tool.name == "review_request")
    assert "baseline_check" in request_schema["properties"]
    for name in ("review_get", "review_recover"):
        schema = next(tool.input_schema for tool in tools if tool.name == name)
        assert set(schema["properties"]) == {"api_version", "review_id"}


async def test_review_poll_and_recovery_derive_mailbox_and_distinct_received_guidance(
    monkeypatch,
):
    subject = service()
    bound = mailbox()
    lookup = Mailboxes(AgentMailboxResult(status="ok", mailbox=bound))
    monkeypatch.setattr("switchstand.chatgpt_mcp.AgentMailboxState", lambda _engine: lookup)
    subject.messages = Messages()  # type: ignore[assignment]
    subject.reviews = AsyncMock()
    review_id = uuid4()
    expected = ReviewResult(status="RECEIVED", review_id=review_id, delivery_id=uuid4())
    subject.reviews.get.return_value = expected
    subject.reviews.recover.return_value = expected
    audits: list[tuple[str, str | None, str]] = []
    tools = dict(build_ordinary_tools(
        subject, audit=lambda *record: audits.append(record),
        agent_identity=lambda: "chat-session",
    ))

    polled = await tools["review_get"]("1", review_id)
    recovered = await tools["review_recover"]("1", review_id)

    assert polled == expected
    assert "bounded active polling" in polled.next_action
    assert "review_get for this exact review_id" in polled.next_action
    assert (recovered.status, recovered.review_id, recovered.delivery_id) == (
        expected.status, expected.review_id, expected.delivery_id,
    )
    assert "Resume this exact recovered review" in recovered.next_action
    assert "review_submit with its evidence-based verdict" in recovered.next_action
    assert "do not call requester-only review_get" in recovered.next_action
    assert recovered.next_action != polled.next_action
    subject.reviews.get.assert_awaited_once_with(review_id, bound)
    subject.reviews.recover.assert_awaited_once_with(review_id, bound)
    assert [record[0] for record in audits] == ["review_get", "review_recover"]


async def test_observability_get_is_exact_read_only_delegation(monkeypatch):
    subject = service()
    occurrences = SimpleNamespace(engine=object())
    subject.reviews = SimpleNamespace(occurrences=occurrences)
    expected = {
        "schema": "switchstand.flow_report.v1",
        "review_pickup": {"status": "KNOWN"},
        "aggregate_learning": {
            "status": "UNKNOWN",
            "reason": "SUFFICIENTLY_COVERED_COMPLETED_TRACE_COHORT_UNAVAILABLE",
            "sample_count": None, "coverage_fraction": None,
            "cohort": {
                "runtime_generation": None, "schema_generation": None,
                "environment": None,
            },
            "metrics": {"lead_time_ms": None, "unobserved_share": None},
            "delivery_estimate": None,
        },
    }
    report = AsyncMock(return_value=expected)
    monkeypatch.setattr("switchstand.chatgpt_mcp.flow_report.report", report)
    targets = []
    tool = dict(build_ordinary_tools(subject, correlate_work=targets.append))["observability_get"]

    result = await tool(ACTIVE)

    assert result == expected
    report.assert_awaited_once_with(occurrences.engine, ACTIVE, occurrences)
    assert targets == [ACTIVE]
    server = build_chatgpt_server(subject)
    schema = next(
        item.input_schema for item in await server.list_tools()
        if item.name == "observability_get"
    )
    assert set(schema["properties"]) == {"work_id"}


async def test_review_submit_derives_current_reviewer_and_delegates_exact_verdict(monkeypatch):
    subject = service()
    bound = mailbox()
    lookup = Mailboxes(AgentMailboxResult(status="ok", mailbox=bound))
    monkeypatch.setattr(
        "switchstand.chatgpt_mcp.AgentMailboxState", lambda _engine: lookup,
    )
    subject.messages = Messages()  # type: ignore[assignment]
    subject.reviews = AsyncMock()
    review_id = uuid4()
    expected = ReviewResult(
        status="SUBMITTED", review_id=review_id, delivery_id=uuid4(),
    )
    subject.reviews.submit.return_value = expected
    targets: list[object] = []
    audits: list[tuple[str, str | None, str]] = []
    tool = dict(build_ordinary_tools(
        subject,
        audit=lambda *record: audits.append(record),
        agent_identity=lambda: "reviewer-chat",
        correlate_work=targets.append,
    ))["review_submit"]
    finding = ReviewFinding(
        finding_id="F1", defect="Defect", evidence="Evidence",
        consequence="Consequence", affected_claim="Claim",
        minimum_clearing_condition="Clear it",
    )
    assessment = BaselineAssessment(
        baseline_id=uuid4(), phase="PRE_IMPLEMENTATION",
        disposition="MATERIAL_DEVIATION", evidence_refs=("git:exact",),
    )

    result = await tool(
        "1", review_id, "FINDINGS", "INHERITED", (finding,), ("git:exact",), assessment,
    )

    assert result == expected
    assert "Poll review_get" not in result.next_action
    assert "review-only assignment" in result.next_action
    assert lookup.seen == (PRINCIPAL.key, "reviewer-chat")
    subject.reviews.submit.assert_awaited_once_with(ReviewSubmit(
        review_id=review_id, verdict="FINDINGS", findings=(finding,),
        evidence_refs=("git:exact",), context_provenance="INHERITED",
        baseline_assessment=assessment,
    ), bound)
    assert targets == [None]
    assert audits == [("review_submit", str(review_id), "SUBMITTED")]


async def test_review_submit_refuses_unbound_authenticated_reviewer(monkeypatch):
    subject = service()
    lookup = Mailboxes(AgentMailboxResult(
        status="denied", reason="agent_not_registered",
    ))
    monkeypatch.setattr(
        "switchstand.chatgpt_mcp.AgentMailboxState", lambda _engine: lookup,
    )
    subject.messages = Messages()  # type: ignore[assignment]
    subject.reviews = AsyncMock()
    audits: list[tuple[str, str | None, str]] = []
    tool = dict(build_ordinary_tools(
        subject,
        audit=lambda *record: audits.append(record),
        agent_identity=lambda: "unregistered-reviewer",
    ))["review_submit"]
    review_id = uuid4()

    result = await tool("1", review_id, "PASS", "UNSEEDED")

    assert (result.status, result.reason) == ("DENIED", "reviewer_binding_changed")
    subject.reviews.submit.assert_not_awaited()
    assert audits == [("review_submit", str(review_id), "DENIED")]


async def test_review_submit_preserves_mailbox_recovery_as_unknown(monkeypatch):
    subject = service()
    lookup = Mailboxes(AgentMailboxResult(
        status="recovery_required", reason="state_unavailable",
    ))
    monkeypatch.setattr(
        "switchstand.chatgpt_mcp.AgentMailboxState", lambda _engine: lookup,
    )
    subject.messages = Messages()  # type: ignore[assignment]
    subject.reviews = AsyncMock()
    audits: list[tuple[str, str | None, str]] = []
    tool = dict(build_ordinary_tools(
        subject,
        audit=lambda *record: audits.append(record),
        agent_identity=lambda: "recovery-reviewer",
    ))["review_submit"]
    review_id = uuid4()

    result = await tool("1", review_id, "BLOCKED", "UNKNOWN")

    assert (result.status, result.reason) == ("UNKNOWN", "state_unavailable")
    subject.reviews.submit.assert_not_awaited()
    assert lookup.seen == (PRINCIPAL.key, "recovery-reviewer")
    assert audits == [("review_submit", str(review_id), "UNKNOWN")]
