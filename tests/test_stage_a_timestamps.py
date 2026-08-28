from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from stage_a_probe_support import ProbeClient
from switchstand.stage_a_probe import collect_evidence, main


class StageATimestampTests(unittest.TestCase):
    def test_invalid_protocol_timestamps_fail_with_safe_strict_json(self):
        def reject_json_constant(value: str) -> None:
            raise AssertionError(f"non-JSON numeric constant emitted: {value}")

        for invalid in (True, -1, float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid=invalid):
                client = ProbeClient()
                client.root["thread"]["updatedAt"] = invalid
                stdout = io.StringIO()

                with patch("switchstand.stage_a_probe.CodexAppServer", return_value=client):
                    with redirect_stdout(stdout):
                        exit_code = main(
                            [
                                "--app-server-socket",
                                "/private/operator/app-server.sock",
                                "--root-thread-id",
                                "root-1",
                            ]
                        )

                result = json.loads(
                    stdout.getvalue(), parse_constant=reject_json_constant
                )
                self.assertEqual(exit_code, 4)
                self.assertEqual(
                    result["error"]["code"], "missing_protocol_timestamp"
                )
                self.assertEqual(
                    result["error"]["phase"], "timestamp_validation"
                )

    def test_zero_protocol_timestamps_remain_strict_json_evidence(self):
        client = ProbeClient()
        client.root["thread"]["createdAt"] = 0
        client.root["thread"]["updatedAt"] = 0.0
        client.pages[0]["data"][0]["createdAt"] = 0.0
        client.pages[0]["data"][0]["updatedAt"] = 0

        evidence = collect_evidence(client, "root-1")

        emitted = json.dumps(evidence, allow_nan=False)
        parsed = json.loads(emitted)
        threads = parsed["snapshots"][0]["threads"]
        self.assertEqual((threads[0]["createdAt"], threads[0]["updatedAt"]), (0, 0.0))
        self.assertEqual((threads[1]["createdAt"], threads[1]["updatedAt"]), (0.0, 0))


if __name__ == "__main__":
    unittest.main()
