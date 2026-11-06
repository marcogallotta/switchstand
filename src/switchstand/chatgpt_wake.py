"""Opt-in local browser metadata observation and report-only logging."""

import ipaddress
import json
import logging
import secrets
from typing import Annotated, Literal
from uuid import UUID

from fastmcp import FastMCP
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool
from starlette.requests import Request
from starlette.responses import JSONResponse

from .agent_mailboxes import AgentMailboxState
from .chatgpt_alerts import AlertReport, HostReporter
from .messages import MessageState

PATH = "/internal/chatgpt-wake"
LOG = logging.getLogger(__name__)
OUTCOMES = frozenset({
    "busy", "draft-present", "stopped", "bound-tab-missing", "wrong-conversation",
    "tab-frozen", "tab-discarded", "composer-unavailable", "insert-failed",
    "inserted-not-submitted", "attempt-outcome-unknown", "source-unavailable",
    "mailbox-binding-changed", "idle", "click-attempted",
})


class _DiagnosticModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Facts(_DiagnosticModel):
    urlMatched: StrictBool | None = None
    editorFound: StrictBool | None = None
    busy: StrictBool | None = None
    draftPresent: StrictBool | None = None
    inserted: StrictBool | None = None
    controlSelection: Literal["missing-form", "no-explicit", "all-hidden", "ambiguous", "unique"] | None = None
    controlPresent: StrictBool | None = None
    controlEnabled: StrictBool | None = None
    clickDispatched: StrictBool | None = None


class _Attempt(_DiagnosticModel):
    id: UUID
    boot: UUID
    release: UUID
    endpoint: UUID
    generation: Annotated[int, Field(strict=True, ge=1)]
    delivery_ids: Annotated[list[UUID], Field(max_length=50)]
    started: AwareDatetime
    finished: AwareDatetime | None
    outcome: Annotated[str, Field(max_length=40)] | None
    reason: Literal["dispatch-start", "result", "dom-exception", "execution-failed",
                    "missing-result", "stopped-before-dispatch"]
    facts: _Facts
    exception: Literal["Error", "TypeError", "ReferenceError", "DOMException", "Other"] | None = None


class _Pair(_DiagnosticModel):
    id: UUID
    boot: UUID
    release: UUID
    at: AwareDatetime
    result: Literal["accepted", "refused"]
    reason: Literal["accepted", "invalid-url", "invalid-tab", "tab-unavailable", "tab-moved",
                    "mailbox-unavailable", "identity-changed", "conversation-already-bound",
                    "unresolved-attempt", "generation-changed", "stop-requested"]
    attempt: UUID | None


class _CurrentControl(_DiagnosticModel):
    release: UUID
    boot: UUID
    reason: Literal["missing-form", "no-explicit", "all-hidden", "ambiguous", "unique",
                    "url-mismatch", "editor-unavailable", "unavailable"]


class _Diagnostics(_DiagnosticModel):
    release: UUID
    boot: UUID
    current_control: _CurrentControl | None = None
    legacy_halt: StrictBool
    block: Literal["retry-evidence-not-current"] | None = None
    report_failure: Literal["report-unavailable"] | None = None
    attempt: _Attempt | None
    pair: _Pair | None


class _WakeAttempt(_DiagnosticModel):
    id: UUID
    action: Literal["prepare", "wait", "send"]
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    detail: Literal["url-mismatch", "editor-missing", "form-missing", "editor-disabled",
                    "attachment-present", "unrelated-draft", "stop-requested", "busy", "insert-failed",
                    "prompt-inserted", "send-control-missing", "send-control-ambiguous",
                    "send-control-disabled", "send-ready", "click-dispatched"] | None = None
    outcome: Literal["busy", "waiting", "ready", "draft-present", "wrong-conversation",
                     "composer-unavailable", "insert-failed", "click-attempted",
                     "unknown-outcome", "execution-failed"] | None = None


class _Observation(_DiagnosticModel):
    observed_at: AwareDatetime
    endpoint: UUID
    generation: Annotated[int, Field(strict=True, ge=1)]
    pending_count: Annotated[int, Field(strict=True, ge=0)] | None
    reason: Literal["idle", "bound-tab-missing", "tab-suspended", "uncertain-wake",
                    "manual-reconciliation", "already-nudged", "busy", "waiting", "ready",
                    "draft-present", "wrong-conversation", "composer-unavailable", "insert-failed",
                    "click-attempted", "source-unavailable", "mailbox-binding-changed",
                    "execution-failed", "unknown-outcome"]
    uncertain: StrictBool
    attempt: _WakeAttempt | None


class _Report(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(strict=True, min_length=1, max_length=80)]
    delivery_ids: Annotated[list[UUID], Field(max_length=50)]
    outcome: str
    incident: AlertReport | None = None
    diagnostics: _Diagnostics | None = None
    observation: _Observation | None = None


def _local(request: Request) -> bool:
    try:
        return (request.client is not None
                and ipaddress.ip_address(request.client.host).is_loopback
                and ipaddress.ip_address(request.url.hostname or "").is_loopback)
    except ValueError:
        return False


