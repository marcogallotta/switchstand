"""Closed scalar and nested types shared by the worker protocol."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import PurePosixPath
import re
import unicodedata
from typing import Any, Mapping
import uuid


PROTOCOL = "worker-v2"
WORK_ID = re.compile(r"[A-Za-z0-9._:-]{8,80}\Z")
SHA1 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
LEASE_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
SUMMARY_CODE = re.compile(r"[a-z0-9_]{1,80}\Z")


class ProtocolError(RuntimeError):
    """A fixed, non-sensitive protocol failure."""

    def __init__(self, code: str, status: int = 0) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("invalid_request")
        result[key] = value
    return result


def strict_json(data: bytes, *, maximum: int) -> Any:
    if len(data) > maximum:
        raise ProtocolError("request_too_large", 413)
    try:
        text = data.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, ProtocolError) as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError("invalid_request") from None


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def exact(value: Mapping[str, Any], keys: set[str]) -> None:
    if set(value) != keys:
        raise ProtocolError("invalid_request")


def integer(value: Any, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 9_007_199_254_740_991:
        raise ProtocolError("invalid_request")
    return value


def uuid_value(value: Any) -> str:
    if not isinstance(value, str):
        raise ProtocolError("invalid_request")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise ProtocolError("invalid_request") from None
    return value


def string(value: Any, maximum: int, *, minimum: int = 0, printable: bool = False) -> str:
    if not isinstance(value, str) or not minimum <= len(value.encode("utf-8")) <= maximum:
        raise ProtocolError("invalid_request")
    if printable and any(ord(character) < 32 or ord(character) > 126 for character in value):
        raise ProtocolError("invalid_request")
    return value


def timestamp(value: Any) -> str:
    if not isinstance(value, str) or not TIMESTAMP.fullmatch(value):
        raise ProtocolError("invalid_request")
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        raise ProtocolError("invalid_request") from None
    return value


def prefix(value: Any) -> str:
    result = string(value, 240, minimum=1)
    pure = PurePosixPath(result)
    if (
        result != unicodedata.normalize("NFC", result)
        or pure.is_absolute()
        or "\\" in result
        or any(part in {"", ".", ".."} for part in result.split("/"))
        or any(ord(character) < 32 or ord(character) == 127 for character in result)
    ):
        raise ProtocolError("invalid_request")
    return result


def branch(value: Any) -> str:
    result = string(value, 100, minimum=1)
    forbidden = " ~^:?*[\\"
    if (
        result.startswith(("-", ".", "/", "refs/"))
        or result.endswith((".", "/"))
        or ".." in result
        or "@{" in result
        or "//" in result
        or any(character in forbidden or ord(character) < 32 or ord(character) == 127 for character in result)
        or any(part.startswith(".") or part.endswith(".lock") for part in result.split("/"))
    ):
        raise ProtocolError("invalid_request")
    return result


def operation_id(value: Any) -> str:
    if not isinstance(value, str) or not WORK_ID.fullmatch(value):
        raise ProtocolError("invalid_request")
    return value


@dataclass(frozen=True)
class Authority:
    work_id: str
    worker_id: str
    instance_id: str
    fence: int
    lease_token: str
    cancellation_version: int

    def fields(self) -> dict[str, Any]:
        return {"protocol": PROTOCOL, **self.__dict__}


@dataclass(frozen=True)
class Claim:
    authority: Authority
    work_type: str
    lease_expires_at: str
    admission_sha: str
    source_text: str
    acceptance: tuple[str, ...]
    repository: Mapping[str, Any]
    checkout_path: str
    prior_checkpoint: Mapping[str, Any] | None
    codex_thread_id: str | None
    accepted_candidate: Mapping[str, str] | None
    limits: Mapping[str, int]


def validate_authority(authority: Authority) -> None:
    operation_id(authority.work_id)
    uuid_value(authority.worker_id)
    uuid_value(authority.instance_id)
    integer(authority.fence, 1)
    integer(authority.cancellation_version)
    if not LEASE_TOKEN.fullmatch(authority.lease_token):
        raise ProtocolError("invalid_request")
