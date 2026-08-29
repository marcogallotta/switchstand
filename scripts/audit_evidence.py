"""Content-addressed audit evidence and source-backed containment assertions."""
from __future__ import annotations

import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib
from typing import Any
import urllib.error
import urllib.request


class EvidenceError(ValueError):
    """Audit evidence or containment source does not match its claim."""


WORKER_QUARANTINE_METHODS = (
    ("WorkerConfig", "create"),
    ("LeaseGuard", "__init__"),
    ("LeaseGuard", "start"),
    ("LeaseGuard", "stop"),
    ("LeaseGuard", "lost"),
    ("LeaseGuard", "require_live"),
    ("LeaseGuard", "attach"),
    ("LeaseGuard", "detach"),
    ("LeaseGuard", "_loop"),
    ("LeaseGuard", "_lose"),
    ("LeaseGuard", "_kill"),
    ("CodexRunner", "__init__"),
    ("CodexRunner", "_boundary"),
    ("CodexRunner", "_run"),
    ("CodexRunner", "bootstrap"),
    ("CodexRunner", "task_turn"),
    ("CodexRunner", "thread_in_state_db"),
    ("Worker", "__init__"),
    ("Worker", "run_once"),
    ("Worker", "_execute"),
)
WORKER_QUARANTINE_DIGESTS = {
    "pyproject.toml": "027356a1aec84a018039d1fb433c24a7e857cafab10e1e5c22148aabcd6d057e",
    "src/switchstand_worker/__main__.py": (
        "99d913476b33fe7e6e39760c5862f77735ee761f135d71fd8986d4dd716311bb"
    ),
    "src/switchstand_worker/__init__.py": (
        "a79cd28862f907be56ae836a53430c54f9c9aa7a42a16d2168d85511b2be7dbc"
    ),
    "src/switchstand_worker/supervisor.py": (
        "cbe43bba889976959afea96d707a87970c41a2b4622f7a1f90d1a8508aed87f3"
    ),
}
GITHUB_PROVENANCE = {
    "github_actions_run": re.compile(
        r"https://github\.com/marcogallotta/switchstand/actions/runs/(?P<id>[0-9]+)(?:/job/[0-9]+)?"
    ),
    "github_pull_request_review": re.compile(
        r"https://github\.com/marcogallotta/switchstand/pull/(?P<pr>[0-9]+)"
        r"#pullrequestreview-(?P<id>[0-9]+)"
    ),
    "github_issue_comment": re.compile(
        r"https://github\.com/marcogallotta/switchstand/(?:pull|issues)/[0-9]+"
        r"#issuecomment-(?P<id>[0-9]+)"
    ),
}
REVIEW_ROLES = {"severity_review", "independent_review", "gate_repair_review"}


