from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch


REPO = Path(__file__).parents[1]
RUNNER_PATH = REPO / "scripts" / "stage_b1_live_check.py"
SPEC = importlib.util.spec_from_file_location("stage_b1_live_check", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)

ROOT_ID = "private-root-id"
CHILD_ID = "private-child-id"


def native_thread(thread_id: str, parent: str | None, status: str) -> dict[str, Any]:
    return {
        "id": thread_id,
        "sessionId": f"private-session-{thread_id}",
        "parentThreadId": parent,
        "source": "cli" if parent is None else {"subAgent": "thread_spawn"},
        "createdAt": 100.0,
        "updatedAt": 101.0,
        "status": {"type": status, **({"activeFlags": []} if status == "active" else {})},
    }


class FakeAuditedClient:
    forbidden_method: str | None = None

    def __init__(self, socket_path: Path, pass_number: int, audit: list[dict[str, Any]]) -> None:
        del socket_path
        self.pass_number = pass_number
        self.audit = audit

    def thread_read(self, thread_id: str, *, include_turns: bool = True):
        self.audit.extend(
            [
                {"pass": self.pass_number, "method": "initialize", "params": {}},
                {"pass": self.pass_number, "method": "initialized", "params": {}},
            ]
        )
        if self.forbidden_method is not None:
            self.audit.append(
                {"pass": self.pass_number, "method": self.forbidden_method, "params": {}}
            )
        self.audit.append(
            {
                "pass": self.pass_number,
                "method": "thread/read",
                "params": {"includeTurns": include_turns, "hasExactThreadId": True},
            }
        )
        status = "active" if self.pass_number == 1 else "idle"
        return {"thread": native_thread(thread_id, None, status)}

    def thread_list(self, params):
        self.audit.append(
            {
                "pass": self.pass_number,
                "method": "thread/list",
                "params": runner.safe_params("thread/list", params),
                "resultCount": 1,
                "nextCursorPresent": False,
            }
        )
        status = "active" if self.pass_number == 1 else "idle"
        return {"data": [native_thread(CHILD_ID, ROOT_ID, status)], "nextCursor": None}

    def close(self):
        return None


class StageB1LiveCheckTests(unittest.TestCase):
    def run_check(self, client_class: type[FakeAuditedClient]):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "app-server.sock"
            socket_path.touch()
            output = Path(directory) / "evidence.json"
            sha = runner.git_value(REPO, "rev-parse", "HEAD")
            tree = runner.git_value(REPO, "rev-parse", "HEAD^{tree}")
            arguments = [
                "stage_b1_live_check.py",
                "--repo",
                str(REPO),
                "--socket",
                str(socket_path),
                "--root-thread-id",
                ROOT_ID,
                "--expected-sha",
                sha,
                "--expected-tree",
                tree,
                "--output",
                str(output),
                "--interval",
                "0.001",
                "--max-passes",
                "3",
            ]
            with patch.object(runner, "AuditedClient", client_class), patch.object(
                sys, "argv", arguments
            ):
                exit_code = runner.main()
            return exit_code, json.loads(output.read_text()), output.read_text()

    def test_passes_only_for_exact_read_only_active_to_idle_evidence(self):
        exit_code, evidence, emitted = self.run_check(FakeAuditedClient)

        self.assertEqual(exit_code, 0)
        self.assertEqual(evidence["result"], "PASS")
        self.assertEqual(evidence["transition"]["pollIntervals"], 1)
        self.assertEqual(evidence["forbiddenMethodCount"], 0)
        self.assertTrue(all(evidence["assertions"].values()))
        self.assertNotIn(ROOT_ID, emitted)
        self.assertNotIn(CHILD_ID, emitted)
        self.assertNotIn("private-session", emitted)

    def test_forbidden_observer_method_fails_closed(self):
        class ForbiddenClient(FakeAuditedClient):
            forbidden_method = "thread/resume"

        exit_code, evidence, _ = self.run_check(ForbiddenClient)

        self.assertEqual(exit_code, 2)
        self.assertEqual(evidence["result"], "BLOCKED")
        self.assertEqual(evidence["code"], "observer_method_contract_failed")
        self.assertGreater(evidence["forbiddenMethodCount"], 0)


if __name__ == "__main__":
    unittest.main()
