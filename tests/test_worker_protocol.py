from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
import uuid

from switchstand_worker.protocol import CAPABILITIES, CoordinatorClient, PROTOCOL, ProtocolError, strict_json


WORKER = "10000000-0000-4000-8000-000000000001"
INSTANCE = "20000000-0000-4000-8000-000000000002"
WORK = "work:test-0001"
TOKEN = "A" * 43
BASE = "a" * 40
THREAD_ID = "30000000-0000-4000-8000-000000000003"


def claim_response(**updates):
    value = {
        "protocol": PROTOCOL,
        "work_id": WORK,
        "work_type": "implementation",
        "worker_id": WORKER,
        "instance_id": INSTANCE,
        "fence": 1,
        "lease_token": TOKEN,
        "lease_expires_at": "2026-08-29T12:00:00Z",
        "cancellation_version": 0,
        "admission_sha": "b" * 64,
        "source_text": "Change one bounded fixture.",
        "acceptance": ["The fixture is exact."],
        "repository": {
            "full_name": "owner/repository",
            "base_sha": BASE,
            "candidate_branch": "candidate/test",
            "allowed_path_prefixes": ["allowed"],
        },
        "checkout_path": f"/v2/work/{WORK}/checkout",
        "prior_checkpoint": None,
        "codex_thread_id": None,
        "accepted_candidate": None,
        "limits": {
            "max_files": 32,
            "max_file_bytes": 65536,
            "max_total_bytes": 262144,
            "max_deletions": 32,
            "max_json_bytes": 393216,
        },
    }
    value.update(updates)
    return value


class _Handler(BaseHTTPRequestHandler):
    routes = {}
    requests = []

    def do_POST(self):
        self._handle()

    def do_GET(self):
        self._handle()

    def _handle(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).requests.append((self.command, self.path, dict(self.headers), body))
        status, headers, response = type(self).routes[(self.command, self.path)]
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format, *args):
        return


