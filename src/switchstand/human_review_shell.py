"""Default-off browser confirmation shell for exact Human Review consequences."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from html import escape
from typing import Protocol
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import bcrypt
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from .canonical_work import CanonicalWorkRepository
from .human_reviews import HumanDecision, HumanReviewResult, HumanReviewState

MAX_FORM_BYTES = 8192
DECISIONS: tuple[HumanDecision, ...] = ("APPROVED", "NO_DISPATCH")
COOKIE = "__Host-human-review"
SESSION_SECONDS = 1800


class HumanReviewWriter(Protocol):
    async def prepare(
        self, package_work_id: UUID, package_revision: str, consequence_id: UUID | None = None,
    ) -> HumanReviewResult: ...

    async def submit(
        self,
        consequence_id: UUID,
        package_work_id: UUID,
        package_revision: str,
        decision: HumanDecision,
    ) -> HumanReviewResult: ...


def _field(values: dict[str, list[str]], name: str, *, limit: int) -> str:
    items = values.get(name, [])
    if len(items) != 1 or not items[0] or len(items[0]) > limit:
        raise ValueError(f"invalid {name}")
    return items[0]


def _page(body: str, *, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html><head><meta charset=utf-8>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Approval · Switchstand</title><style>"
        "body{margin:0;background:#f5f5f2;color:#202720;font:16px/1.6 system-ui}"
        "main{max-width:650px;margin:60px auto;padding:28px;background:white;"
        "border:1px solid #dedfd7;border-radius:13px}h1{line-height:1.2}"
        "button,input{font:inherit;padding:12px;border:1px solid #d7dcd0;border-radius:7px}"
        "button{cursor:pointer;margin:8px;background:#344e2e;color:white}"
        "button[value=NO_DISPATCH]{background:white;color:#5e433a}"
        "li,p{overflow-wrap:anywhere}</style></head>"
        f"<body><main>{body}</main></body></html>",
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                "form-action 'self'; frame-ancestors 'none'"
            ),
            # no-referrer makes browser form POSTs send Origin: null.
            "Referrer-Policy": "same-origin",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


def _render_result(result: HumanReviewResult, *, form: bool) -> HTMLResponse:
    if result.record is None:
        status = 409 if result.status in {"STALE", "CONFLICT"} else 503
        return _page(f"<h1>{escape(result.status)}</h1><p>{escape(result.reason or '')}</p>", status=status)
    record = result.record
    consequence = record.consequence
    details = "".join(f"<li>{escape(item)}</li>" for item in consequence.implementation_scope)
    excluded = "".join(f"<li>{escape(item)}</li>" for item in consequence.excluded_effects)
    baseline_content = ""
    opening = ""
    if consequence.candidate is not None:
        candidate = consequence.candidate
        opening = (
            f"<p>Approve exact code candidate for merge: PR #{candidate.pull_request}</p>"
            f"<p>Head: {candidate.head_sha}<br>Base: {candidate.base_sha}<br>"
            f"Composition: {candidate.composition_sha}</p>"
            "<p>The approved Task is unchanged. This does not authorize activation.</p>"
        )
    if consequence.baseline is not None:
        baseline = consequence.baseline
        opening_values = baseline.opening
        opening = (
            f"<p>WHAT YOU ASKED — {escape(opening_values.what_you_asked)}</p>"
            f"<p>WHAT THIS DOES — {escape(opening_values.what_this_does)}</p>"
            "<p>WHAT THIS DOES NOT DO / WHAT CHANGES FOR YOU — "
            f"{escape(opening_values.what_this_does_not_or_changes_for_you)}</p>"
            f"<p>SIZE — {escape(opening_values.size)}</p>"
        )

        def listed(values: tuple[str, ...]) -> str:
            return "".join(f"<li>{escape(value)}</li>" for value in values)

        context = baseline.decision_context
        baseline_content = (
            f"<h2>Human sources</h2><ul>{listed(baseline.human_source_refs)}</ul>"
            f"<h2>Approved meaning</h2><ul>{listed(baseline.approved_meaning)}</ul>"
            f"<h2>Protected requirements</h2><ul>{listed(baseline.protected_requirements)}</ul>"
            f"<h2>Approved non-goals</h2><ul>{listed(baseline.approved_non_goals)}</ul>"
            f"<h2>Size or burden</h2><ul>{listed(context.size_or_burden)}</ul>"
            f"<h2>Scale assumptions</h2><ul>{listed(context.scale_assumptions)}</ul>"
            "<h2>Material risks and boundaries</h2>"
            f"<ul>{listed(context.material_risks_and_boundaries)}</ul>"
            f"<h2>Continuation effects</h2><ul>{listed(baseline.continuation_effects)}</ul>"
        )
    controls = ""
    if form and record.decision is None:
        buttons = "".join(
            f'<button name="decision" value="{decision}">'
            f'{"Approve" if decision == "APPROVED" else "Reject"}</button>'
            for decision in DECISIONS
        )
        controls = (
            '<form method="post" action="/human-review/submit">'
            f'<input type="hidden" name="consequence_id" value="{record.consequence_id}">'
            f'<input type="hidden" name="package_work_id" value="{record.package_work_id}">'
            f'<input type="hidden" name="package_revision" value="{escape(record.package_revision)}">'
            f'<input type="hidden" name="consequence_digest" value="{record.consequence_digest}">'
            f"{buttons}</form>"
        )
    requester = consequence.requester
    identity = ("" if requester is None else
        f"<p>Requested by <strong>{escape(requester.name)}</strong> · "
        f"{requester.mailbox_id}</p>")
    saved = "" if record.decision is None else f"<h2>Saved: {escape(record.decision)}</h2>"
    return _page(
        f"{identity}{saved}{opening}<h1>Human Review: {escape(result.status)}</h1>"
        f"{baseline_content}"
        f"<p>Target: {escape(consequence.implementation_target)}</p>"
        f"<h2>Implementation scope</h2><ul>{details}</ul>"
        f"<h2>Excluded effects</h2><ul>{excluded}</ul>"
        f"<p>Approval effect: {escape(consequence.approval_effect)}</p>"
        f"<p>State: {record.state}</p>{controls}"
    )


async def _read_form(request: Request) -> dict[str, list[str]]:
    if request.headers.get("content-type", "").split(";", 1)[0] != (
        "application/x-www-form-urlencoded"
    ):
        raise ValueError("invalid content type")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_FORM_BYTES:
            raise ValueError("form too large")
        body.extend(chunk)
    return parse_qs(bytes(body).decode("utf-8"), keep_blank_values=True, max_num_fields=5)


def create_human_review_shell(
    state: HumanReviewWriter,
    *,
    expected_origin: str,
    password_hash: str,
) -> Starlette:
    """Build an unregistered shell; the caller separately owns route installation."""
    origin = urlsplit(expected_origin)
    if (
        origin.scheme != "https"
        or not origin.netloc
        or origin.username is not None
        or origin.password is not None
        or origin.path not in {"", "/"}
        or origin.query
        or origin.fragment
        or expected_origin.endswith("/")
    ):
        raise ValueError("expected_origin must be an exact HTTPS origin without trailing slash")
    encoded_hash = password_hash.encode()
    if len(encoded_hash) > 128 or not encoded_hash.startswith((b"$2a$", b"$2b$", b"$2y$")):
        raise ValueError("password_hash must be a bcrypt hash")
    try:
        bcrypt.checkpw(b"", encoded_hash)
    except ValueError as error:
        raise ValueError("password_hash must be a valid bcrypt hash") from error

    session_key = secrets.token_bytes(32)

    def signature(expiry: str) -> str:
        return hmac.new(session_key, expiry.encode(), hashlib.sha256).hexdigest()

    def authenticate(request: Request) -> Response | None:
        expiry, _, supplied = request.cookies.get(COOKIE, "").partition(".")
        if (expiry.isdecimal() and len(expiry) <= 12 and int(expiry) > time.time()
                and hmac.compare_digest(signature(expiry).encode(), supplied.encode())):
            return None
        return _page(
            '<h1>Switchstand approval</h1><form method="post">'
            '<label>Password <input name="password" type="password" '
            'autocomplete="current-password" required maxlength="72"></label>'
            '<button>Unlock</button></form>', status=401,
        )

    async def login(request: Request) -> Response:
        if request.headers.get("origin") != expected_origin:
            return Response(status_code=403)
        try:
            password = _field(await _read_form(request), "password", limit=72).encode()
            valid = len(password) <= 72 and bcrypt.checkpw(password, encoded_hash)
        except (UnicodeDecodeError, ValueError):
            valid = False
        if not valid:
            return _page('<h1>Password not accepted</h1><a href="">Try again</a>', status=401)
        response = await review(request, unlocked=True)
        expiry = str(int(time.time()) + SESSION_SECONDS)
        response.set_cookie(COOKIE, f"{expiry}.{signature(expiry)}", max_age=SESSION_SECONDS,
                            secure=True, httponly=True, samesite="strict", path="/")
        return response

    async def review(request: Request, unlocked: bool = False) -> Response:
        denied = None if unlocked else authenticate(request)
        if denied is not None:
            return denied
        try:
            work_id = UUID(request.path_params["package_work_id"])
            revision = request.query_params["revision"]
            if not revision or len(revision) > 512:
                raise ValueError
            selected = request.query_params.get("consequence_id")
            consequence_id = UUID(selected) if selected is not None else None
        except (KeyError, ValueError):
            return Response(status_code=400)
        result = (await state.prepare(work_id, revision, consequence_id)
                  if consequence_id is not None else await state.prepare(work_id, revision))
        return _render_result(result, form=True)

    async def submit(request: Request) -> Response:
        denied = authenticate(request)
        if denied is not None:
            return denied
        if request.headers.get("origin") != expected_origin:
            return Response(status_code=403)
        content_length = request.headers.get("content-length")
        try:
            if content_length is not None:
                declared_length = int(content_length)
                if declared_length < 0:
                    return Response(status_code=400)
                if declared_length > MAX_FORM_BYTES:
                    return Response(status_code=413)
        except ValueError:
            return Response(status_code=400)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            return Response(status_code=415)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_FORM_BYTES:
                return Response(status_code=413)
            body.extend(chunk)
        try:
            values = parse_qs(bytes(body).decode("utf-8"), keep_blank_values=True, max_num_fields=5)
            consequence_id = UUID(_field(values, "consequence_id", limit=36))
            work_id = UUID(_field(values, "package_work_id", limit=36))
            revision = _field(values, "package_revision", limit=512)
            digest = _field(values, "consequence_digest", limit=64)
            decision_value = _field(values, "decision", limit=32)
            if decision_value not in DECISIONS:
                raise ValueError("invalid decision")
            decision: HumanDecision = decision_value
        except (UnicodeDecodeError, ValueError):
            return Response(status_code=400)
        prepared = await state.prepare(work_id, revision, consequence_id)
        if (
            prepared.record is None
            or prepared.record.consequence_id != consequence_id
            or not hmac.compare_digest(prepared.record.consequence_digest, digest)
        ):
            return _page("<h1>STALE</h1><p>The reviewed consequence changed.</p>", status=409)
        return _render_result(
            await state.submit(consequence_id, work_id, revision, decision), form=False
        )

    return Starlette(
        routes=[
            Route("/human-review/submit", submit, methods=["POST"]),
            Route("/human-review/{package_work_id}", review, methods=["GET"]),
            Route("/human-review/{package_work_id}", login, methods=["POST"]),
        ]
    )


def create_persistent_human_review_shell(
    engine: AsyncEngine,
    *,
    expected_origin: str,
    password_hash: str,
) -> Starlette:
    """Build the local persistent shell; the caller owns the engine lifecycle."""
    return create_human_review_shell(
        HumanReviewState(engine, CanonicalWorkRepository(engine)),
        expected_origin=expected_origin,
        password_hash=password_hash,
    )
