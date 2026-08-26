from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from switchstand.app_server import CodexAppServer
from switchstand.engine import CodexAdapter, _message_marker


class AppServerProtocolTests(unittest.TestCase):
    def test_client_constructs_normal_turn_and_exact_interrupt_requests(self):
        client = object.__new__(CodexAppServer)
        calls = []
        client._request = lambda method, params: calls.append((method, params)) or (
            {"turn": {"id": "turn-1"}} if method == "turn/start" else {}
        )

        response = client.turn_start_text(
            "thread-1",
            "direct message",
            sandbox_policy={"type": "readOnly"},
        )
        client.turn_interrupt("thread-1", "turn-1")

        self.assertEqual(response["turn"]["id"], "turn-1")
        self.assertEqual(
            calls,
            [
                (
                    "turn/start",
                    {
                        "threadId": "thread-1",
                        "input": [{"type": "text", "text": "direct message"}],
                        "approvalPolicy": "never",
                        "sandboxPolicy": {"type": "readOnly"},
                    },
                ),
                ("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-1"}),
            ],
        )

    def test_adapter_uses_persisted_thread_direct_turn_and_exact_interrupt(self):
        calls = []

        class Client:
            def __init__(self, socket_path, **kwargs):
                calls.append(("connect", Path(socket_path), kwargs))

            def thread_start(self, params):
                calls.append(("thread/start", params))
                return {"thread": {"id": "thread-live"}}

            def thread_name_set(self, thread_id, name):
                calls.append(("thread/name/set", thread_id, name))
                return {}

            def thread_resume(self, thread_id):
                calls.append(("thread/resume", thread_id))
                return {"thread": {"id": thread_id}}

            def turn_start_text(self, thread_id, text, **kwargs):
                calls.append(("turn/start", thread_id, text, kwargs))
                return {"turn": {"id": "turn-live"}}

            def turn_interrupt(self, thread_id, turn_id):
                calls.append(("turn/interrupt", thread_id, turn_id))
                return {}

            def close(self):
                calls.append(("close",))

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with patch("switchstand.engine.CodexAppServer", Client):
                adapter = CodexAdapter(root / "app-server.sock", cwd=root)
                thread_id = adapter.create_attempt(
                    role={"id": "role-a", "name": "Design"},
                    context={"checkpoint": {"latest_correction": "keep scope narrow"}},
                )
                turn_id = adapter.start_message(
                    thread_id=thread_id,
                    message={"id": "message-1", "text": "direct message"},
                )
                adapter.interrupt(thread_id=thread_id, turn_id=turn_id)

        self.assertEqual(thread_id, "thread-live")
        self.assertEqual(turn_id, "turn-live")
        thread_start = next(item for item in calls if item[0] == "thread/start")
        self.assertFalse(thread_start[1]["ephemeral"])
        self.assertEqual(thread_start[1]["serviceName"], "Switchstand")
        turn_start = next(item for item in calls if item[0] == "turn/start")
        self.assertNotIn("client_user_message_id", turn_start[3])
        self.assertEqual(
            turn_start[2],
            f"direct message\n\n{_message_marker('message-1')}",
        )
        self.assertIn(("turn/interrupt", "thread-live", "turn-live"), calls)

    def test_turn_result_preserves_unknown_and_extracts_final_output(self):
        unknown = CodexAdapter._turn_result({"status": "mystery", "items": []})
        completed = CodexAdapter._turn_result(
            {
                "status": "completed",
                "items": [
                    {"type": "agentMessage", "phase": "commentary", "text": "partial"},
                    {"type": "agentMessage", "phase": "final_answer", "text": "final"},
                ],
            }
        )
        self.assertEqual(unknown, {"status": "mystery", "output": None})
        self.assertEqual(completed, {"status": "completed", "output": "final"})

    def test_inspection_uses_only_official_user_message_content(self):
        marker = _message_marker("message-1")
        adapter = object.__new__(CodexAdapter)
        adapter._read = lambda thread_id: {
            "thread": {
                "status": {"type": "idle"},
                "turns": [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [
                            {
                                "type": "userMessage",
                                "id": "user-1",
                                "clientId": "wrong-private-id",
                                "content": [{"type": "text", "text": f"hello\n\n{marker}"}],
                            },
                            {
                                "type": "agentMessage",
                                "id": "agent-1",
                                "phase": "final_answer",
                                "text": f"clean output {marker}",
                            },
                        ],
                    }
                ],
            }
        }

        observed = adapter.inspect_message(thread_id="thread-1", message_id="message-1")

        self.assertEqual(
            observed,
            {"found": True, "turn_id": "turn-1", "status": "completed", "output": "clean output"},
        )


if __name__ == "__main__":
    unittest.main()
