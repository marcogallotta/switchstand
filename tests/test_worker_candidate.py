from __future__ import annotations

import gzip
import hashlib
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest

from switchstand_worker.candidate import (
    build_candidate,
    initialize_local_git,
    materialize_checkout,
    validate_candidate_payload,
    validate_path,
)
from switchstand_worker.protocol import Authority, Claim, ProtocolError, canonical_json


BASE = "a" * 40


def claim(prefixes=("allowed",)):
    authority = Authority(
        "work:test-0001",
        "10000000-0000-4000-8000-000000000001",
        "20000000-0000-4000-8000-000000000002",
        1,
        "A" * 43,
        0,
    )
    return Claim(
        authority,
        "implementation",
        "2026-08-29T12:00:00Z",
        "b" * 64,
        "bounded work",
        (),
        {
            "full_name": "owner/repo",
            "base_sha": BASE,
            "candidate_branch": "candidate/test",
            "allowed_path_prefixes": list(prefixes),
        },
        "/v2/work/work:test-0001/checkout",
        None,
        None,
        None,
        {
            "max_files": 32,
            "max_file_bytes": 65536,
            "max_total_bytes": 262144,
            "max_deletions": 32,
            "max_json_bytes": 393216,
        },
    )


def archive(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as output:
        root = tarfile.TarInfo("repo-root")
        root.type = tarfile.DIRTYPE
        output.addfile(root)
        for name, data, kind in entries:
            info = tarfile.TarInfo(f"repo-root/{name}")
            if kind == "file":
                info.size = len(data)
                output.addfile(info, io.BytesIO(data))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                output.addfile(info)
            else:
                info.type = kind
                info.linkname = "target"
                output.addfile(info)
    return gzip.compress(buffer.getvalue(), mtime=0)


def headers(payload):
    return {
        "Content-Length": str(len(payload)),
        "X-Base-Sha": BASE,
        "X-Archive-Sha256": hashlib.sha256(payload).hexdigest(),
    }


class CheckoutTests(unittest.TestCase):
    def test_materializes_strict_root_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "workspace"
            payload = archive([("allowed", b"", "dir"), ("allowed/file.txt", b"exact\n", "file")])
            materialize_checkout(claim(), payload, headers(payload), destination)
            self.assertEqual((destination / "allowed/file.txt").read_bytes(), b"exact\n")
            self.assertFalse(any(path.name.startswith(".workspace.") for path in Path(temporary).iterdir()))

    def test_rejects_integrity_base_and_existing_destination_without_modification(self):
        payload = archive([("allowed/file.txt", b"exact", "file")])
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "workspace"
            destination.mkdir()
            sentinel = destination / "sentinel"
            sentinel.write_text("preserved")
            for bad_headers in (
                {**headers(payload), "X-Base-Sha": "c" * 40},
                {**headers(payload), "X-Archive-Sha256": "0" * 64},
                headers(payload),
            ):
                with self.subTest(headers=bad_headers), self.assertRaises(ProtocolError):
                    materialize_checkout(claim(), payload, bad_headers, destination)
                self.assertEqual(sentinel.read_text(), "preserved")

    def test_rejects_hostile_archive_entries(self):
        hostile = [
            [("../escape", b"x", "file")],
            [("allowed/link", b"", tarfile.SYMTYPE)],
            [("allowed/link", b"", tarfile.LNKTYPE)],
            [("allowed/fifo", b"", tarfile.FIFOTYPE)],
            [("allowed/file", b"x", "file"), ("allowed/file", b"y", "file")],
            [(".git/config", b"host-controlled", "file")],
            [("allowed/parent", b"file", "file"), ("allowed/parent/child", b"nested", "file")],
        ]
        with tempfile.TemporaryDirectory() as temporary:
            for index, entries in enumerate(hostile):
                payload = archive(entries)
                destination = Path(temporary) / f"workspace-{index}"
                with self.subTest(index=index), self.assertRaises(ProtocolError):
                    materialize_checkout(claim(), payload, headers(payload), destination)
                self.assertFalse(destination.exists())


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        (self.workspace / "allowed").mkdir()
        (self.workspace / "allowed/changed.txt").write_text("before\n")
        (self.workspace / "allowed/deleted.txt").write_text("delete\n")
        initialize_local_git(self.workspace)

    def tearDown(self):
        self.temporary.cleanup()

    def test_builds_canonical_multi_file_and_deletion_manifest(self):
        (self.workspace / "allowed/changed.txt").write_text("after\n")
        (self.workspace / "allowed/new.txt").write_text("new\n")
        (self.workspace / "allowed/deleted.txt").unlink()
        manifest = build_candidate(
            claim(),
            self.workspace,
            operation_id="candidate:00000000-0000-4000-8000-000000000001",
            message="bounded change",
            check_summaries=[{"name": "unit", "outcome": "PASS", "summary": "suite passed"}],
        )
        self.assertEqual([item["path"] for item in manifest["files"]], ["allowed/changed.txt", "allowed/new.txt"])
        self.assertEqual(manifest["deletions"], [{"path": "allowed/deleted.txt"}])
        request = {**claim().authority.fields(), **manifest}
        digest_input = dict(request)
        digest_input.pop("request_digest")
        self.assertEqual(manifest["request_digest"], hashlib.sha256(canonical_json(digest_input)).hexdigest())

    def test_rejects_forbidden_binary_executable_oversize_and_unexpected_deletion(self):
        cases = []
        outside = self.workspace / "outside.txt"
        outside.write_text("changed")
        cases.append(outside)
        for index, action in enumerate(
            (
                lambda path: path.write_bytes(b"binary\0"),
                lambda path: (path.write_text("executable"), path.chmod(0o700)),
                lambda path: path.write_bytes(b"x" * 65_537),
            )
        ):
            reset = Path(self.temporary.name) / f"case-{index}"
            reset.mkdir()
            (reset / "allowed").mkdir()
            initialize_local_git(reset)
            target = reset / "allowed/file"
            action(target)
            with self.subTest(index=index), self.assertRaises(ProtocolError):
                build_candidate(
                    claim(), reset, operation_id=f"candidate:case-{index:04d}", message="change", check_summaries=[]
                )
        with self.assertRaises(ProtocolError):
            build_candidate(
                claim(), self.workspace, operation_id="candidate:outside", message="change", check_summaries=[]
            )

        symlink_workspace = Path(self.temporary.name) / "symlink-base"
        symlink_workspace.mkdir()
        (symlink_workspace / "allowed").mkdir()
        os.symlink("target", symlink_workspace / "allowed/link")
        initialize_local_git(symlink_workspace)
        (symlink_workspace / "allowed/link").unlink()
        with self.assertRaises(ProtocolError):
            build_candidate(
                claim(), symlink_workspace, operation_id="candidate:delete-link", message="change", check_summaries=[]
            )

    def test_rejects_path_collisions_and_bad_summaries(self):
        self.assertEqual(validate_path("allowed/file", ["allowed"]), "allowed/file")
        for path in ("/absolute", "../escape", "allowed\\file", "allowed/../file", "other/file"):
            with self.subTest(path=path), self.assertRaises(ProtocolError):
                validate_path(path, ["allowed"])
        (self.workspace / "allowed/changed.txt").write_text("after")
        with self.assertRaises(ProtocolError):
            build_candidate(
                claim(),
                self.workspace,
                operation_id="candidate:bad-check",
                message="change",
                check_summaries=[{"name": "unit", "outcome": "PASS", "summary": "raw /tmp/path"}],
            )

    def test_maximum_content_and_gzip_validation(self):
        for index in range(32):
            character = chr(97 + index % 26)
            (self.workspace / f"allowed/max-{index:02d}").write_bytes((character * 8_192).encode())
        manifest = build_candidate(
            claim(), self.workspace, operation_id="candidate:maximum", message="maximum", check_summaries=[]
        )
        self.assertEqual(sum(item["decoded_bytes"] for item in manifest["files"]), 262_144)
        encoded = canonical_json(manifest)
        self.assertEqual(validate_candidate_payload(encoded)["request_digest"], manifest["request_digest"])
        zipped = gzip.compress(encoded, mtime=0)
        self.assertEqual(
            validate_candidate_payload(zipped, compressed=True)["request_digest"], manifest["request_digest"]
        )
        with self.assertRaises(ProtocolError):
            validate_candidate_payload(zipped + gzip.compress(b"{}"), compressed=True)


if __name__ == "__main__":
    unittest.main()
