"""Strict HTTP client for the frozen ``worker-v2`` coordinator contract."""

from __future__ import annotations

from typing import Any, Mapping, Protocol
import urllib.error
import urllib.parse
import urllib.request
from .schemas import (
    Authority,
    Claim,
    LEASE_TOKEN,
    PROTOCOL,
    ProtocolError,
    REPOSITORY,
    SHA1,
    SHA256,
    SUMMARY_CODE,
    WORK_ID,
    branch as _branch,
    canonical_json,
    exact as _exact,
    integer as _integer,
    operation_id as _operation_id,
    prefix as _prefix,
    strict_json,
    string as _string,
    timestamp as _timestamp,
    uuid_value as _uuid,
    validate_authority as _validate_authority,
)


CAPABILITIES = [
    "adopted_thread_resume_v1",
    "candidate_manifest_v1",
    "codex_exec_v1",
    "read_isolation_bwrap_v1",
]
SAFE_ERRORS = {
    "idempotency_conflict",
    "invalid_request",
    "not_found",
    "policy_denied",
    "publication_already_authorized",
    "request_too_large",
    "stale_head",
    "stale_or_invalid_lease",
    "temporary_failure",
    "terminal_immutable",
    "unauthorized",
    "work_type_forbidden",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


class CoordinatorPort(Protocol):
    def register(self, worker_id: str, instance_id: str) -> Mapping[str, Any]: ...

    def claim(self, worker_id: str, instance_id: str) -> Claim | None: ...

    def renew(self, authority: Authority) -> Mapping[str, Any]: ...

    def checkout(self, claim: Claim) -> tuple[bytes, Mapping[str, str]]: ...

    def checkpoint(
        self,
        authority: Authority,
        operation_id: str,
        sequence: int,
        phase: str,
        thread_id: str | None,
        state: str,
    ) -> Mapping[str, Any]: ...

    def submit_candidate(self, authority: Authority, manifest: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def complete(
        self,
        authority: Authority,
        operation_id: str,
        *,
        status: str,
        candidate_id: str | None = None,
        review_verdict: str | None = None,
        summary_code: str,
        checks: list[Mapping[str, str]] | None = None,
    ) -> Mapping[str, Any]: ...


class CoordinatorClient:
    """One workspace-scoped worker client; the bearer key never leaves this object."""

    def __init__(self, base_url: str, worker_key: str, *, timeout: float = 1.0) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("invalid coordinator URL")
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("non-loopback coordinator requires HTTPS")
        if not worker_key:
            raise ValueError("worker key is required")
        self.base_url = base_url.rstrip("/")
        self._worker_key = worker_key
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        *,
        maximum: int,
        headers: Mapping[str, str] | None = None,
        binary: bool = False,
    ) -> tuple[int, bytes, Mapping[str, str]]:
        encoded = None if body is None else canonical_json(body)
        if encoded is not None and len(encoded) > maximum:
            raise ProtocolError("request_too_large", 413)
        request_headers = {
            "Authorization": f"Bearer {self._worker_key}",
            "Accept": "application/gzip" if binary else "application/json",
            "User-Agent": "switchstand-local-worker/2",
        }
        if encoded is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        request = urllib.request.Request(self.base_url + path, data=encoded, headers=request_headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if binary:
                    required = ("Content-Type", "Content-Length", "X-Base-Sha", "X-Archive-Sha256")
                    if any(len(response.headers.get_all(name, [])) != 1 for name in required):
                        raise ProtocolError("invalid_request")
                    if response.headers.get("Content-Type") != "application/gzip":
                        raise ProtocolError("invalid_request")
                data = response.read(maximum + 1)
                if len(data) > maximum:
                    raise ProtocolError("request_too_large", 413)
                return response.status, data, dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            data = exc.read(4097)
            if len(data) > 4096:
                raise ProtocolError("temporary_failure", exc.code) from None
            parsed_error = strict_json(data, maximum=4096)
            if not isinstance(parsed_error, dict) or set(parsed_error) != {"error"}:
                raise ProtocolError("temporary_failure", exc.code) from None
            code = parsed_error["error"]
            if not isinstance(code, str) or code not in SAFE_ERRORS:
                raise ProtocolError("temporary_failure", exc.code) from None
            raise ProtocolError(code, exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ProtocolError("temporary_failure", 503) from None

    def _json(self, method: str, path: str, body: Mapping[str, Any], maximum: int) -> Mapping[str, Any]:
        status, data, _ = self._request(method, path, body, maximum=maximum)
        if status != 200:
            raise ProtocolError("temporary_failure", status)
        parsed = strict_json(data, maximum=maximum)
        if not isinstance(parsed, dict):
            raise ProtocolError("invalid_request")
        return parsed

    def register(self, worker_id: str, instance_id: str) -> Mapping[str, Any]:
        response = self._json(
            "POST",
            "/v2/workers/register",
            {"protocol": PROTOCOL, "worker_id": worker_id, "instance_id": instance_id, "capabilities": CAPABILITIES},
            4096,
        )
        _exact(
            response,
            {"protocol", "worker_id", "poll_after_seconds", "lease_seconds", "renew_after_seconds", "server_time"},
        )
        if response["protocol"] != PROTOCOL or response["worker_id"] != worker_id:
            raise ProtocolError("invalid_request")
        if (
            response["poll_after_seconds"] != 2
            or response["lease_seconds"] != 15
            or response["renew_after_seconds"] != 1
        ):
            raise ProtocolError("invalid_request")
        _timestamp(response["server_time"])
        return response

    def claim(self, worker_id: str, instance_id: str) -> Claim | None:
        body = {"protocol": PROTOCOL, "worker_id": worker_id, "instance_id": instance_id}
        status, data, _ = self._request("POST", "/v2/work/claim", body, maximum=16_384)
        if status == 204:
            if data:
                raise ProtocolError("invalid_request")
            return None
        if status != 200:
            raise ProtocolError("temporary_failure", status)
        value = strict_json(data, maximum=16_384)
        if not isinstance(value, dict):
            raise ProtocolError("invalid_request")
        keys = {
            "protocol",
            "work_id",
            "work_type",
            "worker_id",
            "instance_id",
            "fence",
            "lease_token",
            "lease_expires_at",
            "cancellation_version",
            "admission_sha",
            "source_text",
            "acceptance",
            "repository",
            "checkout_path",
            "prior_checkpoint",
            "codex_thread_id",
            "accepted_candidate",
            "limits",
        }
        _exact(value, keys)
        authority = self._validate_claim(value, worker_id, instance_id)
        return Claim(
            authority,
            value["work_type"],
            value["lease_expires_at"],
            value["admission_sha"],
            value["source_text"],
            tuple(value["acceptance"]),
            value["repository"],
            value["checkout_path"],
            value["prior_checkpoint"],
            value["codex_thread_id"],
            value["accepted_candidate"],
            value["limits"],
        )

    def _validate_claim(self, value: Mapping[str, Any], worker_id: str, instance_id: str) -> Authority:
        if value["protocol"] != PROTOCOL or value["worker_id"] != worker_id or value["instance_id"] != instance_id:
            raise ProtocolError("invalid_request")
        work_type = value["work_type"]
        if not isinstance(work_type, str) or work_type not in {"implementation", "review"}:
            raise ProtocolError("invalid_request")
        work_id = value["work_id"]
        if not isinstance(work_id, str) or not WORK_ID.fullmatch(work_id):
            raise ProtocolError("invalid_request")
        _uuid(worker_id)
        _uuid(instance_id)
        fence = _integer(value["fence"], 1)
        cancellation = _integer(value["cancellation_version"])
        token = value["lease_token"]
        if not isinstance(token, str) or not LEASE_TOKEN.fullmatch(token):
            raise ProtocolError("invalid_request")
        if not isinstance(value["admission_sha"], str) or not SHA256.fullmatch(value["admission_sha"]):
            raise ProtocolError("invalid_request")
        _timestamp(value["lease_expires_at"])
        _string(value["source_text"], 4096)
        acceptance = value["acceptance"]
        if not isinstance(acceptance, list) or len(acceptance) > 16:
            raise ProtocolError("invalid_request")
        for item in acceptance:
            _string(item, 512, minimum=1)
        self._validate_repository(value["repository"])
        if value["checkout_path"] != f"/v2/work/{work_id}/checkout":
            raise ProtocolError("invalid_request")
        self._validate_checkpoint(value["prior_checkpoint"], value["codex_thread_id"])
        self._validate_candidate(value["accepted_candidate"])
        if value["accepted_candidate"] is not None and value["work_type"] != "implementation":
            raise ProtocolError("invalid_request")
        if value["limits"] != {
            "max_files": 32,
            "max_file_bytes": 65536,
            "max_total_bytes": 262144,
            "max_deletions": 32,
            "max_json_bytes": 393216,
        }:
            raise ProtocolError("invalid_request")
        return Authority(work_id, worker_id, instance_id, fence, token, cancellation)

    def _validate_repository(self, repository: Any) -> None:
        if not isinstance(repository, dict):
            raise ProtocolError("invalid_request")
        _exact(repository, {"full_name", "base_sha", "candidate_branch", "allowed_path_prefixes"})
        if not isinstance(repository["base_sha"], str) or not SHA1.fullmatch(repository["base_sha"]):
            raise ProtocolError("invalid_request")
        full_name = _string(repository["full_name"], 200, minimum=3, printable=True)
        if not REPOSITORY.fullmatch(full_name):
            raise ProtocolError("invalid_request")
        _branch(repository["candidate_branch"])
        prefixes = repository["allowed_path_prefixes"]
        if not isinstance(prefixes, list) or not 1 <= len(prefixes) <= 8:
            raise ProtocolError("invalid_request")
        for prefix in prefixes:
            _prefix(prefix)
        if prefixes != sorted(set(prefixes), key=lambda item: item.encode()):
            raise ProtocolError("invalid_request")

    def _validate_checkpoint(self, checkpoint: Any, thread_id: Any) -> None:
        if checkpoint is None:
            if thread_id is not None:
                raise ProtocolError("invalid_request")
            return
        if not isinstance(checkpoint, dict):
            raise ProtocolError("invalid_request")
        _exact(checkpoint, {"sequence", "phase", "codex_thread_id", "checkpoint_state"})
        _integer(checkpoint["sequence"], 1)
        phase = checkpoint["phase"]
        if not isinstance(phase, str) or phase not in {
            "checkout_ready",
            "codex_started",
            "working",
            "testing",
            "candidate_ready",
        }:
            raise ProtocolError("invalid_request")
        _string(checkpoint["checkpoint_state"], 4096)
        if checkpoint["codex_thread_id"] != thread_id:
            raise ProtocolError("invalid_request")
        if thread_id is not None:
            _string(thread_id, 256, minimum=1, printable=True)
        elif phase != "checkout_ready":
            raise ProtocolError("invalid_request")

    def _validate_candidate(self, candidate: Any) -> None:
        if candidate is None:
            return
        if not isinstance(candidate, dict):
            raise ProtocolError("invalid_request")
        _exact(candidate, {"candidate_id", "manifest_sha"})
        _uuid(candidate["candidate_id"])
        if not isinstance(candidate["manifest_sha"], str) or not SHA256.fullmatch(candidate["manifest_sha"]):
            raise ProtocolError("invalid_request")

    def checkout(self, claim: Claim) -> tuple[bytes, Mapping[str, str]]:
        authority = claim.authority
        headers = {
            "X-Worker-Id": authority.worker_id,
            "X-Instance-Id": authority.instance_id,
            "X-Lease-Fence": str(authority.fence),
            "X-Lease-Token": authority.lease_token,
            "X-Cancellation-Version": str(authority.cancellation_version),
        }
        status, body, response_headers = self._request(
            "GET", claim.checkout_path, None, maximum=8 * 1024 * 1024, headers=headers, binary=True
        )
        if status != 200:
            raise ProtocolError("temporary_failure", status)
        return body, response_headers

    def renew(self, authority: Authority) -> Mapping[str, Any]:
        _validate_authority(authority)
        response = self._json("POST", f"/v2/work/{authority.work_id}/renew", authority.fields(), 4096)
        _exact(response, {"lease_expires_at", "renew_after_seconds", "cancellation_version"})
        if response["renew_after_seconds"] != 1 or response["cancellation_version"] != authority.cancellation_version:
            raise ProtocolError("stale_or_invalid_lease", 409)
        _timestamp(response["lease_expires_at"])
        return response

    def checkpoint(
        self, authority: Authority, operation_id: str, sequence: int, phase: str, thread_id: str | None, state: str
    ) -> Mapping[str, Any]:
        _validate_authority(authority)
        _operation_id(operation_id)
        _integer(sequence, 1)
        if phase not in {"checkout_ready", "codex_started", "working", "testing", "candidate_ready"}:
            raise ProtocolError("invalid_request")
        if thread_id is not None:
            _string(thread_id, 256, minimum=1, printable=True)
        _string(state, 4096)
        body = {
            **authority.fields(),
            "operation_id": operation_id,
            "sequence": sequence,
            "phase": phase,
            "codex_thread_id": thread_id,
            "checkpoint_state": state,
        }
        response = self._json("POST", f"/v2/work/{authority.work_id}/checkpoint", body, 8192)
        _exact(response, {"accepted_sequence", "lease_expires_at"})
        if response["accepted_sequence"] != sequence:
            raise ProtocolError("invalid_request")
        _timestamp(response["lease_expires_at"])
        return response

    def submit_candidate(self, authority: Authority, manifest: Mapping[str, Any]) -> Mapping[str, Any]:
        _validate_authority(authority)
        required = {
            "operation_id",
            "base_sha",
            "expected_branch_head",
            "message",
            "files",
            "deletions",
            "check_summaries",
            "request_digest",
        }
        _exact(manifest, required)
        _operation_id(manifest["operation_id"])
        for key in ("base_sha", "expected_branch_head"):
            if not isinstance(manifest[key], str) or not SHA1.fullmatch(manifest[key]):
                raise ProtocolError("invalid_request")
        if not isinstance(manifest["request_digest"], str) or not SHA256.fullmatch(manifest["request_digest"]):
            raise ProtocolError("invalid_request")
        body = {**authority.fields(), **manifest}
        response = self._json("POST", f"/v2/work/{authority.work_id}/candidate", body, 393_216)
        _exact(response, {"candidate_id", "manifest_sha", "status"})
        _uuid(response["candidate_id"])
        valid_sha = isinstance(response["manifest_sha"], str) and SHA256.fullmatch(response["manifest_sha"])
        if response["status"] != "candidate_ready" or not valid_sha:
            raise ProtocolError("invalid_request")
        return response

    def complete(
        self,
        authority: Authority,
        operation_id: str,
        *,
        status: str,
        candidate_id: str | None = None,
        review_verdict: str | None = None,
        summary_code: str,
        checks: list[Mapping[str, str]] | None = None,
    ) -> Mapping[str, Any]:
        _validate_authority(authority)
        _operation_id(operation_id)
        if status not in {"succeeded", "failed", "scope_return"}:
            raise ProtocolError("invalid_request")
        if candidate_id is not None:
            _uuid(candidate_id)
        if review_verdict not in {None, "PASS", "BLOCK"}:
            raise ProtocolError("invalid_request")
        if not SUMMARY_CODE.fullmatch(summary_code):
            raise ProtocolError("invalid_request")
        checked = checks or []
        if len(checked) > 32:
            raise ProtocolError("request_too_large", 413)
        names: list[str] = []
        for item in checked:
            if set(item) != {"name", "outcome"}:
                raise ProtocolError("invalid_request")
            names.append(_string(item["name"], 80, minimum=1))
            if item["outcome"] not in {"PASS", "FAIL"}:
                raise ProtocolError("invalid_request")
        if names != sorted(names, key=lambda item: item.encode()) or len(names) != len(set(names)):
            raise ProtocolError("invalid_request")
        body = {
            **authority.fields(),
            "operation_id": operation_id,
            "status": status,
            "candidate_id": candidate_id,
            "review_verdict": review_verdict,
            "summary_code": summary_code,
            "checks": checked,
        }
        response = self._json("POST", f"/v2/work/{authority.work_id}/complete", body, 16_384)
        _exact(response, {"work_id", "status", "completed_at"})
        if response["work_id"] != authority.work_id or response["status"] != status:
            raise ProtocolError("invalid_request")
        _timestamp(response["completed_at"])
        return response
