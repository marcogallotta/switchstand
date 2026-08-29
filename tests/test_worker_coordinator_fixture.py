from __future__ import annotations

import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
import time
import unittest
import uuid

from switchstand_worker.protocol import CoordinatorClient, ProtocolError, canonical_json
from switchstand_worker.supervisor import ProcessResult, Worker, WorkerConfig


WORKER = "10000000-0000-4000-8000-000000000001"
INSTANCE = "20000000-0000-4000-8000-000000000002"
THREAD = "30000000-0000-4000-8000-000000000003"
BASE = "a" * 40


def checkout_archive():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as output:
        for name in ("root", "root/allowed"):
            item = tarfile.TarInfo(name)
            item.type = tarfile.DIRTYPE
            output.addfile(item)
        data = b"base\n"
        item = tarfile.TarInfo("root/allowed/base.txt")
        item.size = len(data)
        output.addfile(item, io.BytesIO(data))
    return gzip.compress(buffer.getvalue(), mtime=0)


class CoordinatorFixture:
    def __init__(self):
        self.lock = threading.Lock()
        self.work_id = "work:fixture-01"
        self.state = "queued"
        self.fence = 0
        self.token = None
        self.worker_id = None
        self.instance_id = None
        self.expiry = 0.0
        self.cancellation_version = 0
        self.sequence = 0
        self.thread_id = None
        self.accepted_candidate = None
        self.receipts = {}
        self.archive = checkout_archive()

    def authority(self, value):
        return (
            value.get("work_id") == self.work_id
            and value.get("worker_id") == self.worker_id
            and value.get("instance_id") == self.instance_id
            and value.get("fence") == self.fence
            and value.get("lease_token") == self.token
            and value.get("cancellation_version") == self.cancellation_version
            and time.monotonic() < self.expiry
            and self.state != "terminal"
        )

    def force_expire(self):
        with self.lock:
            self.expiry = 0

    def claim(self, body):
        with self.lock:
            if self.state == "terminal":
                return 204, None
            if self.state in {"leased", "candidate_ready"} and time.monotonic() < self.expiry:
                return 204, None
            self.fence += 1
            self.worker_id = body["worker_id"]
            self.instance_id = body["instance_id"]
            self.token = ("A" if self.fence == 1 else "B") * 43
            self.expiry = time.monotonic() + 15
            if self.state == "queued":
                self.state = "leased"
            prior = None
            if self.sequence:
                prior = {
                    "sequence": self.sequence,
                    "phase": "codex_started",
                    "codex_thread_id": self.thread_id,
                    "checkpoint_state": "thread_adopted",
                }
            return 200, {
                "protocol": "worker-v2",
                "work_id": self.work_id,
                "work_type": "implementation",
                "worker_id": self.worker_id,
                "instance_id": self.instance_id,
                "fence": self.fence,
                "lease_token": self.token,
                "lease_expires_at": "2026-08-29T12:00:15Z",
                "cancellation_version": self.cancellation_version,
                "admission_sha": "b" * 64,
                "source_text": "write one bounded fixture",
                "acceptance": ["one file is changed"],
                "repository": {
                    "full_name": "owner/repo",
                    "base_sha": BASE,
                    "candidate_branch": "candidate/test",
                    "allowed_path_prefixes": ["allowed"],
                },
                "checkout_path": f"/v2/work/{self.work_id}/checkout",
                "prior_checkpoint": prior,
                "codex_thread_id": self.thread_id,
                "accepted_candidate": self.accepted_candidate,
                "limits": {
                    "max_files": 32,
                    "max_file_bytes": 65536,
                    "max_total_bytes": 262144,
                    "max_deletions": 32,
                    "max_json_bytes": 393216,
                },
            }

    def mutate(self, kind, body):
        with self.lock:
            operation = body.get("operation_id")
            semantic = {
                key: value
                for key, value in body.items()
                if key
                not in {
                    "protocol",
                    "work_id",
                    "worker_id",
                    "instance_id",
                    "fence",
                    "lease_token",
                    "cancellation_version",
                    "request_digest",
                }
            }
            digest = hashlib.sha256(canonical_json(semantic)).hexdigest()
            receipt = self.receipts.get((kind, operation))
            if receipt is not None:
                if receipt[0] != digest:
                    return 409, {"error": "idempotency_conflict"}
                original = receipt[1]
                authority = tuple(
                    body.get(key)
                    for key in (
                        "worker_id",
                        "instance_id",
                        "fence",
                        "lease_token",
                        "cancellation_version",
                    )
                )
                if authority != original:
                    return 409, {"error": "stale_or_invalid_lease"}
                return 200, receipt[2]
            if self.state == "terminal":
                return 409, {"error": "terminal_immutable"}
            if not self.authority(body):
                return 409, {"error": "stale_or_invalid_lease"}
            original = tuple(
                body[key]
                for key in (
                    "worker_id",
                    "instance_id",
                    "fence",
                    "lease_token",
                    "cancellation_version",
                )
            )
            if kind == "checkpoint":
                if body["sequence"] <= self.sequence:
                    return 400, {"error": "invalid_request"}
                self.sequence = body["sequence"]
                self.thread_id = body["codex_thread_id"]
                response = {"accepted_sequence": self.sequence, "lease_expires_at": "2026-08-29T12:00:15Z"}
            elif kind == "candidate":
                self.accepted_candidate = {
                    "candidate_id": str(uuid.uuid4()),
                    "manifest_sha": hashlib.sha256(canonical_json(body)).hexdigest(),
                }
                self.state = "candidate_ready"
                response = {**self.accepted_candidate, "status": "candidate_ready"}
            else:
                self.state = "terminal"
                response = {"work_id": self.work_id, "status": body["status"], "completed_at": "2026-08-29T12:00:01Z"}
            self.receipts[(kind, operation)] = (digest, original, response)
            return 200, response