def install_browser_wake(
    server: FastMCP, messages: MessageState | None, reporter: HostReporter | None = None,
) -> None:
    """Default report-only; an explicitly injected host reporter owns admitted alert dispatch."""
    report_token = secrets.token_urlsafe(32)

    def reject(route: str, reason: str, status: int) -> JSONResponse:
        LOG.warning("browser_wake rejected route=%s reason=%s http_status=%s", route, reason, status)
        return JSONResponse({"status": reason}, status_code=status)

    @server.custom_route(PATH + "/config", methods=["GET"])
    async def config(request: Request) -> JSONResponse:
        if not _local(request):
            return reject("config", "denied", 403)
        return JSONResponse({"status": "ok", "report_token": report_token,
                             "escalation": "configured" if reporter else "unconfigured",
                             "recipient": reporter.recipient if reporter else None}, headers={"Cache-Control": "no-store"})

    @server.custom_route(PATH + "/poll", methods=["GET"])
    async def poll(request: Request) -> JSONResponse:
        if not _local(request):
            return reject("poll", "denied", 403)
        name = request.query_params.get("name", "").strip()
        if not name or len(name) > 80 or set(request.query_params) != {"name"}:
            return reject("poll", "invalid_name", 400)
        if messages is None:
            return reject("poll", "unavailable", 503)
        try:
            state = AgentMailboxState(messages.engine)
            resolved = await state.by_name(name)
            binding = resolved.mailbox
            if resolved.status != "ok" or binding is None:
                LOG.warning("browser_wake poll binding unavailable receipt=%s",
                            json.dumps({"name": name, "status": resolved.status, "reason": resolved.reason}))
                return JSONResponse({"status": resolved.status, "reason": resolved.reason},
                                    status_code=403)
            ids = await messages.all_pending_delivery_ids(binding, observation_only=True)
            if ids is None or (await state.by_name(name)).mailbox != binding:
                return reject("poll", "stale_or_unavailable", 503)
            return JSONResponse({"status": "ok", "name": binding.name,
                                 "endpoint": str(binding.endpoint_id),
                                 "generation": binding.generation,
                                 "delivery_ids": [str(identity) for identity in ids]},
                                headers={"Cache-Control": "no-store"})
        except Exception:  # Unavailable observation must not look empty.
            LOG.exception("browser_wake poll unavailable")
            return reject("poll", "unavailable", 503)

    @server.custom_route(PATH + "/report", methods=["POST"])
    async def report(request: Request) -> JSONResponse:
        if (not _local(request) or not secrets.compare_digest(
                request.headers.get("Authorization", ""), "Bearer " + report_token)):
            return reject("report", "denied", 403)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            return reject("report", "json_required", 415)
        body = await request.body()
        if len(body) > 4096:
            return reject("report", "too_large", 413)
        try:
            payload = _Report.model_validate_json(body)
            name, outcome = payload.name, payload.outcome
            if any(ord(char) < 32 for char in name) or outcome not in OUTCOMES:
                raise ValueError("invalid report")
            references = [str(identity) for identity in payload.delivery_ids]
            if payload.observation is not None and (payload.incident is not None or references
                                                    or payload.diagnostics is not None):
                raise ValueError("observation must be report-only")
            if payload.incident is not None and payload.incident.name != name:
                raise ValueError("incident name mismatch")
            if payload.diagnostics is not None:
                if payload.incident is not None or references:
                    raise ValueError("diagnostics cannot carry incident or mail references")
                attempt = payload.diagnostics.attempt
                if attempt and attempt.outcome is not None and attempt.outcome not in OUTCOMES:
                    raise ValueError("invalid diagnostic outcome")
        except (ValueError, TypeError, KeyError):
            return reject("report", "invalid_report", 400)
        if payload.observation is not None:
            LOG.warning("browser_wake observation name=%s receipt=%s", name,
                        payload.observation.model_dump_json(exclude_none=True))
            return JSONResponse({"status": "logged"})
        if payload.diagnostics is not None:
            # Metadata only. Never invoke the alert reporter for a diagnostic receipt.
            LOG.warning("browser_wake diagnostic name=%s receipt=%s", name,
                        payload.diagnostics.model_dump_json(exclude_none=True))
            return JSONResponse({"status": "logged"})
        LOG.warning("browser_wake name=%s outcome=%s delivery_ids=%s escalation=%s",
                    name, outcome, references, "configured" if reporter else "unconfigured")
        if payload.incident is not None:
            LOG.warning("browser_wake incident receipt=%s", payload.incident.model_dump_json())
        if reporter is not None and payload.incident is not None:
            try:
                receipt = await reporter.send(payload.incident)
                LOG.warning("browser_wake alert incident_id=%s phase=%s result=%s",
                            payload.incident.incident_id, payload.incident.phase,
                            json.dumps({key: receipt.get(key) for key in ("status", "reason", "message_id")}))
                return JSONResponse({"status": "logged", "escalation": "configured",
                                     "recipient": reporter.manifest.recipient, "alert": receipt})
            except Exception:
                LOG.exception("browser_wake alert outcome unknown incident_id=%s phase=%s; retain same incident identity",
                              payload.incident.incident_id, payload.incident.phase)
                return JSONResponse({"status": "logged", "escalation": "unknown"}, status_code=503)
        return JSONResponse({"status": "logged",
                             "escalation": "configured" if reporter else "unconfigured"})

    # Route decorators register these local handlers with FastMCP.
    _ = config, poll, report
