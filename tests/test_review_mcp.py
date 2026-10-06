from unittest.mock import AsyncMock
from uuid import uuid4

from chatgpt_fixture import ACTIVE, PRINCIPAL, service

from switchstand.agent_mailboxes import AgentMailbox, AgentMailboxResult
from switchstand.chatgpt_mcp import build_chatgpt_server, build_ordinary_tools
from switchstand.reviews import ReviewRequest, ReviewResult


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
    assert "review_request" not in dict(build_ordinary_tools(subject))

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