class Handler(BaseHTTPRequestHandler):
    fixture = CoordinatorFixture()

    def do_GET(self):
        fixture = type(self).fixture
        with fixture.lock:
            authority = {
                "work_id": fixture.work_id,
                "worker_id": self.headers.get("X-Worker-Id"),
                "instance_id": self.headers.get("X-Instance-Id"),
                "fence": int(self.headers.get("X-Lease-Fence", "0")),
                "lease_token": self.headers.get("X-Lease-Token"),
                "cancellation_version": int(self.headers.get("X-Cancellation-Version", "-1")),
            }
            if not fixture.authority(authority):
                self.reply(409, {"error": "stale_or_invalid_lease"})
                return
            payload = fixture.archive
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Base-Sha", BASE)
        self.send_header("X-Archive-Sha256", hashlib.sha256(payload).hexdigest())
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        fixture = type(self).fixture
        if self.path == "/v2/workers/register":
            status, result = (
                200,
                {
                    "protocol": "worker-v2",
                    "worker_id": body["worker_id"],
                    "poll_after_seconds": 2,
                    "lease_seconds": 15,
                    "renew_after_seconds": 1,
                    "server_time": "2026-08-29T12:00:00Z",
                },
            )
        elif self.path == "/v2/work/claim":
            status, result = fixture.claim(body)
        elif self.path.endswith("/renew"):
            with fixture.lock:
                if fixture.authority(body):
                    fixture.expiry = time.monotonic() + 15
                    status, result = (
                        200,
                        {
                            "lease_expires_at": "2026-08-29T12:00:15Z",
                            "renew_after_seconds": 1,
                            "cancellation_version": fixture.cancellation_version,
                        },
                    )
                else:
                    status, result = 409, {"error": "stale_or_invalid_lease"}
        else:
            kind = self.path.rsplit("/", 1)[-1]
            status, result = fixture.mutate(kind, body)
        self.reply(status, result)

    def reply(self, status, value):
        if status == 204:
            data = b""
        else:
            data = canonical_json(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        return


class FakeRunner:
    def __init__(self, config, claim, guard):
        pass

    def bootstrap(self):
        return THREAD

    def task_turn(self, workspace, thread_id):
        (workspace / "allowed/result.txt").write_text("result\n")
        return ProcessResult(0, thread_id, None)


class CoordinatorFixtureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = CoordinatorFixture()
        Handler.fixture = self.fixture
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.client = CoordinatorClient(f"http://127.0.0.1:{self.server.server_port}", "worker-secret")

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def test_full_worker_journey_over_deterministic_http_fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = WorkerConfig(
                "unused",
                "worker-secret",
                Path(temporary),
                WORKER,
                INSTANCE,
                Path("/bin/true"),
                Path("/dev/null"),
                Path("/usr/bin/bwrap"),
            )
            self.assertTrue(Worker(config, client=self.client, runner_factory=FakeRunner).run_once())
        self.assertEqual(self.fixture.state, "terminal")
        self.assertEqual(self.fixture.fence, 1)
        self.assertIsNotNone(self.fixture.accepted_candidate)

    def test_idempotency_precedence_higher_fence_and_stale_writes(self):
        claim = self.client.claim(WORKER, INSTANCE)
        assert claim is not None
        operation = "checkpoint:00000000-0000-4000-8000-000000000001"
        original = self.client.checkpoint(claim.authority, operation, 1, "codex_started", THREAD, "thread_adopted")
        self.assertEqual(
            self.client.checkpoint(claim.authority, operation, 1, "codex_started", THREAD, "thread_adopted"), original
        )
        with self.assertRaises(ProtocolError) as changed:
            self.client.checkpoint(claim.authority, operation, 2, "working", THREAD, "changed")
        self.assertEqual(changed.exception.code, "idempotency_conflict")
        self.fixture.force_expire()
        reclaimed = self.client.claim(WORKER, INSTANCE)
        assert reclaimed is not None
        self.assertEqual(reclaimed.authority.fence, 2)
        with self.assertRaises(ProtocolError) as stale:
            self.client.checkpoint(reclaimed.authority, operation, 1, "codex_started", THREAD, "thread_adopted")
        self.assertEqual(stale.exception.code, "stale_or_invalid_lease")
        with self.assertRaises(ProtocolError):
            self.client.complete(claim.authority, "complete:old-fence", status="failed", summary_code="stale")
        with self.assertRaises(ProtocolError) as stale_candidate:
            self.client.submit_candidate(
                claim.authority,
                {
                    "operation_id": "candidate:old-fence",
                    "base_sha": BASE,
                    "expected_branch_head": BASE,
                    "message": "stale",
                    "files": [],
                    "deletions": [{"path": "allowed/base.txt"}],
                    "check_summaries": [],
                    "request_digest": "c" * 64,
                },
            )
        self.assertEqual(stale_candidate.exception.code, "stale_or_invalid_lease")


if __name__ == "__main__":
    unittest.main()
