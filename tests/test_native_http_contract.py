from __future__ import annotations

import json
import unittest

from switchstand import native_http_contract as contract
from switchstand import service


class NativeHttpContractTests(unittest.TestCase):
    def test_exact_constants_and_safe_bodies_are_shared(self):
        self.assertEqual(contract.MAX_BODY_BYTES, 64 * 1024)
        self.assertEqual(contract.CONTROL_HEADER_NAME, "X-Switchstand-Control")
        self.assertEqual(
            (
                contract.NATIVE_SELECTION_CONTROL_VALUE,
                contract.NATIVE_INPUT_CONTROL_VALUE,
                contract.NATIVE_STOP_CONTROL_VALUE,
            ),
            ("native-selection-v1", "native-input-v1", "native-stop-v1"),
        )
        self.assertIs(service.MAX_BODY_BYTES, contract.MAX_BODY_BYTES)
        self.assertIs(service.CONTROL_HEADER_NAME, contract.CONTROL_HEADER_NAME)
        self.assertIs(service.NATIVE_HEADER, contract.NATIVE_STOP_CONTROL_VALUE)
        self.assertEqual(
            json.dumps(contract.CONTROL_REQUEST_REJECTED_BODY, sort_keys=True),
            '{"code": "control_request_rejected", "outcome": "not_sent"}',
        )
        self.assertEqual(
            json.dumps(contract.INVALID_REQUEST_BODY, sort_keys=True),
            '{"code": "invalid_request", "outcome": "not_sent"}',
        )

    def test_loopback_and_same_origin_predicates_preserve_service_behavior(self):
        for host, expected in (
            ("localhost:4180", True),
            ("127.0.0.1:4180", True),
            ("[::1]:4180", True),
            ("0.0.0.0:4180", False),
            ("example.com", False),
            (None, False),
        ):
            with self.subTest(host=host):
                self.assertEqual(contract.is_loopback_host(host), expected)
                self.assertEqual(service._loopback(host), expected)

        host = "127.0.0.1:4180"
        for origin, expected in (
            (None, True),
            ("http://127.0.0.1:4180", True),
            ("HTTP://127.0.0.1:4180", True),
            ("null", False),
            ("https://127.0.0.1:4180", False),
            ("http://127.0.0.1:9", False),
            ("not an origin", False),
        ):
            with self.subTest(origin=origin):
                self.assertEqual(contract.is_same_origin_http(origin, host), expected)
                self.assertEqual(service._same_origin_http(origin, host), expected)


if __name__ == "__main__":
    unittest.main()