class ProtocolClientTests(unittest.TestCase):
    def setUp(self):
        _Handler.routes = {}
        _Handler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.client = CoordinatorClient(f"http://127.0.0.1:{self.server.server_port}", "worker-secret")

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def route(self, method, path, status, value, headers=None):
        body = value if isinstance(value, bytes) else json.dumps(value, separators=(",", ":")).encode()
        _Handler.routes[(method, path)] = (status, headers or {"Content-Type": "application/json"}, body)

    def test_register_claim_checkout_and_mutations_use_exact_contract(self):
        self.route(
            "POST",
            "/v2/workers/register",
            200,
            {
                "protocol": PROTOCOL,
                "worker_id": WORKER,
                "poll_after_seconds": 2,
                "lease_seconds": 15,
                "renew_after_seconds": 1,
                "server_time": "2026-08-29T12:00:00Z",
            },
        )
        self.route("POST", "/v2/work/claim", 200, claim_response())
        archive = b"archive"
        self.route(
            "GET",
            f"/v2/work/{WORK}/checkout",
            200,
            archive,
            {
                "Content-Type": "application/gzip",
                "X-Base-Sha": BASE,
                "X-Archive-Sha256": "0" * 64,
            },
        )
        self.route(
            "POST",
            f"/v2/work/{WORK}/renew",
            200,
            {
                "lease_expires_at": "2026-08-29T12:00:15Z",
                "renew_after_seconds": 1,
                "cancellation_version": 0,
            },
        )
        self.route(
            "POST",
            f"/v2/work/{WORK}/checkpoint",
            200,
            {
                "accepted_sequence": 1,
                "lease_expires_at": "2026-08-29T12:00:15Z",
            },
        )
        self.route(
            "POST",
            f"/v2/work/{WORK}/complete",
            200,
            {
                "work_id": WORK,
                "status": "succeeded",
                "completed_at": "2026-08-29T12:00:01Z",
            },
        )
        self.client.register(WORKER, INSTANCE)
        claim = self.client.claim(WORKER, INSTANCE)
        self.assertIsNotNone(claim)
        assert claim is not None
        body, _ = self.client.checkout(claim)
        self.assertEqual(body, archive)
        self.client.renew(claim.authority)
        self.client.checkpoint(
            claim.authority, "checkpoint:00000000-0000-4000-8000-000000000001", 1, "checkout_ready", None, "ready"
        )
        self.client.complete(
            claim.authority,
            "complete:00000000-0000-4000-8000-000000000001",
            status="succeeded",
            candidate_id=str(uuid.uuid4()),
            summary_code="done",
        )
        register = json.loads(_Handler.requests[0][3])
        self.assertEqual(register["capabilities"], CAPABILITIES)
        checkout_headers = _Handler.requests[2][2]
        self.assertEqual(checkout_headers["X-Lease-Fence"], "1")
        self.assertEqual(checkout_headers["Authorization"], "Bearer worker-secret")

    def test_claim_204_and_fixed_error(self):
        self.route("POST", "/v2/work/claim", 204, b"")
        self.assertIsNone(self.client.claim(WORKER, INSTANCE))
        self.route("POST", "/v2/work/claim", 409, {"error": "stale_or_invalid_lease"})
        with self.assertRaises(ProtocolError) as context:
            self.client.claim(WORKER, INSTANCE)
        self.assertEqual((context.exception.status, context.exception.code), (409, "stale_or_invalid_lease"))

    def test_rejects_duplicate_unknown_and_invalid_nested_claim_fields(self):
        duplicate = b'{"protocol":"worker-v2","protocol":"worker-v2"}'
        with self.assertRaisesRegex(ProtocolError, "invalid_request"):
            strict_json(duplicate, maximum=4096)
        for update in (
            {"extra": True},
            {"fence": True},
            {"lease_token": "short"},
            {"accepted_candidate": {"candidate_id": str(uuid.uuid4()), "manifest_sha": "z" * 64}},
            {"limits": {"max_files": 33}},
        ):
            value = claim_response(**update)
            self.route("POST", "/v2/work/claim", 200, value)
            with self.subTest(update=update), self.assertRaises(ProtocolError):
                self.client.claim(WORKER, INSTANCE)

    def test_rejects_invalid_claim_authority_state_and_malformed_nested_values(self):
        invalid_checkpoint = {
            "sequence": 2,
            "phase": "working",
            "codex_thread_id": None,
            "checkpoint_state": "finite_turn_started",
        }
        cases = (
            {"lease_expires_at": "2026-99-99T99:99:99Z"},
            {"prior_checkpoint": invalid_checkpoint},
            {
                "repository": {
                    **claim_response()["repository"],
                    "allowed_path_prefixes": ["allowed//nested"],
                }
            },
            {
                "repository": {
                    **claim_response()["repository"],
                    "candidate_branch": "candidate/.hidden",
                }
            },
            {
                "repository": {
                    **claim_response()["repository"],
                    "allowed_path_prefixes": [{"not": "a string"}],
                }
            },
            {
                "prior_checkpoint": {
                    "sequence": 2,
                    "phase": [],
                    "codex_thread_id": THREAD_ID,
                    "checkpoint_state": "bad",
                },
                "codex_thread_id": THREAD_ID,
            },
        )
        for update in cases:
            self.route("POST", "/v2/work/claim", 200, claim_response(**update))
            with self.subTest(update=update), self.assertRaisesRegex(ProtocolError, "invalid_request"):
                self.client.claim(WORKER, INSTANCE)

    def test_outbound_body_limit_fails_before_request(self):
        with self.assertRaisesRegex(ProtocolError, "request_too_large"):
            self.client._json("POST", "/never", {"value": "x" * 4096}, 100)
        self.assertEqual(_Handler.requests, [])

    def test_redirect_is_not_followed_with_worker_bearer(self):
        self.route(
            "POST",
            "/v2/work/claim",
            302,
            {"error": "temporary_failure"},
            {"Content-Type": "application/json", "Location": "http://127.0.0.1:1/capture"},
        )
        with self.assertRaises(ProtocolError) as context:
            self.client.claim(WORKER, INSTANCE)
        self.assertEqual(context.exception.code, "temporary_failure")
        self.assertEqual(len(_Handler.requests), 1)

    def test_non_loopback_plain_http_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "requires HTTPS"):
            CoordinatorClient("http://coordinator.example", "worker-secret")


if __name__ == "__main__":
    unittest.main()
