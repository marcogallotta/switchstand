from __future__ import annotations

import gzip
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from switchstand_worker.protocol import Authority, Claim, ProtocolError
from switchstand_worker.__main__ import main
from switchstand_worker.supervisor import CodexRunner, LeaseGuard, ProcessResult, Worker, WorkerConfig


THREAD = "30000000-0000-4000-8000-000000000003"
CANDIDATE = "40000000-0000-4000-8000-000000000004"


def make_claim(*, thread_id=None, prior=None, accepted=None, fence=1, work_type="implementation"):
    authority = Authority(
        "work:test-0001",
        "10000000-0000-4000-8000-000000000001",
        "20000000-0000-4000-8000-000000000002",
        fence,
        "A" * 43,
        0,
    )
    return Claim(
        authority,
        work_type,
        "2026-08-29T12:00:00Z",
        "b" * 64,
        "make a bounded change",
        ("one file",),
        {
            "full_name": "owner/repo",
            "base_sha": "a" * 40,
            "candidate_branch": "candidate/test",
            "allowed_path_prefixes": ["allowed"],
        },
        "/v2/work/work:test-0001/checkout",
        prior,
        thread_id,
        accepted,
        {
            "max_files": 32,
            "max_file_bytes": 65536,
            "max_total_bytes": 262144,
            "max_deletions": 32,
            "max_json_bytes": 393216,
        },
    )


def checkout_archive():
    tar = io.BytesIO()
    with tarfile.open(fileobj=tar, mode="w") as output:
        root = tarfile.TarInfo("root")
        root.type = tarfile.DIRTYPE
        output.addfile(root)
        directory = tarfile.TarInfo("root/allowed")
        directory.type = tarfile.DIRTYPE
        output.addfile(directory)
        info = tarfile.TarInfo("root/allowed/base.txt")
        content = b"base\n"
        info.size = len(content)
        output.addfile(info, io.BytesIO(content))
    return gzip.compress(tar.getvalue(), mtime=0)


class FakeClient:
    def __init__(self, claim, *, renew_error=None):
        self.next_claim = claim
        self.renew_error = renew_error
        self.calls = []
        self.payload = checkout_archive()

    def register(self, worker_id, instance_id):
        self.calls.append(("register", worker_id, instance_id))
        return {}

    def claim(self, worker_id, instance_id):
        self.calls.append(("claim", worker_id, instance_id))
        return self.next_claim

    def renew(self, authority):
        self.calls.append(("renew", authority.fence))
        if self.renew_error:
            raise ProtocolError(self.renew_error, 409 if self.renew_error != "temporary_failure" else 503)
        return {"lease_expires_at": "later", "renew_after_seconds": 1, "cancellation_version": 0}

    def checkout(self, claim):
        self.calls.append(("checkout", claim.authority.fence))
        return self.payload, {
            "Content-Length": str(len(self.payload)),
            "X-Base-Sha": "a" * 40,
            "X-Archive-Sha256": hashlib.sha256(self.payload).hexdigest(),
        }

    def checkpoint(self, authority, operation_id, sequence, phase, thread_id, state):
        self.calls.append(("checkpoint", authority.fence, sequence, phase, thread_id, state))
        return {"accepted_sequence": sequence, "lease_expires_at": "later"}

    def submit_candidate(self, authority, manifest):
        self.calls.append(("candidate", authority.fence, manifest))
        return {"candidate_id": CANDIDATE, "manifest_sha": "c" * 64, "status": "candidate_ready"}

    def complete(self, authority, operation_id, **body):
        self.calls.append(("complete", authority.fence, body))
        return {"work_id": authority.work_id, "status": body["status"], "completed_at": "now"}


class LostCandidateResponseClient(FakeClient):
    def __init__(self, claim):
        super().__init__(claim)
        self.drop_once = True

    def submit_candidate(self, authority, manifest):
        response = super().submit_candidate(authority, manifest)
        if self.drop_once:
            self.drop_once = False
            raise ProtocolError("temporary_failure", 503)
        return response


