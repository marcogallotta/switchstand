"""Checkout validation and deterministic candidate construction."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile
import unicodedata
from typing import Any, Iterable, Mapping
import zlib

from .protocol import Claim, ProtocolError, canonical_json


MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 4_000
MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_TAR_BYTES = MAX_EXPANDED_BYTES + MAX_ARCHIVE_ENTRIES * 1024 + 10 * 1024


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = {key.lower(): value for key, value in headers.items()}
    return lowered.get(name.lower())


def validate_path(path: str, prefixes: Iterable[str]) -> str:
    if not isinstance(path, str) or not path or len(path.encode("utf-8")) > 240:
        raise ProtocolError("policy_denied", 403)
    if path != unicodedata.normalize("NFC", path) or "\\" in path:
        raise ProtocolError("policy_denied", 403)
    pure = PurePosixPath(path)
    parts = pure.parts
    if pure.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise ProtocolError("policy_denied", 403)
    if any(any(ord(character) < 32 or ord(character) == 127 for character in part) for part in parts):
        raise ProtocolError("policy_denied", 403)
    allowed = any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in prefixes)
    if not allowed:
        raise ProtocolError("policy_denied", 403)
    return path


def _inflate_one_gzip(payload: bytes) -> bytes:
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ProtocolError("request_too_large", 413)
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        result = inflater.decompress(payload, MAX_TAR_BYTES + 1)
        if inflater.unconsumed_tail or len(result) > MAX_TAR_BYTES:
            raise ProtocolError("request_too_large", 413)
        result += inflater.flush(MAX_TAR_BYTES + 1 - len(result))
    except zlib.error:
        raise ProtocolError("invalid_request") from None
    if not inflater.eof or inflater.unused_data or inflater.unconsumed_tail or len(result) > MAX_TAR_BYTES:
        raise ProtocolError("request_too_large" if len(result) > MAX_TAR_BYTES else "invalid_request")
    return result


def materialize_checkout(
    claim: Claim,
    payload: bytes,
    headers: Mapping[str, str],
    destination: Path,
) -> None:
    """Verify and atomically publish one strict-root gzip tar checkout."""
    if destination.exists():
        raise ProtocolError("policy_denied", 403)
    content_length = _header(headers, "Content-Length")
    archive_sha = _header(headers, "X-Archive-Sha256")
    base_sha = _header(headers, "X-Base-Sha")
    if content_length != str(len(payload)) or base_sha != claim.repository["base_sha"]:
        raise ProtocolError("invalid_request")
    if archive_sha != hashlib.sha256(payload).hexdigest():
        raise ProtocolError("invalid_request")
    expanded = _inflate_one_gzip(payload)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    os.chmod(temporary, 0o700)
    try:
        _extract_tar(expanded, temporary)
        os.replace(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _extract_tar(expanded: bytes, temporary: Path) -> None:
    seen: set[str] = set()
    folded: set[str] = set()
    roots: set[str] = set()
    root_entries = 0
    entry_kinds: dict[str, str] = {}
    total = 0
    try:
        archive = tarfile.open(fileobj=io.BytesIO(expanded), mode="r:")
    except tarfile.TarError:
        raise ProtocolError("invalid_request") from None
    with archive:
        members = archive.getmembers()
        if not members or len(members) > MAX_ARCHIVE_ENTRIES:
            raise ProtocolError("request_too_large" if len(members) > MAX_ARCHIVE_ENTRIES else "invalid_request")
        validated: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
        for member in members:
            name = member.name
            if (
                name != unicodedata.normalize("NFC", name)
                or "\\" in name
                or any(ord(character) < 32 or ord(character) == 127 for character in name)
            ):
                raise ProtocolError("invalid_request")
            pure = PurePosixPath(name)
            parts = pure.parts
            if pure.is_absolute() or len(parts) < 1 or any(part in {"", ".", ".."} for part in parts):
                raise ProtocolError("invalid_request")
            roots.add(parts[0])
            if ".git" in parts[1:]:
                raise ProtocolError("invalid_request")
            relative = "/".join(parts[1:])
            if relative:
                if relative in seen or relative.casefold() in folded:
                    raise ProtocolError("invalid_request")
                seen.add(relative)
                folded.add(relative.casefold())
            else:
                root_entries += 1
            if not (member.isdir() or member.isfile()):
                raise ProtocolError("invalid_request")
            if member.isfile():
                total += member.size
                if total > MAX_EXPANDED_BYTES:
                    raise ProtocolError("request_too_large", 413)
            entry_kinds[relative] = "directory" if member.isdir() else "file"
            validated.append((member, parts[1:]))
        if len(roots) != 1 or root_entries != 1:
            raise ProtocolError("invalid_request")
        for relative in entry_kinds:
            parts = PurePosixPath(relative).parts
            for index in range(1, len(parts)):
                if entry_kinds.get("/".join(parts[:index])) == "file":
                    raise ProtocolError("invalid_request")
        for member, relative_parts in validated:
            if not relative_parts:
                if not member.isdir():
                    raise ProtocolError("invalid_request")
                continue
            target = temporary.joinpath(*relative_parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source = archive.extractfile(member)
            if source is None:
                raise ProtocolError("invalid_request")
            data = source.read(member.size + 1)
            if len(data) != member.size:
                raise ProtocolError("invalid_request")
            mode = 0o700 if member.mode & 0o111 else 0o600
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)


def initialize_local_git(workspace: Path) -> None:
    environment = _git_environment(workspace)
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Switchstand Worker",
            "GIT_AUTHOR_EMAIL": "worker.invalid@example.invalid",
            "GIT_COMMITTER_NAME": "Switchstand Worker",
            "GIT_COMMITTER_EMAIL": "worker.invalid@example.invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
        }
    )
    for command in (
        ["git", "init", "--quiet"],
        ["git", "add", "--all"],
        ["git", "commit", "--quiet", "--allow-empty", "-m", "bounded checkout"],
    ):
        completed = subprocess.run(
            command,
            cwd=workspace,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode:
            raise ProtocolError("invalid_request")


def _git(workspace: Path, *arguments: str) -> bytes:
    environment = _git_environment(workspace)
    completed = subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode:
        raise ProtocolError("invalid_request")
    return completed.stdout


def _git_environment(workspace: Path) -> dict[str, str]:
    private_home = workspace.parent / ".git-environment"
    private_home.mkdir(mode=0o700, exist_ok=True)
    os.chmod(private_home, 0o700)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(private_home),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _changed_paths(workspace: Path) -> tuple[list[str], list[str]]:
    raw = _git(workspace, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    fields = raw.split(b"\0")
    files: list[str] = []
    deletions: list[str] = []
    index = 0
    while index < len(fields) and fields[index]:
        entry = fields[index]
        index += 1
        if len(entry) < 4 or entry[2:3] != b" ":
            raise ProtocolError("invalid_request")
        status = entry[:2].decode("ascii", "strict")
        try:
            path = entry[3:].decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolError("policy_denied", 403) from None
        if "R" in status or "C" in status or "U" in status or status == "!!":
            raise ProtocolError("policy_denied", 403)
        if "D" in status:
            deletions.append(path)
        else:
            files.append(path)
    return files, deletions


def _base_mode(workspace: Path, path: str) -> str:
    output = _git(workspace, "ls-tree", "HEAD", "--", path).decode("utf-8")
    if not output:
        raise ProtocolError("policy_denied", 403)
    return output.split(maxsplit=1)[0]


def _base_paths(workspace: Path) -> dict[str, str]:
    raw = _git(workspace, "ls-tree", "-r", "-z", "--name-only", "HEAD")
    result: dict[str, str] = {}
    for encoded in raw.split(b"\0"):
        if not encoded:
            continue
        try:
            path = encoded.decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolError("policy_denied", 403) from None
        folded = unicodedata.normalize("NFC", path).casefold()
        if folded in result and result[folded] != path:
            raise ProtocolError("policy_denied", 403)
        result[folded] = path
    return result


def build_candidate(
    claim: Claim,
    workspace: Path,
    *,
    operation_id: str,
    message: str,
    check_summaries: list[Mapping[str, str]],
) -> dict[str, Any]:
    if claim.work_type != "implementation" or claim.accepted_candidate is not None:
        raise ProtocolError("work_type_forbidden", 403)
    if not message or message != message.strip() or "\n" in message or len(message.encode("utf-8")) > 160:
        raise ProtocolError("invalid_request")
    prefixes = claim.repository["allowed_path_prefixes"]
    changed, deleted = _changed_paths(workspace)
    if not changed and not deleted:
        raise ProtocolError("invalid_request")
    if len(changed) > 32 or len(deleted) > 32:
        raise ProtocolError("request_too_large", 413)
    all_paths = changed + deleted
    validated = [validate_path(path, prefixes) for path in all_paths]
    if len(validated) != len(set(validated)) or len({path.casefold() for path in validated}) != len(validated):
        raise ProtocolError("policy_denied", 403)
    base_paths = _base_paths(workspace)
    for path in changed:
        collision = base_paths.get(path.casefold())
        if collision is not None and collision != path:
            raise ProtocolError("policy_denied", 403)
    files: list[dict[str, Any]] = []
    total = 0
    for path in sorted(changed, key=lambda item: item.encode("utf-8")):
        target = workspace / path
        if target.is_symlink() or not target.is_file() or target.stat().st_mode & 0o111:
            raise ProtocolError("policy_denied", 403)
        if target.stat().st_size > 65_536:
            raise ProtocolError("request_too_large", 413)
        data = target.read_bytes()
        if b"\0" in data:
            raise ProtocolError("policy_denied", 403)
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolError("policy_denied", 403) from None
        if path.casefold() in base_paths:
            if _base_mode(workspace, path) != "100644":
                raise ProtocolError("policy_denied", 403)
            if _git(workspace, "show", f"HEAD:{path}") == data:
                raise ProtocolError("policy_denied", 403)
        total += len(data)
        if total > 262_144:
            raise ProtocolError("request_too_large", 413)
        files.append(
            {
                "path": path,
                "mode": "100644",
                "type": "blob",
                "content_base64": base64.b64encode(data).decode("ascii"),
                "decoded_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    deletions: list[dict[str, str]] = []
    for path in sorted(deleted, key=lambda item: item.encode("utf-8")):
        if _base_mode(workspace, path) != "100644":
            raise ProtocolError("policy_denied", 403)
        deletions.append({"path": path})
    checks = _validate_checks(check_summaries)
    request = {
        "operation_id": operation_id,
        "base_sha": claim.repository["base_sha"],
        "expected_branch_head": claim.repository["base_sha"],
        "message": message,
        "files": files,
        "deletions": deletions,
        "check_summaries": checks,
    }
    digest_input = {**claim.authority.fields(), **request}
    request["request_digest"] = hashlib.sha256(canonical_json(digest_input)).hexdigest()
    if len(canonical_json({**claim.authority.fields(), **request})) > 393_216:
        raise ProtocolError("request_too_large", 413)
    return request


def _validate_checks(checks: list[Mapping[str, str]]) -> list[dict[str, str]]:
    if len(checks) > 32:
        raise ProtocolError("request_too_large", 413)
    result: list[dict[str, str]] = []
    for item in checks:
        if set(item) != {"name", "outcome", "summary"}:
            raise ProtocolError("invalid_request")
        name, outcome, summary = item["name"], item["outcome"], item["summary"]
        if not isinstance(name, str) or not 1 <= len(name.encode()) <= 80 or outcome not in {"PASS", "FAIL"}:
            raise ProtocolError("invalid_request")
        if (
            not isinstance(summary, str)
            or len(summary.encode()) > 1024
            or any(token in summary.lower() for token in ("/", "prompt", "output", "error"))
        ):
            raise ProtocolError("invalid_request")
        result.append(dict(item))
    if result != sorted(result, key=lambda item: item["name"].encode()) or len(
        {item["name"] for item in result}
    ) != len(result):
        raise ProtocolError("invalid_request")
    return result


def validate_candidate_payload(value: bytes, *, compressed: bool = False) -> Mapping[str, Any]:
    """Test seam for strict single-member gzip and canonical Base64 checks."""
    if len(value) > 393_216:
        raise ProtocolError("request_too_large", 413)
    if compressed:
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            value = inflater.decompress(value, 393_217)
            if inflater.unconsumed_tail or len(value) > 393_216:
                raise ProtocolError("request_too_large", 413)
            value += inflater.flush(393_217 - len(value))
        except zlib.error:
            raise ProtocolError("invalid_request") from None
        if not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
            raise ProtocolError("invalid_request")
    if len(value) > 393_216:
        raise ProtocolError("request_too_large", 413)
    try:
        parsed = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProtocolError("invalid_request") from None
    if not isinstance(parsed, dict):
        raise ProtocolError("invalid_request")
    for file in parsed.get("files", []):
        try:
            decoded = base64.b64decode(file["content_base64"], validate=True)
        except (KeyError, TypeError, binascii.Error):
            raise ProtocolError("invalid_request") from None
        if base64.b64encode(decoded).decode("ascii") != file["content_base64"]:
            raise ProtocolError("invalid_request")
    return parsed
