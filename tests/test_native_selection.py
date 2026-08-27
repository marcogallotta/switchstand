from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any
import unittest

from switchstand.native_selection import resolve_native_selection, validate_native_selection_v1


FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "native_selection_v1.json").read_text(encoding="utf-8")
)


def materialize(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"hugeInteger"}:
        return int(value["hugeInteger"])
    if isinstance(value, dict):
        return {key: materialize(item) for key, item in value.items()}
    return value


def expanded_case(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    selection = deepcopy(FIXTURES["baseSelection"])
    observation = deepcopy(FIXTURES["baseObservation"])
    selection.update(materialize(case.get("selectionPatch", {})))
    observation.update(materialize(case.get("observationPatch", {})))
    if "recordPatch" in case:
        observation["agentRecords"][0].update(case["recordPatch"])
    return selection, observation


def expected(case: dict[str, Any]) -> dict[str, Any]:
    if case.get("expectedBase"):
        return FIXTURES["baseExpected"]
    if "expectedError" in case:
        code = case["expectedError"]
        return {"code": code, "message": FIXTURES["errorMessages"][code]}
    return case["expected"]


class NativeSelectionContractTests(unittest.TestCase):
    def test_shared_resolution_cases(self):
        for case in FIXTURES["resolveCases"]:
            with self.subTest(case=case["id"]):
                selection, observation = expanded_case(case)
                result = resolve_native_selection(
                    selection,
                    observation,
                    now=materialize(case["now"]),
                    maximum_observation_age_seconds=materialize(
                        case["maximumObservationAgeSeconds"]
                    ),
                )
                self.assertEqual(result, expected(case))

    def test_privacy_classes_are_omitted_by_projection_and_rejected_by_closed_dto(self):
        for privacy_class in FIXTURES["privacyClasses"]:
            with self.subTest(privacy_class=privacy_class["id"]):
                observation = deepcopy(FIXTURES["baseObservation"])
                observation["agentRecords"][0].update(privacy_class["fields"])
                result = resolve_native_selection(
                    FIXTURES["baseSelection"],
                    observation,
                    now=100,
                    maximum_observation_age_seconds=5,
                )
                self.assertEqual(result, FIXTURES["baseExpected"])
                candidate = {**FIXTURES["baseExpected"], **privacy_class["fields"]}
                with self.assertRaisesRegex(ValueError, "^invalid native-selection-v1 DTO$"):
                    validate_native_selection_v1(candidate)

    def test_closed_dto_rejects_unknown_and_missing_fields(self):
        unknown = {**FIXTURES["baseExpected"], **FIXTURES["unknownDtoFields"]}
        missing = {**FIXTURES["baseExpected"]}
        missing.pop("present")

        for candidate in (unknown, missing):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(ValueError, "^invalid native-selection-v1 DTO$"):
                    validate_native_selection_v1(candidate)

    def test_duplicate_agent_ref_fails_closed_instead_of_retargeting(self):
        observation = deepcopy(FIXTURES["baseObservation"])
        observation["agentRecords"].append(deepcopy(observation["agentRecords"][0]))

        result = resolve_native_selection(
            FIXTURES["baseSelection"],
            observation,
            now=100,
            maximum_observation_age_seconds=5,
        )

        self.assertEqual(
            result,
            {
                "code": "INVALID_AGENT_REF",
                "message": FIXTURES["errorMessages"]["INVALID_AGENT_REF"],
            },
        )


if __name__ == "__main__":
    unittest.main()