class FakeRunner:
    instances = []

    def __init__(self, config, claim, guard):
        self.claim = claim
        self.bootstrap_called = False
        self.task_thread = None
        type(self).instances.append(self)

    def bootstrap(self):
        self.bootstrap_called = True
        return THREAD

    def task_turn(self, workspace, thread_id):
        self.task_thread = thread_id
        (workspace / "allowed/result.txt").write_text("candidate\n")
        verdict = "PASS" if self.claim.work_type == "review" else None
        return ProcessResult(0, thread_id, verdict)


class BlockingRunner(FakeRunner):
    process = None

    def task_turn(self, workspace, thread_id):
        process = subprocess.Popen(["sleep", "30"], start_new_session=True)
        type(self).process = process
        self.claim_guard.attach(process)
        process.wait()
        self.claim_guard.detach(process)
        return ProcessResult(process.returncode, thread_id, None)

    def __init__(self, config, claim, guard):
        super().__init__(config, claim, guard)
        self.claim_guard = guard


class FailedResumeRunner(FakeRunner):
    def task_turn(self, workspace, thread_id):
        self.task_thread = thread_id
        return ProcessResult(1, thread_id, None)


def config(root, executable=Path("/bin/true"), auth=Path("/dev/null")):
    return WorkerConfig(
        "http://coordinator.invalid",
        "worker-secret",
        root,
        "10000000-0000-4000-8000-000000000001",
        "20000000-0000-4000-8000-000000000002",
        executable,
        auth,
        Path("/usr/bin/bwrap"),
    )


class WorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        FakeRunner.instances = []
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def run_worker(self, claim, *, renew_error=None, runner=FakeRunner):
        client = FakeClient(claim, renew_error=renew_error)
        worker = Worker(config(self.root), client=client, runner_factory=runner)
        self.assertTrue(worker.run_once())
        return client

    def test_claim_bootstrap_checkpoint_candidate_and_complete(self):
        client = self.run_worker(make_claim())
        checkpoints = [call for call in client.calls if call[0] == "checkpoint"]
        self.assertEqual(
            [(item[2], item[3], item[4]) for item in checkpoints],
            [
                (1, "checkout_ready", None),
                (2, "codex_started", THREAD),
                (3, "working", THREAD),
                (4, "testing", THREAD),
            ],
        )
        candidate = next(call for call in client.calls if call[0] == "candidate")[2]
        self.assertEqual(candidate["files"][0]["path"], "allowed/result.txt")
        completion = [call for call in client.calls if call[0] == "complete"][-1]
        self.assertEqual((completion[2]["status"], completion[2]["candidate_id"]), ("succeeded", CANDIDATE))

    def test_reclaim_resumes_exact_adopted_thread_at_higher_fence(self):
        prior = {
            "sequence": 7,
            "phase": "working",
            "codex_thread_id": THREAD,
            "checkpoint_state": "finite_turn_started",
        }
        client = self.run_worker(make_claim(thread_id=THREAD, prior=prior, fence=2))
        runner = FakeRunner.instances[-1]
        self.assertFalse(runner.bootstrap_called)
        self.assertEqual(runner.task_thread, THREAD)
        self.assertTrue(
            all(call[1] == 2 for call in client.calls if call[0] in {"checkpoint", "candidate", "complete"})
        )

    def test_accepted_candidate_completes_without_checkout_or_codex(self):
        accepted = {"candidate_id": CANDIDATE, "manifest_sha": "c" * 64}
        client = self.run_worker(make_claim(thread_id=THREAD, accepted=accepted, fence=2))
        self.assertEqual(FakeRunner.instances, [])
        self.assertNotIn("checkout", [call[0] for call in client.calls])
        completion = next(call for call in client.calls if call[0] == "complete")
        self.assertEqual(completion[2]["candidate_id"], CANDIDATE)

    def test_lost_candidate_response_recovers_exact_candidate_without_second_codex_turn(self):
        client = LostCandidateResponseClient(make_claim())
        worker = Worker(config(self.root), client=client, runner_factory=FakeRunner)
        self.assertTrue(worker.run_once())
        self.assertNotIn("complete", [call[0] for call in client.calls])
        first_runner_count = len(FakeRunner.instances)
        client.next_claim = make_claim(
            thread_id=THREAD,
            accepted={"candidate_id": CANDIDATE, "manifest_sha": "c" * 64},
            fence=2,
        )
        self.assertTrue(worker.run_once())
        self.assertEqual(len(FakeRunner.instances), first_runner_count)
        completion = [call for call in client.calls if call[0] == "complete"][-1]
        self.assertEqual(completion[2]["candidate_id"], CANDIDATE)

    def test_review_never_submits_candidate(self):
        client = self.run_worker(make_claim(work_type="review"))
        self.assertNotIn("candidate", [call[0] for call in client.calls])
        completion = next(call for call in client.calls if call[0] == "complete")
        self.assertEqual(completion[2]["review_verdict"], "PASS")

    def test_failed_post_adoption_resume_returns_scope_without_replacement(self):
        prior = {
            "sequence": 7,
            "phase": "working",
            "codex_thread_id": THREAD,
            "checkpoint_state": "finite_turn_started",
        }
        client = self.run_worker(
            make_claim(thread_id=THREAD, prior=prior, fence=2),
            runner=FailedResumeRunner,
        )
        runner = FakeRunner.instances[-1]
        self.assertFalse(runner.bootstrap_called)
        self.assertEqual(runner.task_thread, THREAD)
        self.assertNotIn("candidate", [call[0] for call in client.calls])
        completion = [call for call in client.calls if call[0] == "complete"][-1]
        self.assertEqual(
            (completion[2]["status"], completion[2]["summary_code"]),
            ("scope_return", "codex_exact_resume_failed"),
        )

    def test_no_claim_is_a_clean_poll(self):
        client = FakeClient(None)
        worker = Worker(config(self.root), client=client, runner_factory=FakeRunner)
        self.assertFalse(worker.run_once())

    def test_renewal_loss_kills_codex_and_emits_no_candidate_or_completion(self):
        client = self.run_worker(make_claim(), renew_error="stale_or_invalid_lease", runner=BlockingRunner)
        self.assertIsNotNone(BlockingRunner.process)
        process = BlockingRunner.process
        assert process is not None
        self.assertIsNotNone(process.poll())
        names = [call[0] for call in client.calls]
        self.assertNotIn("candidate", names)
        self.assertNotIn("complete", names)