def _verify_head_binding(root: Path, head_sha: str, field: str) -> None:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{head_sha}^{{commit}}"],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode:
        raise EvidenceError(f"{field}.head_sha is not an available commit")
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", head_sha, "HEAD"],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode:
        raise EvidenceError(f"{field}.head_sha is not an ancestor of the candidate")
    result = subprocess.run(
        ["git", "diff", "--name-only", head_sha, "HEAD", "--"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    allowed = {"audit/findings.json", "audit/change-scope.json"}
    changed = {line for line in result.stdout.splitlines() if line}
    if any(path not in allowed and not path.startswith("audit/receipts/") for path in changed):
        raise EvidenceError(f"{field}.head_sha is not the exact implementation head")


def _github_json(url: str, field: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "switchstand-audit-gate"},
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            value = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{field} external GitHub evidence is unavailable") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{field} external GitHub evidence is invalid")
    return value


def _verify_external_provenance(
    kind: str, reference: str, producer: str, role: str, subject: str, head_sha: str, field: str
) -> None:
    pattern = GITHUB_PROVENANCE.get(kind)
    match = pattern.fullmatch(reference) if pattern is not None else None
    if match is None:
        raise EvidenceError(f"{field} receipt lacks eligible external GitHub provenance")
    object_id = match.group("id")
    if kind == "github_actions_run":
        value = _github_json(
            f"https://api.github.com/repos/marcogallotta/switchstand/actions/runs/{object_id}", field
        )
        if (
            value.get("id") != int(object_id)
            or value.get("head_sha") != head_sha
            or value.get("conclusion") != "success"
            or producer != f"github-actions-run:{object_id}"
            or role in REVIEW_ROLES
        ):
            raise EvidenceError(f"{field} GitHub Actions provenance does not bind its claim")
        return
    endpoint = (
        f"pulls/{match.group('pr')}/reviews/{object_id}"
        if kind == "github_pull_request_review"
        else f"issues/comments/{object_id}"
    )
    value = _github_json(f"https://api.github.com/repos/marcogallotta/switchstand/{endpoint}", field)
    user = value.get("user")
    body = value.get("body")
    producer_is_agent = re.fullmatch(r"codex-agent:/[A-Za-z0-9_/-]+", producer) is not None
    if (
        value.get("id") != int(object_id)
        or not isinstance(user, dict)
        or not isinstance(user.get("login"), str)
        or not isinstance(user.get("id"), int)
        or value.get("author_association") not in {"OWNER", "MEMBER", "COLLABORATOR"}
        or not isinstance(body, str)
        or f"[role:{role}]" not in body
        or f"[subject:{subject}]" not in body
        or head_sha not in body
    ):
        raise EvidenceError(f"{field} GitHub review provenance does not bind its claim")
    if role in REVIEW_ROLES:
        if not producer_is_agent or f"[producer:{producer}]" not in body:
            raise EvidenceError(f"{field} GitHub review does not attest its agent producer")
    elif producer != f"github-user-id:{user['id']}":
        raise EvidenceError(f"{field} GitHub evidence producer does not match its publisher")
    if kind == "github_pull_request_review" and (
        value.get("commit_id") != head_sha or value.get("state") != "APPROVED"
    ):
        raise EvidenceError(f"{field} GitHub review does not approve the exact head")


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{field} must be a nonempty string")
    return value


def _utc(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EvidenceError(f"{field} must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise EvidenceError(f"{field} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise EvidenceError(f"{field} must use UTC")


def verify_evidence(
    value: object,
    field: str,
    root: Path,
    *,
    durable: bool = False,
    expected_role: str | None = None,
    expected_subject: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{field} must be an object")
    expected = {"kind", "role", "subject", "producer", "reference", "sha256"}
    if set(value) != expected:
        raise EvidenceError(f"{field} has wrong evidence keys")
    kind = _nonempty(value["kind"], f"{field}.kind")
    role = _nonempty(value["role"], f"{field}.role")
    subject = _nonempty(value["subject"], f"{field}.subject")
    producer = _nonempty(value["producer"], f"{field}.producer")
    reference = _nonempty(value["reference"], f"{field}.reference")
    if expected_role is not None and role != expected_role:
        raise EvidenceError(f"{field}.role must be {expected_role}")
    if expected_subject is not None and subject != expected_subject:
        raise EvidenceError(f"{field}.subject must be {expected_subject}")
    digest = value["sha256"]
    if digest is not None and (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise EvidenceError(f"{field}.sha256 must be null or 64 lowercase hexadecimal characters")
    if not durable:
        if kind not in {"asana_audit", "content_addressed_receipt"}:
            raise EvidenceError(f"{field}.kind is unsupported")
        if kind == "asana_audit" and digest is not None:
            raise EvidenceError(f"{field} Asana import cannot claim a content digest")
        return value
    if kind != "content_addressed_receipt" or digest is None:
        raise EvidenceError(f"{field} must be a content-addressed receipt")
    expected_reference = f"audit/receipts/{digest}.json"
    if reference != expected_reference:
        raise EvidenceError(f"{field}.reference must be {expected_reference}")
    receipt_path = root / reference
    try:
        raw = receipt_path.read_bytes()
    except OSError as exc:
        raise EvidenceError(f"{field} receipt is unavailable") from exc
    if hashlib.sha256(raw).hexdigest() != digest:
        raise EvidenceError(f"{field} receipt digest does not match its content")
    try:
        receipt = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{field} receipt is not valid JSON") from exc
    if not isinstance(receipt, dict):
        raise EvidenceError(f"{field} receipt must contain one object")
    receipt_keys = {
        "schema_version",
        "role",
        "subject",
        "producer",
        "created_at",
        "head_sha",
        "evidence_kind",
        "evidence_reference",
        "summary",
    }
    if set(receipt) != receipt_keys or receipt["schema_version"] != 1:
        raise EvidenceError(f"{field} receipt has wrong schema")
    if (receipt["role"], receipt["subject"], receipt["producer"]) != (role, subject, producer):
        raise EvidenceError(f"{field} receipt identity does not match the register")
    _utc(receipt["created_at"], f"{field}.receipt.created_at")
    head_sha = _nonempty(receipt["head_sha"], f"{field}.receipt.head_sha")
    if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise EvidenceError(f"{field}.receipt.head_sha must be an exact commit SHA")
    evidence_kind = _nonempty(receipt["evidence_kind"], f"{field}.receipt.evidence_kind")
    evidence_reference = _nonempty(
        receipt["evidence_reference"], f"{field}.receipt.evidence_reference"
    )
    if role == "ci" and evidence_kind != "github_actions_run":
        raise EvidenceError(f"{field} CI evidence must be a GitHub Actions run")
    _verify_head_binding(root, head_sha, field)
    _verify_external_provenance(
        evidence_kind, evidence_reference, producer, role, subject, head_sha, field
    )
    _nonempty(receipt["summary"], f"{field}.receipt.summary")
    return value


def _module(path: Path) -> ast.Module:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise EvidenceError(f"worker quarantine source is unavailable or invalid: {path}") from exc


def _class_method(tree: ast.Module, class_name: str, method_name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == method_name:
                    return child
    raise EvidenceError(f"worker quarantine method is missing: {class_name}.{method_name}")


def _method_is_fixed_quarantine(method: ast.FunctionDef, label: str) -> None:
    body = method.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Raise):
        raise EvidenceError(f"worker quarantine is not the sole behavior in {label}")
    raised = body[0].exc
    if (
        not isinstance(raised, ast.Call)
        or not isinstance(raised.func, ast.Name)
        or raised.func.id != "RuntimeError"
        or len(raised.args) != 1
        or not isinstance(raised.args[0], ast.Constant)
        or raised.args[0].value != "worker_quarantined"
    ):
        raise EvidenceError(f"worker quarantine lacks the fixed error in {label}")


def assert_worker_quarantine(root: Path) -> None:
    for relative, expected_digest in WORKER_QUARANTINE_DIGESTS.items():
        try:
            actual_digest = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        except OSError as exc:
            raise EvidenceError(f"worker quarantine source is unavailable: {relative}") from exc
        if actual_digest != expected_digest:
            raise EvidenceError(f"worker quarantine source differs from the reviewed stub: {relative}")
    try:
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise EvidenceError("worker entrypoint configuration is unavailable or invalid") from exc
    if pyproject.get("project", {}).get("scripts", {}).get("switchstand-worker") != (
        "switchstand_worker.__main__:main"
    ):
        raise EvidenceError("worker console entrypoint is not the quarantined module")
    main_tree = _module(root / "src/switchstand_worker/__main__.py")
    top_imports = [node for node in main_tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    imported = {
        alias.name
        for node in top_imports
        if not (isinstance(node, ast.ImportFrom) and node.module == "__future__")
        for alias in node.names
    }
    if imported != {"sys"}:
        raise EvidenceError("worker module entrypoint imports execution authority")
    disclosure_constants = [
        node
        for node in main_tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "QUARANTINE_ERROR" for target in node.targets)
        and isinstance(node.value, ast.Constant)
        and node.value.value == "WORKER_QUARANTINED"
    ]
    if len(disclosure_constants) != 1:
        raise EvidenceError("worker module entrypoint lacks the fixed quarantine disclosure")
    main = next(
        (node for node in main_tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"),
        None,
    )
    if main is None:
        raise EvidenceError("worker module entrypoint main is missing")
    calls = [node for node in ast.walk(main) if isinstance(node, ast.Call)]
    if len(calls) != 1:
        raise EvidenceError("worker module entrypoint performs work beyond fixed disclosure")
    disclosure = calls[0]
    if (
        not isinstance(disclosure.func, ast.Name)
        or disclosure.func.id != "print"
        or len(disclosure.args) != 1
        or not isinstance(disclosure.args[0], ast.Name)
        or disclosure.args[0].id != "QUARANTINE_ERROR"
        or len(disclosure.keywords) != 1
        or disclosure.keywords[0].arg != "file"
        or not isinstance(disclosure.keywords[0].value, ast.Attribute)
        or not isinstance(disclosure.keywords[0].value.value, ast.Name)
        or disclosure.keywords[0].value.value.id != "sys"
        or disclosure.keywords[0].value.attr != "stderr"
    ):
        raise EvidenceError("worker module entrypoint lacks the exact quarantine disclosure")
    returns = [node.value for node in ast.walk(main) if isinstance(node, ast.Return)]
    if len(returns) != 1 or not isinstance(returns[0], ast.Constant) or returns[0].value != 1:
        raise EvidenceError("worker module entrypoint does not return the fixed failure status")

    init_tree = _module(root / "src/switchstand_worker/__init__.py")
    if any(
        isinstance(node, ast.ImportFrom) and node.module == "switchstand_worker.supervisor"
        for node in init_tree.body
    ):
        raise EvidenceError("worker package eagerly imports execution authority")
    if any(
        isinstance(child, ast.Call)
        for node in init_tree.body
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for child in ast.walk(node)
    ):
        raise EvidenceError("worker package performs eager execution")

    supervisor_tree = _module(root / "src/switchstand_worker/supervisor.py")
    supervisor_imports = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in supervisor_tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
        if not (isinstance(node, ast.ImportFrom) and node.module == "__future__")
    }
    if supervisor_imports != {"dataclasses", "pathlib", "typing"}:
        raise EvidenceError("worker supervisor imports execution authority while quarantined")
    expected_by_class: dict[str, set[str]] = {}
    for class_name, method_name in WORKER_QUARANTINE_METHODS:
        expected_by_class.setdefault(class_name, set()).add(method_name)
    for node in supervisor_tree.body:
        if isinstance(node, ast.ClassDef) and node.name in expected_by_class:
            actual = {child.name for child in node.body if isinstance(child, ast.FunctionDef)}
            if actual != expected_by_class[node.name]:
                raise EvidenceError(f"worker quarantine API surface changed in {node.name}")
    for class_name, method_name in WORKER_QUARANTINE_METHODS:
        _method_is_fixed_quarantine(
            _class_method(supervisor_tree, class_name, method_name), f"{class_name}.{method_name}"
        )
