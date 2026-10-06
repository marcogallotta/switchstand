"""Default-off browser confirmation shell for exact Human Review consequences."""

from __future__ import annotations

import base64
import binascii
import hmac
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
DECISIONS: tuple[HumanDecision, ...] = ("APPROVED", "WAIT", "HOLD", "NO_DISPATCH")


class HumanReviewWriter(Protocol):
    async def prepare(self, package_work_id: UUID, package_revision: str) -> HumanReviewResult: ...

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
        "<!doctype html><html><head><meta charset=utf-8><title>Human Review</title></head>"
        f"<body><main>{body}</main></body></html>",
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
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
    controls = ""
    if form and record.decision is None:
        buttons = "".join(
            f'<button name="decision" value="{decision}">{decision}</button>'
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
    return _page(
        f"<h1>Human Review: {escape(result.status)}</h1>"
        f"<p>Target: {escape(consequence.implementation_target)}</p>"
        f"<h2>Implementation scope</h2><ul>{details}</ul>"
        f"<h2>Excluded effects</h2><ul>{excluded}</ul>"
        f"<p>Approval effect: {consequence.approval_effect}</p>"
        f"<p>State: {record.state}</p>{controls}"
    )


def _authorized(request: Request, username: bytes, password_hash: bytes) -> bool:
    scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        supplied_user, password = base64.b64decode(encoded, validate=True).split(b":", 1)
    except (binascii.Error, ValueError):
        return False
    if len(password) > 72:
        return False
    valid_user = hmac.compare_digest(supplied_user, username)
    try:
        valid_password = bcrypt.checkpw(password, password_hash)
    except ValueError:
        return False
    return valid_user and valid_password


def create_human_review_shell(
    state: HumanReviewWriter,
    *,
    expected_origin: str,
    username: str,
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
    if not username or ":" in username or len(username) > 256:
        raise ValueError("username must be a bounded Basic-auth username")
    encoded_username = username.encode()
    encoded_hash = password_hash.encode()
    if len(encoded_hash) > 128 or not encoded_hash.startswith((b"$2a$", b"$2b$", b"$2y$")):
        raise ValueError("password_hash must be a bcrypt hash")
    try:
        bcrypt.checkpw(b"", encoded_hash)
    except ValueError as error:
        raise ValueError("password_hash must be a valid bcrypt hash") from error

    def authenticate(request: Request) -> Response | None:
        if _authorized(request, encoded_username, encoded_hash):
            return None
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Human Review"'})

    async def review(request: Request) -> Response:
        denied = authenticate(request)
        if denied is not None:
            return denied
        try:
            work_id = UUID(request.path_params["package_work_id"])
            revision = request.query_params["revision"]
            if not revision or len(revision) > 512:
                raise ValueError
        except (KeyError, ValueError):
            return Response(status_code=400)
        return _render_result(await state.prepare(work_id, revision), form=True)

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
        prepared = await state.prepare(work_id, revision)
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
        ]
    )


def create_persistent_human_review_shell(
    engine: AsyncEngine,
    *,
    expected_origin: str,
    username: str,
    password_hash: str,
) -> Starlette:
    """Build the local persistent shell; the caller owns the engine lifecycle."""
    return create_human_review_shell(
        HumanReviewState(engine, CanonicalWorkRepository(engine)),
        expected_origin=expected_origin,
        username=username,
        password_hash=password_hash,
    )
