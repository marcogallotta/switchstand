from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from chatgpt_fixture import ACTIVE, PRINCIPAL, service

from switchstand.agent_mailboxes import AgentMailbox, AgentMailboxResult
from switchstand.chatgpt_mcp import build_chatgpt_server, build_ordinary_tools
from switchstand.reviews import ReviewFinding, ReviewRequest, ReviewResult, ReviewSubmit


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


async def test_review_request_is_default_off_and_has_no_caller_identity_fields(monkeypatch):
    subject = service()
    assert not {"review_request", "review_submit"} & set(dict(build_ordinary_tools(subject)))

    bound = mailbox()
    lookup = Mailboxes(AgentMailboxResult(status="ok", mailbox=bound))
    monkeypatch.setattr(
        "switchstand.chatgpt_mcp.AgentMailboxState", lambda _engine: lookup,
    )
    subject.messages = Messages()  # type: ignore[assignment]
    subject.reviews = AsyncMock()
    tools = await build_chatgpt_server(subject).list_tools()
    schema = next(tool.input_schema for tool in tools if tool.name == "review_request")

    assert {
        "api_version", "subject_work_id", "observed_revision", "review_kind",
        "candidate_ref", "mode", "prior_review_id", "finding_ids",
    } == set(schema["properties"])
    assert not {
        "requester", "requester_endpoint_id", "principal", "grant_id", "reviewer",
    } & set(schema["properties"])
    submit_schema = next(tool.input_schema for tool in tools if tool.name == "review_submit")
    assert {
        "api_version", "review_id", "verdict", "context_provenance", "findings",
        "evidence_refs",
    } == set(submit_schema["properties"])
    assert not {
        "reviewer", "reviewer_endpoint_id", "principal", "grant_id", "generation",
    } & set(submit_schema["properties"])


async def test_observability_get_is_exact_read_only_delegation(monkeypatch):
    subject = service()
    occurrences = SimpleNamespace(engine=object())
    subject.reviews = SimpleNamespace(occurrences=occurrences)
    expected = {
        "schema": "switchstand.flow_report.v1",
        "status": "PARTIAL",
        "work_id": str(ACTIVE),
        "captured_at": "2026-10-06T21:00:00+00:00",
        "review_pickup": {"status": "KNOWN"},
        "coverage": {"review_pickup": {"status": "KNOWN", "reason": None}},
    }
    report = AsyncMock(return_value=expected)
    monkeypatch.setattr("switchstand.chatgpt_mcp.flow_report.report", report)
    targets = []
    tool = dict(build_ordinary_tools(subject, correlate_work=targets.append))["observability_get"]

    result = await tool(ACTIVE)

    assert result.schema_name == "switchstand.flow_report.v1"
    assert result.status == "PARTIAL" and result.work_id == ACTIVE
    assert result.review_pickup.status == "KNOWN"
    assert result.model_extra == {
        "coverage": {"review_pickup": {"status": "KNOWN", "reason": None}},
    }
    report.assert_awaited_once_with(occurrences.engine, ACTIVE, occurrences)
    assert targets == [ACTIVE]
    server = build_chatgpt_server(subject)
    schema = next(
        item.input_schema for item in await server.list_tools()
        if item.name == "observability_get"
    )
    assert set(schema["properties"]) == {"work_id"}


async def test_review_request_derives_current_mailbox_and_delegates_exact_request(monkeypatch):
    subject = service()
    bound = mailbox()
    lookup = Mailboxes(AgentMailboxResult(status="ok", mailbox=bound))
    monkeypatch.setattr(
        "switchstand.chatgpt_mcp.AgentMailboxState", lambda _engine: lookup,
    )
    subject.messages = Messages()  # type: ignore[assignment]
    subject.reviews = AsyncMock()
    expected = ReviewResult(
        status="SENT", review_id=uuid4(), delivery_id=uuid4(),
    )
    subject.reviews.request.return_value = expected
    targets: list[object] = []
    audits: list[tuple[str, str | None, str]] = []
    tool = dict(build_ordinary_tools(
        subject,
        audit=lambda *record: audits.append(record),
        agent_identity=lambda: "chat-session",
        correlate_work=targets.append,
    ))["review_request"]
    prior = uuid4()

    result = await tool(
        "1", ACTIVE, "pg_exact", "CODE", "git:abc", "FOCUSED", prior, ("F1",),
    )

    assert result == expected
    assert lookup.seen == (PRINCIPAL.key, "chat-session")
    subject.reviews.request.assert_awaited_once_with(
        ReviewRequest(
            subject_work_id=ACTIVE, observed_revision="pg_exact", review_kind="CODE",
            candidate_ref="git:abc", mode="FOCUSED", prior_review_id=prior,
            finding_ids=("F1",),
        ),
        bound,
    )
    assert targets == [ACTIVE]
    assert audits == [("review_request", str(ACTIVE), "SENT")]


async def test_review_request_refuses_unbound_authenticated_caller(monkeypatch):
    subject = service()
    lookup = Mailboxes(AgentMailboxResult(
        status="denied", reason="agent_not_registered",
    ))
    monkeypatch.setattr(
        "switchstand.chatgpt_mcp.AgentMailboxState", lambda _engine: lookup,
    )
    subject.messages = Messages()  # type: ignore[assignment]
    subject.reviews = AsyncMock()
    tool = dict(build_ordinary_tools(
        subject, agent_identity=lambda: "unregistered-chat",
    ))["review_request"]

    result = await tool("1", ACTIVE, "pg_exact", "CODE")

    assert (result.status, result.reason) == ("DENIED", "requester_not_current")
    subject.reviews.request.assert_not_awaited()


async def test_review_request_preserves_mailbox_recovery_as_unknown(monkeypatch):
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
        agent_identity=lambda: "recovery-chat",
    ))["review_request"]

    result = await tool("1", ACTIVE, "pg_exact", "CODE")

    assert (result.status, result.reason) == ("UNKNOWN", "state_unavailable")
    subject.reviews.request.assert_not_awaited()
    assert lookup.seen == (PRINCIPAL.key, "recovery-chat")
    assert audits == [("review_request", str(ACTIVE), "UNKNOWN")]


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

    result = await tool(
        "1", review_id, "FINDINGS", "INHERITED", (finding,), ("git:exact",),
    )

    assert result == expected
    assert lookup.seen == (PRINCIPAL.key, "reviewer-chat")
    subject.reviews.submit.assert_awaited_once_with(ReviewSubmit(
        review_id=review_id, verdict="FINDINGS", findings=(finding,),
        evidence_refs=("git:exact",), context_provenance="INHERITED",
    ), bound)
    assert targets == [review_id]
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
