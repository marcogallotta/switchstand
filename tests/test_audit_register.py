from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_audit_register", ROOT / "scripts" / "check_audit_register.py"
)
assert SPEC and SPEC.loader
check_audit_register = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_audit_register)
audit_evidence = sys.modules["audit_evidence"]
audit_register_policy = sys.modules["audit_register_policy"]


def load_register(*, include_gate_history: bool = False) -> dict[str, Any]:
    register = json.loads((ROOT / "audit" / "findings.json").read_text(encoding="utf-8"))
    if include_gate_history:
        record = register["gate_repair_history"][0]
        if not record["review_receipts"]:
            subject = f"gate-repair:{record['base_sha']}:{record['extensions_sha256']}"
            record["review_receipts"] = [
                durable("gate_repair_review", subject, "reviewer-a", "a"),
                durable("gate_repair_review", subject, "reviewer-b", "b"),
            ]
    else:
        register.pop("gate_repair_history", None)
    return register


def durable(
    role: str,
    subject: str,
    producer: str = "reviewer-a",
    digest_character: str = "a",
) -> dict[str, object]:
    digest = digest_character * 64
    return {
        "kind": "content_addressed_receipt",
        "role": role,
        "subject": subject,
        "producer": producer,
        "reference": f"audit/receipts/{digest}.json",
        "sha256": digest,
    }


def closure_evidence(subject: str, *, distinct: bool) -> dict[str, dict[str, object]]:
    roles = {
        "reproducer": "reproducer",
        "regression": "regression",
        "ci": "ci",
        "review": "independent_review",
        "receipt": "closure",
    }
    return {
        name: durable(
            role,
            subject,
            producer=f"producer-{name}",
            digest_character=str(index) if distinct else "a",
        )
        for index, (name, role) in enumerate(roles.items(), start=1)
    }


