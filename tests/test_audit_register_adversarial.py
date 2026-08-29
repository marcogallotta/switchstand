from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any
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

    def test_r1b_rejected_paths_are_exactly_bound_to_real_findings(self):
        expected = {
            "README.md",
            "docs/decisions/0001-prototype-boundary.md",
            "src/switchstand/native_contracts.py",
            "src/switchstand/native_http.py",
            "src/switchstand/native_http_contract.py",
            "src/switchstand/native_workbench.py",
            "src/switchstand/service.py",
            "src/switchstand/static/index.html",
            "tests/browser/focus_chromium.spec.js",
            "tests/browser/native_selection_chromium.spec.js",
            "tests/browser_focus.test.js",
            "tests/test_native_http.py",
            "tests/test_native_http_contract.py",
            "tests/test_native_workbench.py",
            "tests/test_service.py",
            "tests/test_service_request_targets.py",
        }
        extensions = self.register["affected_path_extensions"]
        self.assertEqual({item["finding_id"] for item in extensions}, {"A-01", "TE-002"})
        self.assertEqual({path for item in extensions for path in item["paths"]}, expected)
        self.assertTrue(
            expected <= set(audit_register_policy.affected_paths(self.register, ["A-01", "TE-002"]))
        )
        scope = {
            "schema_version": 1,
            "base_sha": "base",
            "kind": "containment",
            "finding_ids": ["A-01", "A-03", "TE-002", "TE-003"],
            "runnable_blocker_id": None,
            "gate_repair_receipts": [],
            "rationale": "Quarantine the reviewed native and legacy control surfaces.",
        }
        with (
            patch.object(check_audit_register, "_load_json", return_value=scope),
            patch.object(check_audit_register, "_changed_paths", return_value=expected),
        ):
            check_audit_register.validate_scope(
                "base",
                self.register,
                self.findings,
                self.register["runnable_surface_paths"],
                self.register["runnable_blockers"],
                ["freeze"],
                current_register=self.register,
            )

    def test_affected_path_extensions_are_append_only_and_cannot_be_repointed(self):
        removed = deepcopy(self.register)
        removed["affected_path_extensions"] = []
        with self.assertRaisesRegex(ValueError, "affected_path_extensions must be append-only"):
            audit_register_policy.validate_transitions(
                self.register, removed, ROOT, check_audit_register.validate_register
            )
        repointed = deepcopy(self.register)
        repointed["affected_path_extensions"][0]["finding_id"] = "TE-003"
        with self.assertRaisesRegex(ValueError, "affected_path_extensions must be append-only"):
            audit_register_policy.validate_transitions(
                self.register, repointed, ROOT, check_audit_register.validate_register
            )

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

    def test_scope_extension_requires_gate_repair_and_later_authorizes_only_exact_paths(self):
        current = deepcopy(self.register)
        extension = {
            "finding_id": "SWSEC-002",
            "paths": ["src/switchstand/app_server_protocol.py", "tests/test_app_server_protocol.py"],
            "rationale": "Bind the separately reviewed strict protocol module and regression tests.",
        }
        current["affected_path_extensions"].append(extension)
        base_sha = "b" * 40
        digest = audit_register_policy.extension_digest([extension])
        subject = f"gate-repair:{base_sha}:{digest}"
        reviews = [
            durable("gate_repair_review", subject, "reviewer-a", "a"),
            durable("gate_repair_review", subject, "reviewer-b", "b"),
        ]
        current.setdefault("gate_repair_history", []).append({
            "base_sha": base_sha,
            "extensions_sha256": digest,
            "review_receipts": reviews,
        })
        with mocked_receipts():
            check_audit_register.validate_register(current)
            audit_register_policy.validate_transitions(
                self.register, current, ROOT, check_audit_register.validate_register
            )
        gate_scope = {
            "schema_version": 1,
            "base_sha": base_sha,
            "kind": "gate_repair",
            "finding_ids": [],
            "runnable_blocker_id": None,
            "gate_repair_receipts": reviews,
            "rationale": "Append exact omitted protocol paths without changing finding identity.",
        }
        with (
            mocked_receipts(),
            patch.object(check_audit_register, "_load_json", return_value=gate_scope),
            patch.object(
                check_audit_register,
                "_changed_paths",
                return_value={"audit/change-scope.json", "audit/findings.json"},
            ),
        ):
            check_audit_register.validate_scope(
                base_sha,
                self.register,
                self.findings,
                self.register["runnable_surface_paths"],
                self.register["runnable_blockers"],
                ["freeze"],
                current_register=current,
            )
        remediation = deepcopy(gate_scope)
        remediation["kind"] = "remediation"
        remediation["finding_ids"] = ["SWSEC-002"]
        remediation["gate_repair_receipts"] = []
        with (
            patch.object(check_audit_register, "_load_json", return_value=remediation),
            patch.object(check_audit_register, "_changed_paths", return_value={"audit/findings.json"}),
            self.assertRaisesRegex(ValueError, "require a two-review gate_repair"),
        ):
            check_audit_register.validate_scope(
                base_sha,
                self.register,
                self.findings,
                self.register["runnable_surface_paths"],
                self.register["runnable_blockers"],
                ["freeze"],
                current_register=current,
            )
        with (
            patch.object(check_audit_register, "_load_json", return_value=remediation),
            patch.object(check_audit_register, "_changed_paths", return_value={"audit/findings.json"}),
            self.assertRaisesRegex(ValueError, "require a two-review gate_repair"),
        ):
            check_audit_register.validate_scope(
                base_sha,
                self.register,
                self.findings,
                self.register["runnable_surface_paths"],
                self.register["runnable_blockers"],
                [],
                current_register=current,
            )
        with (
            patch.object(check_audit_register, "_load_json", return_value=remediation),
            patch.object(
                check_audit_register,
                "_changed_paths",
                return_value={"src/switchstand/app_server_protocol.py"},
            ),
        ):
            check_audit_register.validate_scope(
                base_sha,
                current,
                self.findings,
                current["runnable_surface_paths"],
                current["runnable_blockers"],
                ["freeze"],
                current_register=current,
            )

    def test_gate_repair_history_revalidates_its_durable_receipts(self):
        missing = load_register(include_gate_history=True)
        missing["gate_repair_history"][0]["review_receipts"] = []
        with self.assertRaisesRegex(ValueError, "requires exactly two reviews"):
            check_audit_register.validate_register(missing)

    def test_repository_gate_accepts_completed_receipt_fixture(self):
        register = load_register(include_gate_history=True)
        scope = json.loads((ROOT / "audit" / "change-scope.json").read_text(encoding="utf-8"))
        if not scope["gate_repair_receipts"]:
            scope["gate_repair_receipts"] = register["gate_repair_history"][0]["review_receipts"]
        real_load = check_audit_register._load_json

        def completed_candidate(path: Path) -> dict[str, Any]:
            if path.name == "findings.json":
                return register
            if path.name == "change-scope.json":
                return scope
            return real_load(path)

        with mocked_receipts(), patch.object(
            check_audit_register, "_load_json", side_effect=completed_candidate
        ):
            result = check_audit_register.main([
                "--base-ref", "8e517e344380fdd3da2813d922ee90ad6b8ace35",
                "--now", "2026-08-29T19:00:00Z",
            ])
        self.assertEqual(result, 0)

    def test_reviewed_extension_batch_cannot_be_substituted_after_review(self):
        current = deepcopy(self.register)
        extension = {
            "finding_id": "SWSEC-002",
            "paths": ["src/switchstand/app_server_protocol.py"],
            "rationale": "Bind one reviewed protocol path.",
        }
        substituted = {
            "finding_id": "SWSEC-002",
            "paths": ["tests/test_app_server_protocol.py"],
            "rationale": "Unreviewed extra path.",
        }
        current["affected_path_extensions"].extend([extension, substituted])
        base_sha = "b" * 40
        reviewed_digest = audit_register_policy.extension_digest([extension])
        subject = f"gate-repair:{base_sha}:{reviewed_digest}"
        receipts = [
            durable("gate_repair_review", subject, "reviewer-a", "a"),
            durable("gate_repair_review", subject, "reviewer-b", "b"),
        ]
        current.setdefault("gate_repair_history", []).append({
            "base_sha": base_sha,
            "extensions_sha256": audit_register_policy.extension_digest([extension, substituted]),
            "review_receipts": receipts,
        })
        with mocked_receipts(), self.assertRaises(AssertionError):
            check_audit_register.validate_register(current)

    def test_scope_extensions_reject_unknown_wildcard_duplicate_and_register_side_effects(self):
        for label, extension, message in (
            (
                "unknown",
                {"finding_id": "MISSING-001", "paths": ["src/new.py"], "rationale": "x"},
                "unknown finding",
            ),
            (
                "wildcard",
                {"finding_id": "A-01", "paths": ["src/*.py"], "rationale": "x"},
                "exact repository-relative paths",
            ),
            (
                "duplicate",
                {"finding_id": "A-01", "paths": ["src/switchstand/native_input.py"], "rationale": "x"},
                "duplicate existing scope",
            ),
        ):
            with self.subTest(label=label):
                current = deepcopy(self.register)
                current["affected_path_extensions"].append(extension)
                with self.assertRaisesRegex(ValueError, message):
                    check_audit_register.validate_register(current)
        current = deepcopy(self.register)
        current["findings"][0]["next_action"] = "Silently change finding truth."
        with self.assertRaisesRegex(ValueError, "may only append affected_path_extensions"):
            audit_register_policy.validate_gate_register_repair(self.register, current)

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
