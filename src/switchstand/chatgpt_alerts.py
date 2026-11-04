"""Default-off fixed host reporter. Issuance is a separate trusted control effect."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4, uuid5

from pydantic import AwareDatetime, Field, JsonValue

from .agent_mailboxes import AgentMailbox, AgentMailboxState
from .agent_messages import replay_agent_request, send_agent_request
from .contracts import ClosedModel, LaunchAuthority
from .grant_state import GrantState
from .grants import PrincipalContext, WorkGrant
from .messages import MessageState, MessageSubmitResult

WORK = UUID("662bd6ec-ed4e-5ee7-b02a-34311a42d6ca")
NAME = "ChatGPT Wake Reporter"
ISSUED_RECIPIENT = "ChatGPT Wake Setup 2026-11-04"
RECIPIENT = "browser extension"
RECIPIENT_ENDPOINT = UUID("f23abc36-d48d-46a4-aab3-01c1e608f469")
RECIPIENT_GENERATION = 1


class ReporterManifest(ClosedModel):
    version: Literal[1] = 1
    name: Literal["ChatGPT Wake Reporter"] = NAME
    recipient: Literal["ChatGPT Wake Setup 2026-11-04"] = ISSUED_RECIPIENT
    work_id: Literal["662bd6ec-ed4e-5ee7-b02a-34311a42d6ca"] = "662bd6ec-ed4e-5ee7-b02a-34311a42d6ca"
    service_id: UUID
    grant_id: UUID
    expires_at: AwareDatetime
    package: str = Field(pattern=r"^[0-9a-f]{40}$")

    @property
    def principal(self) -> PrincipalContext:
        return PrincipalContext(issuer="switchstand-host", client_id="chatgpt-wake-reporter",
            subject=f"chatgpt-wake-reporter:{self.service_id}", assurance="authenticated")

    @property
    def session(self) -> str:
        return f"service:chatgpt-wake-reporter:{self.service_id}"

    @property
    def grant(self) -> WorkGrant:
        return WorkGrant(id=self.grant_id, version=1, principal=self.principal,
            authority=LaunchAuthority(active_work_id=WORK), scope="launch",
            operations=frozenset({"message"}), issuer="switchstand-host",
            provenance=f"chatgpt-wake-reporter:{self.package}:{self.service_id}",
            expires_at=self.expires_at)


def plan(package: str) -> ReporterManifest:
    return ReporterManifest(service_id=uuid4(), grant_id=uuid4(), package=package,
        expires_at=datetime.now(UTC) + timedelta(days=7))


def read_manifest(path: Path, digest: str) -> ReporterManifest:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("HOLD: manifest hash mismatch")
    return ReporterManifest.model_validate_json(raw)


async def check_binding(manifest: ReporterManifest, state: AgentMailboxState) -> AgentMailbox:
    result = await state.for_actor(manifest.principal.key, manifest.session)
    binding = result.mailbox
    named = (await state.by_name(NAME)).mailbox
    if (binding is None or binding != named or binding.name != NAME or binding.generation != 1):
        raise ValueError("HOLD: reporter binding missing or changed")
    return binding


async def bootstrap(manifest: ReporterManifest, grants: GrantState, *, issue: bool) -> AgentMailbox:
    """Trusted issuer only. Same manifest completes partial issuance, never rotates."""
    state = AgentMailboxState(grants.engine)
    named = await state.by_name(NAME)
    if named.status == "recovery_required":
        raise ValueError("HOLD: registry unavailable")
    if named.mailbox is not None and (
        named.mailbox.principal_key != manifest.principal.key
        or (await state.for_actor(manifest.principal.key, manifest.session)).mailbox != named.mailbox
    ):
        raise ValueError("HOLD: reporter name collision; no takeover")
    expected = manifest.grant
    if not expected.current():
        raise ValueError("HOLD: manifest expired")
    current = await grants.current(manifest.principal.key)
    if current is None and issue:
        await grants.issue(expected, expected_version=None)
    elif current != expected:
        raise ValueError("HOLD: grant absent or differs from exact manifest; no reissue")
    if issue:
        result = await state.register_agent(NAME, manifest.principal.key, manifest.session)
        if result.status != "ok":
            raise ValueError("HOLD: grant issued, registration incomplete; replay exact manifest")
    return await check_binding(manifest, state)


async def revoke(manifest: ReporterManifest, grants: GrantState) -> None:
    expected = manifest.grant
    revoked = expected.model_copy(update={"version": 2, "state": "revoked"})
    current = await grants.current(manifest.principal.key)
    if current == revoked:
        return
    if current != expected:
        raise ValueError("HOLD: grant changed; no revocation of another revision")
    await grants.issue(revoked, expected_version=1)


class AlertReport(ClosedModel):
    incident_id: UUID
    phase: Literal["blocked", "recovered"]
    name: str = Field(min_length=1, max_length=80)
    endpoint: UUID
    generation: int = Field(ge=1, strict=True)
    delivery_ids: list[UUID] = Field(min_length=1, max_length=50)
    reason: Literal["draft-present", "bound-tab-missing"]
    started_at: AwareDatetime
    observed_at: AwareDatetime

    def payload(self, phase: str | None = None) -> JsonValue:
        # Fingerprint ALL frozen evidence while keeping ordinary mailbox payload under800 chars.
        document = self.model_dump(mode="json", exclude={"phase"})
        fingerprint = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
        return {"type": "chatgpt-wake-alert", "phase": phase or self.phase,
                "incident": str(self.incident_id), "mailbox": self.name,
                "reason": self.reason, "work_id": str(WORK),
                "pending_count": len(self.delivery_ids), "evidence_sha256": fingerprint}

    def message_id(self, phase: str | None = None) -> UUID:
        return uuid5(self.incident_id, phase or self.phase)


class ExpiredObservation(ClosedModel):
    """Definitive first-creation absence under the reporter admission lock."""
    status: Literal["denied"] = "denied"
    reason: Literal["observation_expired"] = "observation_expired"


class HostReporter:
    """No caller-selected actor, recipient, body or grant. Host injection is opt-in."""
    recipient = RECIPIENT

    def __init__(self, manifest: ReporterManifest, messages: MessageState):
        self.manifest, self.messages = manifest, messages
        self.grants = GrantState(messages.engine)
        self.mailboxes = AgentMailboxState(messages.engine)

    async def check_recipient(self) -> bool:
        """The renamed parent must retain its exact admitted endpoint/generation."""
        target = (await self.mailboxes.by_name(self.recipient)).mailbox
        return (target is not None and target.endpoint_id == RECIPIENT_ENDPOINT
                and target.generation == RECIPIENT_GENERATION)

    async def send(self, report: AlertReport) -> dict[str, JsonValue]:
        async with self.grants.locked(self.manifest.principal.key) as grant:
            if grant is None or grant != self.manifest.grant or not grant.current():
                return {"status": "unavailable", "reason": "reporter_grant_not_current"}
            try:
                sender = await check_binding(self.manifest, self.mailboxes)
            except ValueError:
                return {"status": "unavailable", "reason": "reporter_binding_changed"}
            payload = report.payload()
            # Replay first: no current evidence requirement can hide a committed effect.
            result = await replay_agent_request(self.messages, sender, report.message_id(), payload)
            if result is None:
                if report.phase == "recovered":
                    original = await replay_agent_request(self.messages, sender,
                        report.message_id("blocked"), report.payload("blocked"))
                    if original is None or original.status != "ok":
                        return {"status": "denied", "reason": "no_committed_incident"}
                else:
                    now = datetime.now(UTC)
                    age = (report.observed_at - report.started_at).total_seconds()
                    fresh = (now - report.observed_at).total_seconds()
                    if fresh > 90:
                        # Exact replay already proved absence while this grant lock serializes sends.
                        return ExpiredObservation().model_dump(mode="json")
                    if age < 0 or fresh < 0 or (report.reason == "draft-present" and age < 60):
                        return {"status": "denied", "reason": "unqualified_observation"}
                    target = (await self.mailboxes.by_name(report.name)).mailbox
                    if target is None or target.endpoint_id != report.endpoint or target.generation != report.generation:
                        return {"status": "stale", "reason": "observed_binding_changed"}
                    pending = await self.messages.all_pending_delivery_ids(target, observation_only=True)
                    if pending is None:
                        return {"status": "unavailable", "reason": "pending_observation_unavailable"}
                    if not set(report.delivery_ids).issubset(pending):
                        return {"status": "stale", "reason": "pending_evidence_changed"}
                    if (await self.mailboxes.by_name(report.name)).mailbox != target:
                        return {"status": "stale", "reason": "observed_binding_changed"}
                if not await self.check_recipient():
                    return {"status": "stale", "reason": "recipient_binding_changed"}
                result = await send_agent_request(self.mailboxes, self.messages, sender,
                    self.recipient, report.message_id(), payload,
                    expected_recipient=(RECIPIENT_ENDPOINT, RECIPIENT_GENERATION))
            if isinstance(result, MessageSubmitResult) and result.status == "ok" and result.message:
                return {"status": "queued", "message_id": str(result.message.message_id),
                        "delivery_id": str(result.message.delivery_id)}
            return {"status": result.status, "reason": result.reason}
