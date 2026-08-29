from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import patch

from tests.test_audit_register import (
    ROOT,
    audit_register_policy,
    check_audit_register,
    durable,
    load_register,
    mocked_receipts,
)


class AuditRegisterAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.register = load_register()
        self.findings = check_audit_register.validate_register(self.register)

    def test_medium_to_high_may_only_shorten_deadline_with_two_reviews(self):
        current = deepcopy(self.register)
        finding = current["findings"][1]
        finding["severity"] = "HIGH"
        finding["severity_rationale"] = "Two reviews confirmed high impact."
        finding["deadline"] = "2026-08-31T18:10:38Z"
        subject = "SWSEC-002:MEDIUM->HIGH"
        finding["severity_change_receipts"] = [
            durable("severity_review", subject, "reviewer-a", "a"),
            durable("severity_review", subject, "reviewer-b", "b"),
        ]
        with mocked_receipts():
            check_audit_register.validate_register(current)
            audit_register_policy.validate_transitions(
                self.register, current, ROOT, check_audit_register.validate_register
            )
        finding["deadline"] = "2026-09-12T18:10:38Z"
        with mocked_receipts(), self.assertRaisesRegex(ValueError, "cannot exceed 72 hours"):
            check_audit_register.validate_register(current)

    def test_transition_allows_only_its_exact_receipt_files(self):
        scope = {
            "schema_version": 1,
            "base_sha": "base",
            "kind": "remediation",
            "finding_ids": ["SWSEC-002"],
            "runnable_blocker_id": None,
            "gate_repair_receipts": [],
            "rationale": "Remediate with exact evidence.",
        }
        receipt = "audit/receipts/" + "a" * 64 + ".json"
        with (
            patch.object(check_audit_register, "_load_json", return_value=scope),
            patch.object(check_audit_register, "_changed_paths", return_value={receipt}),
        ):
            check_audit_register.validate_scope(
                "base", self.register, self.findings, self.register["runnable_surface_paths"],
                self.register["runnable_blockers"], ["freeze"], {receipt}
            )
        with (
            patch.object(check_audit_register, "_load_json", return_value=scope),
            patch.object(check_audit_register, "_changed_paths", return_value={receipt}),
            self.assertRaisesRegex(ValueError, "exceed declared remediation scope"),
        ):
            check_audit_register.validate_scope(
                "base", self.register, self.findings, self.register["runnable_surface_paths"],
                self.register["runnable_blockers"], ["freeze"], set()
            )

    def test_gate_repair_is_two_reviewed_and_cannot_reach_product_code(self):
        subject = "gate-repair:base"
        scope = {
            "schema_version": 1,
            "base_sha": "base",
            "kind": "gate_repair",
            "finding_ids": [],
            "runnable_blocker_id": None,
            "gate_repair_receipts": [
                durable("gate_repair_review", subject, "reviewer-a", "a"),
                durable("gate_repair_review", subject, "reviewer-b", "b"),
            ],
            "rationale": "Correct a demonstrated enforcement defect.",
        }
        for count in (0, 1):
            invalid = deepcopy(scope)
            invalid["gate_repair_receipts"] = invalid["gate_repair_receipts"][:count]
            with (
                mocked_receipts(),
                patch.object(check_audit_register, "_load_json", return_value=invalid),
                patch.object(
                    check_audit_register, "_changed_paths", return_value={"scripts/check_audit_register.py"}
                ),
                self.assertRaisesRegex(ValueError, "requires exactly two reviews"),
            ):
                check_audit_register.validate_scope(
                    "base", self.register, self.findings, self.register["runnable_surface_paths"],
                    self.register["runnable_blockers"], ["freeze"]
                )
        with (
            mocked_receipts(),
            patch.object(check_audit_register, "_load_json", return_value=scope),
            patch.object(
                check_audit_register, "_changed_paths", return_value={"scripts/check_audit_register.py"}
            ),
        ):
            check_audit_register.validate_scope(
                "base", self.register, self.findings, self.register["runnable_surface_paths"],
                self.register["runnable_blockers"], ["freeze"]
            )
        with (
            mocked_receipts(),
            patch.object(check_audit_register, "_load_json", return_value=scope),
            patch.object(check_audit_register, "_changed_paths", return_value={"src/switchstand/service.py"}),
            self.assertRaisesRegex(ValueError, "exceed declared gate_repair scope"),
        ):
            check_audit_register.validate_scope(
                "base", self.register, self.findings, self.register["runnable_surface_paths"],
                self.register["runnable_blockers"], ["freeze"]
            )

    def test_gate_authority_always_requires_gate_repair_even_without_freeze(self):
        remediation = {
            "schema_version": 1,
            "base_sha": "base",
            "kind": "remediation",
            "finding_ids": ["SWSEC-003"],
            "runnable_blocker_id": None,
            "gate_repair_receipts": [],
            "rationale": "Attempt to weaken the overlapping workflow path.",
        }
        with (
            patch.object(check_audit_register, "_load_json", return_value=remediation),
            patch.object(
                check_audit_register, "_changed_paths", return_value={".github/workflows/quality.yml"}
            ),
            self.assertRaisesRegex(ValueError, "require a two-review gate_repair"),
        ):
            check_audit_register.validate_scope(
                "base", self.register, self.findings, self.register["runnable_surface_paths"],
                self.register["runnable_blockers"], []
            )

    def test_runnable_repair_cannot_register_its_own_blocker(self):
        current = deepcopy(self.register)
        current["runnable_blockers"] = [{
            "id": "RUN-001", "title": "Self-authored blocker", "status": "OPEN",
            "affected_paths": ["src/switchstand/service.py"], "owner": "candidate",
            "source_evidence": durable("runnable_blocker", "RUN-001"), "closure": None,
        }]
        scope = {
            "schema_version": 1, "base_sha": "base", "kind": "runnable_repair",
            "finding_ids": [], "runnable_blocker_id": "RUN-001", "gate_repair_receipts": [],
            "rationale": "Attempt a same-change blocker registration and repair.",
        }
        with (
            patch.object(check_audit_register, "_load_json", return_value=scope),
            patch.object(check_audit_register, "_changed_paths", return_value={"src/switchstand/service.py"}),
            self.assertRaisesRegex(ValueError, "must name one OPEN blocker"),
        ):
            check_audit_register.validate_scope(
                "base", self.register, self.findings, current["runnable_surface_paths"],
                current["runnable_blockers"], ["freeze"]
            )


if __name__ == "__main__":
    unittest.main()
