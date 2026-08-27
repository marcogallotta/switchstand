#!/usr/bin/env python3
"""Exact-head, privacy-safe Stage B1 live checkpoint runner.

The runner observes one explicit root; it never discovers, resumes, subscribes to,
or controls a thread. Evidence must be written outside the target repository.
"""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping

from switchstand.app_server import CodexAppServer


ALLOWED_METHODS = {"initialize", "initialized", "thread/read", "thread/list"}


def observer_api() -> tuple[type[Any], str, str]:
    """Load either merged B1 or its reviewed predecessor without changing the target tree."""
    try:
        module = importlib.import_module("switchstand.native_board")
        return module.NativeBoard, "poll_once", "agents"
    except ModuleNotFoundError:
        module = importlib.import_module("switchstand.native_observer")
        return module.NativeObserver, "observe_once", "threads"


def safe_threads(snapshot: Mapping[str, Any], collection: str) -> list[dict[str, Any]]:
    items = snapshot.get(collection, [])
    result = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        status = item.get("status")
        result.append(
            {
                "ref": item.get("agentRef", item.get("ref")),
                "parentRef": item.get("parentRef"),
                "depth": item.get("depth"),
                "status": status.get("type") if isinstance(status, Mapping) else status,
            }
        )
    return result


def git_value(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=repo, text=True, stderr=subprocess.DEVNULL
    ).strip()


def git_status(repo: Path) -> bytes:
    return subprocess.check_output(
        ["git", "status", "--porcelain=v1", "-z"], cwd=repo, stderr=subprocess.DEVNULL
    )