def fake_verify(
    value: object,
    field: str,
    _root: Path,
    *,
    durable: bool = False,
    expected_role: str | None = None,
    expected_subject: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise audit_evidence.EvidenceError(f"{field} must be an object")
    if durable:
        assert value["kind"] == "content_addressed_receipt"
    if expected_role is not None:
        assert value["role"] == expected_role
    if expected_subject is not None:
        assert value["subject"] == expected_subject
    return value


@contextmanager
def mocked_receipts():
    with (
        patch.object(check_audit_register, "verify_evidence", side_effect=fake_verify),
        patch.object(audit_register_policy, "verify_evidence", side_effect=fake_verify),
    ):
        yield


class AuditRegisterTests(unittest.TestCase):
    def setUp(self):
        self.register = load_register()

    def test_current_register_is_valid_and_complete(self):
        current = load_register(include_gate_history=True)
        with mocked_receipts():
            findings = check_audit_register.validate_register(current)
        self.assertEqual(
            {finding["id"] for finding in findings},
            {
                "SWSEC-001",
                "SWSEC-002",
                "SWSEC-003",
                "A-01",
                "A-02",
                "A-03",
                "TE-001",
                "TE-002",
                "TE-003",
                "TE-004",
                "TE-005",
                "TE-006",
            },
        )
        architecture = next(run for run in self.register["audit_runs"] if run["task"] == "1217969349490810")
        self.assertEqual(architecture["status"], "NOT_EXECUTED")

    def test_rejects_duplicate_ids_ownerless_records_and_bad_deadlines(self):
        duplicate = deepcopy(self.register)
        duplicate["findings"].append(deepcopy(duplicate["findings"][0]))
        with self.assertRaisesRegex(check_audit_register.ValidationError, "duplicate finding id"):
            check_audit_register.validate_register(duplicate)

        ownerless = deepcopy(self.register)
        ownerless["findings"][0]["owner"] = ""
        with self.assertRaisesRegex(check_audit_register.ValidationError, "owner must be a nonempty"):
            check_audit_register.validate_register(ownerless)

        stale = deepcopy(self.register)
        stale["findings"][0]["deadline"] = "2026-08-29T18:10:37Z"
        with self.assertRaisesRegex(check_audit_register.ValidationError, "deadline precedes discovery"):
            check_audit_register.validate_register(stale)

        late_high = deepcopy(self.register)
        late_high["findings"][3]["deadline"] = "2026-09-01T18:09:34Z"
        with self.assertRaisesRegex(check_audit_register.ValidationError, "cannot exceed 72 hours"):
            check_audit_register.validate_register(late_high)

    def test_containment_requires_disabled_reachability_and_durable_evidence(self):
        contained = deepcopy(self.register)
        finding = contained["findings"][0]
        finding["state"] = "CONTAINED"
        finding["containment"] = durable("containment", "SWSEC-001")
        with mocked_receipts():
            with self.assertRaisesRegex(check_audit_register.ValidationError, "disabled or removed"):
                check_audit_register.validate_register(contained)

        open_disabled = deepcopy(self.register)
        open_disabled["findings"][0]["reachability"] = "DISABLED"
        with self.assertRaisesRegex(check_audit_register.ValidationError, "must be CONTAINED"):
            check_audit_register.validate_register(open_disabled)

    def test_closure_requires_all_two_phase_evidence(self):
        closed = deepcopy(self.register)
        finding = closed["findings"][1]
        finding["state"] = "CLOSED"
        finding["reachability"] = "DISABLED"
        finding["fix"] = durable("fix", "SWSEC-002", digest_character="f")
        finding["closure"] = closure_evidence("SWSEC-002", distinct=True)
        with mocked_receipts():
            check_audit_register.validate_register(closed)
            finding["closure"]["review"] = None
            with self.assertRaisesRegex(check_audit_register.ValidationError, "closure.review must be an object"):
                check_audit_register.validate_register(closed)

    def test_transition_rejects_deletion_illegal_close_and_unreviewed_severity_change(self):
        current = deepcopy(self.register)
        current["findings"].pop()
        with self.assertRaisesRegex(ValueError, "cannot be removed"):
            audit_register_policy.validate_transitions(
                self.register, current, ROOT, check_audit_register.validate_register
            )

        current = deepcopy(self.register)
        current["findings"][0]["state"] = "CLOSED"
        with self.assertRaisesRegex(ValueError, "illegal state transition"):
            audit_register_policy.validate_transitions(
                self.register, current, ROOT, check_audit_register.validate_register
            )

        current = deepcopy(self.register)
        current["findings"][1]["severity"] = "LOW"
        with self.assertRaisesRegex(ValueError, "requires exactly two new receipts"):
            audit_register_policy.validate_transitions(
                self.register, current, ROOT, check_audit_register.validate_register
            )

        current = deepcopy(self.register)
        current["findings"][0]["deadline"] = "2026-08-30T18:10:38Z"
        with self.assertRaisesRegex(ValueError, "deadline is immutable"):
            audit_register_policy.validate_transitions(
                self.register, current, ROOT, check_audit_register.validate_register
            )

    def test_scope_metadata_audit_history_and_runnable_map_are_immutable(self):
        cases = []
        changed = deepcopy(self.register)
        changed["findings"][0]["affected_paths"] = ["src/unrelated_feature.py"]
        cases.append(("affected_paths", changed))
        changed = deepcopy(self.register)
        changed["findings"][0]["owner"] = "unreviewed-owner"
        cases.append(("owner", changed))
        changed = deepcopy(self.register)
        changed["audit_runs"].pop()
        cases.append(("audit_runs", changed))
        changed = deepcopy(self.register)
        changed["runnable_surface_paths"].append("src/unrelated_feature.py")
        cases.append(("runnable_surface_paths", changed))
        for expected, current in cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, expected):
                audit_register_policy.validate_transitions(
                    self.register, current, ROOT, check_audit_register.validate_register
                )

    def test_severity_change_requires_two_distinct_role_bound_receipts(self):
        current = deepcopy(self.register)
        finding = current["findings"][1]
        finding["severity"] = "LOW"
        finding["severity_rationale"] = "Reviewed lower impact."
        subject = "SWSEC-002:MEDIUM->LOW"
        repeated = durable("severity_review", subject)
        finding["severity_change_receipts"] = [repeated, deepcopy(repeated)]
        with mocked_receipts(), self.assertRaisesRegex(ValueError, "two independent receipts"):
            audit_register_policy.validate_transitions(
                self.register, current, ROOT, check_audit_register.validate_register
            )

    def test_content_addressed_receipt_must_exist_and_match_its_digest(self):
        made_up = durable("fix", "SWSEC-002")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(audit_evidence.EvidenceError, "unavailable"):
                audit_evidence.verify_evidence(
                    made_up,
                    "receipt",
                    root,
                    durable=True,
                    expected_role="fix",
                    expected_subject="SWSEC-002",
                )
            receipt = {
                "schema_version": 1,
                "role": "fix",
                "subject": "SWSEC-002",
                "producer": "github-user-id:42",
                "created_at": "2026-08-29T20:00:00Z",
                "head_sha": "00357f9db56e0784644657d19595285bc9a2c8cd",
                "evidence_kind": "github_issue_comment",
                "evidence_reference": (
                    "https://github.com/marcogallotta/switchstand/pull/57#issuecomment-123456"
                ),
                "summary": "Focused reproducer and correction passed.",
            }
            raw = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
            digest = hashlib.sha256(raw).hexdigest()
            path = root / "audit" / "receipts" / f"{digest}.json"
            path.parent.mkdir(parents=True)
            path.write_bytes(raw)
            evidence = durable("fix", "SWSEC-002", producer="github-user-id:42")
            evidence["reference"] = f"audit/receipts/{digest}.json"
            evidence["sha256"] = digest
            with (
                patch.object(audit_evidence, "_verify_head_binding"),
                patch.object(audit_evidence, "_verify_external_provenance"),
            ):
                audit_evidence.verify_evidence(
                    evidence,
                    "receipt",
                    root,
                    durable=True,
                    expected_role="fix",
                    expected_subject="SWSEC-002",
                )
            receipt["evidence_reference"] = "git:self-asserted"
            raw = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
            digest = hashlib.sha256(raw).hexdigest()
            path = root / "audit" / "receipts" / f"{digest}.json"
            path.write_bytes(raw)
            evidence["reference"], evidence["sha256"] = f"audit/receipts/{digest}.json", digest
            with (
                patch.object(audit_evidence, "_verify_head_binding"),
                self.assertRaisesRegex(ValueError, "eligible external GitHub provenance"),
            ):
                audit_evidence.verify_evidence(
                    evidence, "receipt", root, durable=True, expected_role="fix",
                    expected_subject="SWSEC-002"
                )
        with self.assertRaisesRegex(ValueError, "not an available commit"):
            audit_evidence._verify_head_binding(ROOT, "f" * 40, "receipt")
        github_object = {
            "id": 123456,
            "user": {"login": "reviewer-a", "id": 42},
            "author_association": "COLLABORATOR",
            "body": (
                "[role:fix] [subject:SWSEC-002] "
                "00357f9db56e0784644657d19595285bc9a2c8cd"
            ),
        }
        with (
            patch.object(audit_evidence, "_github_json", return_value=github_object),
            self.assertRaisesRegex(ValueError, "producer does not match"),
        ):
            audit_evidence._verify_external_provenance(
                "github_issue_comment",
                "https://github.com/marcogallotta/switchstand/pull/57#issuecomment-123456",
                "github-user-id:999",
                "fix",
                "SWSEC-002",
                "00357f9db56e0784644657d19595285bc9a2c8cd",
                "receipt",
            )

    def test_closure_cannot_reuse_one_receipt_for_multiple_roles(self):
        closed = deepcopy(self.register)
        finding = closed["findings"][1]
        finding["state"] = "CLOSED"
        finding["reachability"] = "DISABLED"
        finding["fix"] = durable("fix", "SWSEC-002", digest_character="f")
        finding["closure"] = closure_evidence("SWSEC-002", distinct=False)
        with mocked_receipts(), self.assertRaisesRegex(ValueError, "receipts must be distinct"):
            check_audit_register.validate_register(closed)

        closed["findings"][1]["closure"] = closure_evidence("SWSEC-002", distinct=True)
        for item in closed["findings"][1]["closure"].values():
            item["producer"] = "same-producer"
        with mocked_receipts(), self.assertRaisesRegex(ValueError, "independent review producer"):
            check_audit_register.validate_register(closed)

    def test_worker_containment_is_bound_to_all_entrypoints_and_source_guards(self):
        protection = self.register["capability_protections"][0]
        required = {
            "pyproject.toml",
            "src/switchstand_worker/__main__.py",
            "src/switchstand_worker/__init__.py",
            "src/switchstand_worker/supervisor.py",
        }
        self.assertEqual(set(protection["paths"]), required)
        self.assertTrue(required.issubset(set(self.register["findings"][0]["affected_paths"])))
        contained = deepcopy(self.register)
        contained["capability_protections"][0]["state"] = "DISABLED"
        finding = contained["findings"][0]
        finding["state"] = "CONTAINED"
        finding["reachability"] = "DISABLED"
        finding["containment"] = durable("containment", "SWSEC-001")
        with mocked_receipts(), self.assertRaisesRegex(ValueError, "source differs from the reviewed stub"):
            check_audit_register.validate_register(contained)
        actual: dict[str, set[str]] = {}
        for class_name, method_name in audit_evidence.WORKER_QUARANTINE_METHODS:
            actual.setdefault(class_name, set()).add(method_name)
        self.assertEqual(actual["WorkerConfig"], {"create"})
        self.assertEqual(actual["Worker"], {"__init__", "run_once", "_execute"})
        self.assertEqual(
            actual["CodexRunner"],
            {"__init__", "_boundary", "_run", "bootstrap", "task_turn", "thread_in_state_db"},
        )
        self.assertEqual(
            actual["LeaseGuard"],
            {"__init__", "start", "stop", "lost", "require_live", "attach", "detach", "_loop", "_lose", "_kill"},
        )

    def test_freeze_reports_reachable_critical_high_wip_and_overdue_high(self):
        findings = check_audit_register.validate_register(self.register)
        reasons = audit_register_policy.freeze_reasons(
            findings, datetime(2026, 9, 2, tzinfo=timezone.utc)
        )
        self.assertIn("reachable Critical SWSEC-001", reasons)
        self.assertIn("High WIP is 6, maximum is 2", reasons)
        self.assertIn("overdue High A-01", reasons)
        self.assertIn("12 findings lack immutable discovery receipts", reasons)

    def test_contained_critical_paths_remain_protected_until_closure(self):
        findings = check_audit_register.validate_register(self.register)
        findings[0]["state"] = "CONTAINED"
        findings[0]["reachability"] = "DISABLED"
        findings[0]["containment"] = durable("containment", "SWSEC-001")
        reasons = audit_register_policy.protected_capability_reasons(
            findings, {"src/switchstand_worker/supervisor.py"}
        )
        self.assertEqual(reasons, ["protected Critical capability SWSEC-001 changed before closure"])

    def test_registration_scope_allows_only_governance_paths(self):
        scope = {
            "schema_version": 1,
            "base_sha": "base",
            "kind": "registration",
            "finding_ids": [],
            "runnable_blocker_id": None,
            "gate_repair_receipts": [],
            "rationale": "Register current audit results.",
        }
        findings = check_audit_register.validate_register(self.register)
        with (
            patch.object(check_audit_register, "_load_json", return_value=scope),
            patch.object(
                check_audit_register,
                "_changed_paths",
                return_value={"audit/findings.json", "scripts/check_audit_register.py"},
            ),
        ):
            check_audit_register.validate_scope(
                "base",
                None,
                findings,
                self.register["runnable_surface_paths"],
                self.register["runnable_blockers"],
                ["freeze"],
            )
        with (
            patch.object(check_audit_register, "_load_json", return_value=scope),
            patch.object(
                check_audit_register,
                "_changed_paths",
                return_value={"scripts/check_audit_register.py"},
            ),
            self.assertRaisesRegex(ValueError, "two-review gate_repair"),
        ):
            check_audit_register.validate_scope(
                "base",
                self.register,
                findings,
                self.register["runnable_surface_paths"],
                self.register["runnable_blockers"],
                ["freeze"],
            )
        with (
            patch.object(check_audit_register, "_load_json", return_value=scope),
            patch.object(check_audit_register, "_changed_paths", return_value={"src/switchstand/engine.py"}),
            self.assertRaisesRegex(check_audit_register.ValidationError, "exceed declared registration scope"),
        ):
            check_audit_register.validate_scope(
                "base",
                None,
                findings,
                self.register["runnable_surface_paths"],
                self.register["runnable_blockers"],
                ["freeze"],
            )

    def test_remediation_scope_is_bound_to_cited_finding_paths(self):
        scope = {
            "schema_version": 1,
            "base_sha": "base",
            "kind": "remediation",
            "finding_ids": ["SWSEC-002"],
            "runnable_blocker_id": None,
            "gate_repair_receipts": [],
            "rationale": "Reject malformed App Server envelopes.",
        }
        findings = check_audit_register.validate_register(self.register)
        with (
            patch.object(check_audit_register, "_load_json", return_value=scope),
            patch.object(check_audit_register, "_changed_paths", return_value={"src/switchstand/app_server.py"}),
        ):
            check_audit_register.validate_scope(
                "base",
                self.register,
                findings,
                self.register["runnable_surface_paths"],
                self.register["runnable_blockers"],
                ["freeze"],
            )
        with (
            patch.object(check_audit_register, "_load_json", return_value=scope),
            patch.object(check_audit_register, "_changed_paths", return_value={"src/switchstand/engine.py"}),
            self.assertRaisesRegex(check_audit_register.ValidationError, "exceed declared remediation scope"),
        ):
            check_audit_register.validate_scope(
                "base",
                self.register,
                findings,
                self.register["runnable_surface_paths"],
                self.register["runnable_blockers"],
                ["freeze"],
            )
        with (
            patch.object(check_audit_register, "_load_json", return_value=scope),
            patch.object(
                check_audit_register,
                "_changed_paths",
                return_value={"scripts/check_audit_register.py", "src/switchstand/app_server.py"},
            ),
            self.assertRaisesRegex(ValueError, "two-review gate_repair"),
        ):
            check_audit_register.validate_scope(
                "base",
                self.register,
                findings,
                self.register["runnable_surface_paths"],
                self.register["runnable_blockers"],
                ["freeze"],
            )

    def test_runnable_repair_requires_registered_open_blocker(self):
        scope = {
            "schema_version": 1,
            "base_sha": "base",
            "kind": "runnable_repair",
            "finding_ids": [],
            "runnable_blocker_id": "RUN-DOES-NOT-EXIST",
            "gate_repair_receipts": [],
            "rationale": "Repair the declared runnable path.",
        }
        findings = check_audit_register.validate_register(self.register)
        with (
            patch.object(check_audit_register, "_load_json", return_value=scope),
            patch.object(check_audit_register, "_changed_paths", return_value={"src/switchstand/service.py"}),
            self.assertRaisesRegex(check_audit_register.ValidationError, "must name one OPEN blocker"),
        ):
            check_audit_register.validate_scope(
                "base",
                self.register,
                findings,
                self.register["runnable_surface_paths"],
                self.register["runnable_blockers"],
                ["freeze"],
            )

if __name__ == "__main__":
    unittest.main()
