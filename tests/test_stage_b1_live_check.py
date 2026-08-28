from __future__ import annotations

from contextlib import redirect_stderr
import importlib.util
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from switchstand.app_server import CodexAppServer


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
    bounded_method: str | None = None
    record_turn_probe = True
    turn_probe_copies = 1
    turn_probe_overrides: dict[str, Any] | None = None
    statuses = ("active", "idle")

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
        status = self.statuses[min(self.pass_number - 1, len(self.statuses) - 1)]
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
        status = self.statuses[min(self.pass_number - 1, len(self.statuses) - 1)]
        return {"data": [native_thread(CHILD_ID, ROOT_ID, status)], "nextCursor": None}

    def stop_request(self, method, params, **kwargs):
        del kwargs
        if self.bounded_method is not None:
            self.audit.append(
                {
                    "pass": self.pass_number,
                    "method": self.bounded_method,
                    "params": {},
                }
            )
        if self.record_turn_probe:
            turn_params = dict(params)
            if self.turn_probe_overrides is not None:
                turn_params.update(self.turn_probe_overrides)
            for _ in range(self.turn_probe_copies):
                self.audit.append(
                    {
                        "pass": self.pass_number,
                        "method": method,
                        "params": runner.safe_params(method, turn_params),
                    }
                )
        return "ok", {"data": []}

    def close(self):
        return None


class StageB1LiveCheckTests(unittest.TestCase):
    def test_audits_each_bounded_transport_entry_once_without_sensitive_ids(self):
        audit = []
        client = runner.AuditedClient.__new__(runner.AuditedClient)
        client._pass_number = 7
        client._audit = audit
        turn_params = {
            "threadId": ROOT_ID,
            "limit": 1,
            "sortDirection": "desc",
            "itemsView": "notLoaded",
        }

        with patch.object(
            CodexAppServer, "bounded_request", return_value=("ok", {})
        ) as transport:
            client.stop_request("thread/turns/list", turn_params)
            client.bounded_request("turn/start", {"threadId": CHILD_ID})

        self.assertEqual(transport.call_count, 2)
        self.assertEqual(
            [entry["method"] for entry in audit],
            ["thread/turns/list", "turn/start"],
        )
        self.assertEqual(
            audit[0]["params"],
            {
                "hasExactThreadId": True,
                "limit": 1,
                "sortDirection": "desc",
                "itemsView": "notLoaded",
                "exactParameterNames": True,
            },
        )
        self.assertNotIn(ROOT_ID, json.dumps(audit))
        self.assertNotIn(CHILD_ID, json.dumps(audit))

    def test_rejects_resolved_repository_output_without_writing_or_mutation(self):
        invalid_output = REPO / ".stage-b1-invalid-evidence.json"
        self.assertFalse(invalid_output.exists())

        for output in (REPO, invalid_output):
            with self.subTest(output=output.name):
                initial_status = runner.git_status(REPO)
                initial_stat = output.stat() if output.exists() else None
                arguments = [
                    "stage_b1_live_check.py",
                    "--repo",
                    str(REPO),
                    "--socket",
                    str(REPO / "missing.sock"),
                    "--root-thread-id",
                    ROOT_ID,
                    "--expected-sha",
                    "unused",
                    "--expected-tree",
                    "unused",
                    "--output",
                    str(output),
                ]
                stderr = StringIO()

                with patch.object(sys, "argv", arguments), redirect_stderr(stderr):
                    exit_code = runner.main()

                self.assertEqual(exit_code, 2)
                self.assertEqual(
                    json.loads(stderr.getvalue()),
                    {
                        "schemaVersion": 1,
                        "result": "BLOCKED",
                        "code": "output_path_inside_repository",
                    },
                )
                self.assertEqual(runner.git_status(REPO), initial_status)
                if initial_stat is None:
                    self.assertFalse(output.exists())
                else:
                    self.assertEqual(output.stat(), initial_stat)

    def run_check(self, client_class: type[FakeAuditedClient]):
        with tempfile.TemporaryDirectory(dir=REPO.parent) as directory:
            socket_path = Path(directory) / "app-server.sock"
            socket_path.touch()
            output = Path(directory) / "evidence.json"
            self.assertFalse(output.resolve().is_relative_to(REPO.resolve()))
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

    def test_unauthorized_bounded_method_fails_closed(self):
        class ForbiddenBoundedClient(FakeAuditedClient):
            bounded_method = "turn/start"

        exit_code, evidence, _ = self.run_check(ForbiddenBoundedClient)

        self.assertEqual(exit_code, 2)
        self.assertEqual(evidence["code"], "observer_method_contract_failed")
        self.assertEqual(evidence["forbiddenMethodCount"], 2)

    def test_missing_authorized_turn_probe_fails_closed(self):
        class MissingTurnProbeClient(FakeAuditedClient):
            record_turn_probe = False

        exit_code, evidence, _ = self.run_check(MissingTurnProbeClient)

        self.assertEqual(exit_code, 2)
        self.assertEqual(evidence["code"], "observer_method_contract_failed")
        self.assertEqual(evidence["forbiddenMethodCount"], 0)

    def test_duplicate_authorized_turn_probe_fails_closed(self):
        class DuplicateTurnProbeClient(FakeAuditedClient):
            turn_probe_copies = 2

        exit_code, evidence, _ = self.run_check(DuplicateTurnProbeClient)

        self.assertEqual(exit_code, 2)
        self.assertEqual(evidence["code"], "observer_method_contract_failed")
        self.assertEqual(evidence["forbiddenMethodCount"], 0)

    def test_inexact_turn_probe_params_fail_closed(self):
        class InexactTurnProbeClient(FakeAuditedClient):
            turn_probe_overrides = {"limit": 2}

        exit_code, evidence, _ = self.run_check(InexactTurnProbeClient)

        self.assertEqual(exit_code, 2)
        self.assertEqual(evidence["code"], "observer_method_contract_failed")
        self.assertEqual(evidence["forbiddenMethodCount"], 0)

    def test_non_integer_turn_probe_limits_fail_closed(self):
        for malformed_limit in (True, 1.0):
            with self.subTest(limit=repr(malformed_limit)):
                class InexactTurnProbeClient(FakeAuditedClient):
                    turn_probe_overrides = {"limit": malformed_limit}

                exit_code, evidence, _ = self.run_check(InexactTurnProbeClient)

                self.assertEqual(exit_code, 2)
                self.assertEqual(evidence["code"], "observer_method_contract_failed")
                self.assertEqual(evidence["forbiddenMethodCount"], 0)

    def test_root_loaded_by_another_server_fails_after_first_pass(self):
        class NotLoadedClient(FakeAuditedClient):
            statuses = ("notLoaded",)

        exit_code, evidence, _ = self.run_check(NotLoadedClient)

        self.assertEqual(exit_code, 2)
        self.assertEqual(evidence["code"], "root_not_loaded_on_observer_server")
        self.assertEqual(len(evidence["passes"]), 1)
        self.assertEqual(evidence["forbiddenMethodCount"], 0)


if __name__ == "__main__":
    unittest.main()