def safe_params(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
    if method == "thread/read":
        return {
            "includeTurns": params.get("includeTurns"),
            "hasExactThreadId": isinstance(params.get("threadId"), str)
            and bool(params.get("threadId")),
        }
    if method == "thread/list":
        return {
            "useStateDbOnly": params.get("useStateDbOnly"),
            "limit": params.get("limit"),
            "cursorPresent": "cursor" in params,
            "ancestorFilterPresent": isinstance(params.get("ancestorThreadId"), str)
            and bool(params.get("ancestorThreadId")),
            "sourceKindCount": len(params.get("sourceKinds", []))
            if isinstance(params.get("sourceKinds"), list)
            else None,
        }
    return {}


class AuditedClient(CodexAppServer):
    def __init__(self, socket_path: Path, pass_number: int, audit: list[dict[str, Any]]) -> None:
        self._pass_number = pass_number
        self._audit = audit
        super().__init__(socket_path, client_name="switchstand-stage-b1-live-check")

    def _record(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        record = {
            "pass": self._pass_number,
            "method": method,
            "params": safe_params(method, params),
        }
        self._audit.append(record)
        return record

    def _request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        record = self._record(method, params)
        result = super()._request(method, params)
        if method == "thread/list":
            data = result.get("data")
            record["resultCount"] = len(data) if isinstance(data, list) else None
            record["nextCursorPresent"] = result.get("nextCursor") is not None
        return result

    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._record(method, params)
        super()._notify(method, params)


def write_result(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def blocked(
    output: Path,
    code: str,
    *,
    sha: str | None = None,
    tree: str | None = None,
    audit: list[dict[str, Any]] | None = None,
    passes: list[dict[str, Any]] | None = None,
) -> int:
    methods = [entry["method"] for entry in audit or []]
    value = {
        "schemaVersion": 1,
        "result": "BLOCKED",
        "code": code,
        "sha": sha,
        "tree": tree,
        "passes": passes or [],
        "audit": audit or [],
        "methodAllowlist": sorted(set(methods)),
        "forbiddenMethodCount": sum(method not in ALLOWED_METHODS for method in methods),
        "repositoryMutationCount": 0,
    }
    write_result(output, value)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--root-thread-id", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-tree", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--max-passes", type=int, default=30)
    args = parser.parse_args()

    try:
        sha = git_value(args.repo, "rev-parse", "HEAD")
        tree = git_value(args.repo, "rev-parse", "HEAD^{tree}")
        initial_status = git_status(args.repo)
    except Exception:
        return blocked(args.output, "exact_head_unavailable")
    if sha != args.expected_sha or tree != args.expected_tree:
        return blocked(args.output, "exact_head_mismatch", sha=sha, tree=tree)
    if not args.socket.exists():
        return blocked(args.output, "app_server_socket_unavailable", sha=sha, tree=tree)
    if args.interval <= 0 or args.max_passes < 2:
        return blocked(args.output, "invalid_runner_limits", sha=sha, tree=tree)

    audit: list[dict[str, Any]] = []
    passes: list[dict[str, Any]] = []
    current_pass = 0

    def factory() -> AuditedClient:
        return AuditedClient(args.socket, current_pass, audit)

    try:
        observer_class, poll_method, collection = observer_api()
        observer = observer_class(factory, args.root_thread_id)
    except Exception:
        return blocked(args.output, "observer_unavailable", sha=sha, tree=tree)
    last_active: dict[str, int] = {}
    transition: dict[str, Any] | None = None

    for pass_number in range(1, args.max_passes + 1):
        current_pass = pass_number
        getattr(observer, poll_method)()
        snapshot = observer.snapshot()
        observation = snapshot.get("observation", {})
        projected = safe_threads(snapshot, collection)
        passes.append(
            {
                "pass": pass_number,
                "connected": observation.get("connected") is True,
                "historical": observation.get("historical") is True,
                "threads": projected,
            }
        )
        if observation.get("connected") is not True:
            return blocked(
                args.output,
                "observation_pass_failed",
                sha=sha,
                tree=tree,
                audit=audit,
                passes=passes,
            )

        for item in projected:
            ref = item["ref"]
            if item["parentRef"] is None or not isinstance(ref, str):
                continue
            if item["status"] == "active":
                last_active[ref] = pass_number
            elif item["status"] == "idle" and ref in last_active:
                interval_count = pass_number - last_active[ref]
                if interval_count <= 2:
                    transition = {
                        "threadRef": ref,
                        "activePass": last_active[ref],
                        "idlePass": pass_number,
                        "pollIntervals": interval_count,
                    }
                    break
        if transition is not None:
            break
        time.sleep(args.interval)

    methods = [entry["method"] for entry in audit]
    forbidden = [method for method in methods if method not in ALLOWED_METHODS]
    list_records = [entry for entry in audit if entry["method"] == "thread/list"]
    read_records = [entry for entry in audit if entry["method"] == "thread/read"]
    list_flags_valid = bool(list_records) and all(
        entry["params"].get("useStateDbOnly") is True
        and entry["params"].get("ancestorFilterPresent") is True
        and entry["params"].get("limit") == 100
        and isinstance(entry["params"].get("sourceKindCount"), int)
        and entry["params"]["sourceKindCount"] > 0
        for entry in list_records
    )
    read_flags_valid = bool(read_records) and all(
        entry["params"].get("includeTurns") is False
        and entry["params"].get("hasExactThreadId") is True
        for entry in read_records
    )
    pagination_complete = bool(list_records) and all(
        any(
            entry["method"] == "thread/list"
            and entry["pass"] == pass_number
            and entry.get("nextCursorPresent") is False
            for entry in list_records
        )
        for pass_number in range(1, len(passes) + 1)
    )
    sequences = [
        [entry["method"] for entry in audit if entry["pass"] == pass_number]
        for pass_number in range(1, len(passes) + 1)
    ]
    sequence_valid = all(
        len(sequence) >= 4
        and sequence[:3] == ["initialize", "initialized", "thread/read"]
        and all(method == "thread/list" for method in sequence[3:])
        for sequence in sequences
    )
    if transition is None:
        return blocked(
            args.output,
            "active_to_idle_not_observed",
            sha=sha,
            tree=tree,
            audit=audit,
            passes=passes,
        )
    if (
        forbidden
        or not list_flags_valid
        or not read_flags_valid
        or not pagination_complete
        or not sequence_valid
    ):
        return blocked(
            args.output,
            "observer_method_contract_failed",
            sha=sha,
            tree=tree,
            audit=audit,
            passes=passes,
        )
    try:
        final_status = git_status(args.repo)
        final_sha = git_value(args.repo, "rev-parse", "HEAD")
        final_tree = git_value(args.repo, "rev-parse", "HEAD^{tree}")
    except Exception:
        return blocked(
            args.output,
            "repository_postcheck_unavailable",
            sha=sha,
            tree=tree,
            audit=audit,
            passes=passes,
        )
    if final_status != initial_status or final_sha != sha or final_tree != tree:
        return blocked(
            args.output,
            "repository_mutation_detected",
            sha=sha,
            tree=tree,
            audit=audit,
            passes=passes,
        )

    write_result(
        args.output,
        {
            "schemaVersion": 1,
            "result": "PASS",
            "code": None,
            "sha": sha,
            "tree": tree,
            "transition": transition,
            "passes": passes,
            "audit": audit,
            "methodAllowlist": sorted(set(methods)),
            "forbiddenMethodCount": 0,
            "repositoryMutationCount": 0,
            "assertions": {
                "includeTurnsFalse": True,
                "useStateDbOnlyTrue": True,
                "paginationCompleteEveryPass": True,
                "requestSequenceValidEveryPass": True,
                "descendantActiveToIdleWithinTwoPollIntervals": True,
            },
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
