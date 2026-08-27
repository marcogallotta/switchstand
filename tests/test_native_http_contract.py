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

    def test_valid_loopback_host_authorities(self):
        for host in (
            "localhost",
            "LOCALHOST:4180",
            "127.0.0.1",
            "127.0.0.2:65535",
            "[::1]",
            "[0:0:0:0:0:0:0:1]:0",
        ):
            with self.subTest(host=host):
                self.assertTrue(contract.is_loopback_host(host))

    def test_invalid_or_non_loopback_host_authorities(self):
        for label, host in (
            ("missing", None),
            ("empty", ""),
            ("hostname", "example.com"),
            ("ipv4", "0.0.0.0:4180"),
            ("ipv6", "[::2]:4180"),
            ("userinfo", "user@localhost:4180"),
            ("path", "localhost:4180/path"),
            ("query", "localhost:4180?query"),
            ("fragment", "localhost:4180#fragment"),
            ("scheme", "http://localhost:4180"),
            ("backslash", "localhost:4180\\path"),
            ("leading whitespace", " localhost:4180"),
            ("interior whitespace", "local host:4180"),
            ("control", "localhost:4180\x7f"),
            ("missing bracket", "[::1:4180"),
            ("extra bracket", "[::1]]:4180"),
            ("bracketed ipv4", "[127.0.0.1]:4180"),
            ("unbracketed ipv6", "::1"),
            ("empty port", "localhost:"),
            ("nonnumeric port", "localhost:abc"),
            ("negative port", "localhost:-1"),
            ("multiple ports", "localhost:80:90"),
            ("large port", "localhost:65536"),
            ("oversized port text", f"localhost:{'9' * 5000}"),
            ("unicode port", "localhost:\u0664\u0661\u0668\u0660"),
        ):
            with self.subTest(label=label, host=host):
                self.assertFalse(contract.is_loopback_host(host))

    def test_origin_is_absent_or_exact_http_host(self):
        for host in ("localhost", "LOCALHOST:4180", "127.0.0.1:80", "[::1]:4180"):
            with self.subTest(host=host, origin=None):
                self.assertTrue(contract.is_same_origin_http(None, host))
            with self.subTest(host=host, origin=f"http://{host}"):
                self.assertTrue(contract.is_same_origin_http(f"http://{host}", host))

    def test_malformed_cross_origin_and_normalized_origins_are_rejected(self):
        host = "LOCALHOST:04180"
        for label, origin in (
            ("null", "null"),
            ("non-http", f"https://{host}"),
            ("uppercase scheme", f"HTTP://{host}"),
            ("userinfo", f"http://user@{host}"),
            ("path", f"http://{host}/"),
            ("query", f"http://{host}?query"),
            ("fragment", f"http://{host}#fragment"),
            ("leading whitespace", f" http://{host}"),
            ("trailing whitespace", f"http://{host} "),
            ("control", f"http://{host}\x00"),
            ("cross-origin", "http://localhost:9"),
            ("host case normalized", "http://localhost:04180"),
            ("port normalized", "http://LOCALHOST:4180"),
        ):
            with self.subTest(label=label, origin=origin):
                self.assertFalse(contract.is_same_origin_http(origin, host))

    def test_origin_rejects_missing_or_invalid_host_even_when_absent(self):
        for host in (None, "", "example.com", "localhost/path"):
            with self.subTest(host=host):
                self.assertFalse(contract.is_same_origin_http(None, host))

    def test_service_consumes_the_exact_shared_predicates(self):
        self.assertIs(service._loopback, contract.is_loopback_host)
        self.assertIs(service._same_origin_http, contract.is_same_origin_http)


if __name__ == "__main__":
    unittest.main()