class LeaseAndIsolationTests(unittest.TestCase):
    def test_group_cleanup_kills_descendant_after_leader_exits(self):
        script = (
            "import subprocess,time; "
            "p=subprocess.Popen(['/usr/bin/python3','-c',"
            "'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)']); "
            "print(p.pid,flush=True); time.sleep(30)"
        )
        process = subprocess.Popen(
            ["/usr/bin/python3", "-c", script],
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None
        child_pid = int(process.stdout.readline())
        LeaseGuard._kill(process)
        process.stdout.close()
        self.assertIsNotNone(process.poll())
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_stale_renewal_kills_real_process_group_and_forbids_writes(self):
        client = FakeClient(make_claim(), renew_error="stale_or_invalid_lease")
        guard = LeaseGuard(client, make_claim())
        process = subprocess.Popen(
            [
                "/usr/bin/python3",
                "-c",
                "import subprocess,time; p=subprocess.Popen(['sleep','30']); print(p.pid,flush=True); time.sleep(30)",
            ],
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None
        child_pid = int(process.stdout.readline())
        guard.attach(process)
        guard.start()
        self.assertTrue(guard._lost.wait(3))
        guard.stop()
        process.stdout.close()
        self.assertIsNotNone(process.poll())
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)
        with self.assertRaisesRegex(ProtocolError, "stale_or_invalid_lease"):
            guard.require_live()

    def test_bubblewrap_child_has_no_worker_or_github_secret_and_cannot_read_outside(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = root / "fake-codex"
            fake.write_text(
                "#!/bin/sh\nread ignored\n"
                'if [ -n "${SWITCHSTAND_WORKER_KEY+x}" ] || [ -n "${GITHUB_TOKEN+x}" ]; then exit 9; fi\n'
                "if [ -r /outside-canary ]; then exit 8; fi\n"
                f'printf \'%s\\n\' \'{{"type":"thread.started","thread_id":"{THREAD}"}}\'\n'
            )
            fake.chmod(0o700)
            auth = root / "auth.json"
            auth.write_text("{}")
            workspace = root / "workspace"
            workspace.mkdir()
            os.environ["SWITCHSTAND_WORKER_KEY"] = "parent-worker-secret"
            os.environ["GITHUB_TOKEN"] = "parent-github-secret"
            os.environ["OPENAI_API_KEY"] = "parent-openai-secret"
            client = FakeClient(make_claim())
            guard = LeaseGuard(client, make_claim())
            runner = CodexRunner(config(root, fake, auth), make_claim(), guard)
            result = runner._run(workspace, ["ignored"], "private input")
            self.assertEqual((result.exit_code, result.thread_id), (0, THREAD))
            os.environ.pop("SWITCHSTAND_WORKER_KEY")
            os.environ.pop("GITHUB_TOKEN")
            os.environ.pop("OPENAI_API_KEY")

    def test_state_db_adoption_requires_complete_paginated_listing(self):
        class FakeAppServer:
            def __init__(self, responses):
                self.responses = list(responses)
                self.calls = []

            def thread_list(self, params):
                self.calls.append(params)
                return self.responses.pop(0)

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            auth = root / "auth.json"
            auth.write_text("{}")
            claim = make_claim()
            guard = LeaseGuard(FakeClient(claim), claim)
            runner = CodexRunner(config(root, auth=auth), claim, guard)
            workspace = root / "workspace"
            workspace.mkdir()
            socket_path = runner.job_state / "state-fixed.sock"
            socket_path.touch()
            process = mock.Mock(pid=999999)
            process.poll.return_value = None

            complete = FakeAppServer(
                [
                    {"data": [{"id": THREAD}], "nextCursor": "page-2"},
                    {"data": [], "nextCursor": None},
                ]
            )
            with (
                mock.patch("switchstand_worker.supervisor.uuid.uuid4", return_value=mock.Mock(hex="fixed")),
                mock.patch("switchstand_worker.supervisor.subprocess.Popen", return_value=process),
                mock.patch("switchstand_worker.supervisor.CodexAppServer", return_value=complete),
                mock.patch("switchstand_worker.supervisor.LeaseGuard._kill"),
            ):
                self.assertTrue(runner.thread_in_state_db(THREAD, workspace))
            self.assertEqual(len(complete.calls), 2)
            self.assertTrue(all(call["useStateDbOnly"] is True for call in complete.calls))
            self.assertNotIn("cursor", complete.calls[0])
            self.assertEqual(complete.calls[1]["cursor"], "page-2")

            socket_path.touch()
            incomplete = FakeAppServer(
                [
                    {"data": [{"id": THREAD}], "nextCursor": "repeat"},
                    {"data": [], "nextCursor": "repeat"},
                ]
            )
            with (
                mock.patch("switchstand_worker.supervisor.uuid.uuid4", return_value=mock.Mock(hex="fixed")),
                mock.patch("switchstand_worker.supervisor.subprocess.Popen", return_value=process),
                mock.patch("switchstand_worker.supervisor.CodexAppServer", return_value=incomplete),
                mock.patch("switchstand_worker.supervisor.LeaseGuard._kill"),
            ):
                self.assertFalse(runner.thread_in_state_db(THREAD, workspace))

    def test_cli_reports_only_fixed_failure_without_raw_details(self):
        os.environ["SWITCHSTAND_WORKER_KEY"] = "worker-secret"
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch(
                    "switchstand_worker.__main__.WorkerConfig.create",
                    side_effect=RuntimeError("raw /private/path thread 00000000-0000-0000-0000-000000000000"),
                ),
                mock.patch("sys.stderr", new=io.StringIO()) as error,
            ):
                result = main(
                    [
                        "--coordinator-url",
                        "https://coordinator.example",
                        "--state-root",
                        temporary,
                        "--once",
                    ]
                )
        os.environ.pop("SWITCHSTAND_WORKER_KEY", None)
        self.assertEqual(result, 1)
        self.assertEqual(error.getvalue(), "WORKER_FAILURE\n")


if __name__ == "__main__":
    unittest.main()
