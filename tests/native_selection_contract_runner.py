"""Test-only JSON bridge from Node to the production Python resolver."""
from __future__ import annotations

from copy import deepcopy
import json
import sys
from typing import Any

from switchstand.native_selection import resolve_native_selection, validate_native_selection_v1


def materialize(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"hugeInteger"}:
        return int(value["hugeInteger"])
    if isinstance(value, dict):
        return {key: materialize(item) for key, item in value.items()}
    return value


def main() -> None:
    payload = json.load(sys.stdin)
    base_selection = payload["baseSelection"]
    base_observation = payload["baseObservation"]
    results = []
    for case in payload["resolveCases"]:
        selection = deepcopy(base_selection)
        observation = deepcopy(base_observation)
        selection.update(materialize(case.get("selectionPatch", {})))
        observation.update(materialize(case.get("observationPatch", {})))
        if "recordPatch" in case:
            observation["agentRecords"][0].update(case["recordPatch"])
        results.append(
            resolve_native_selection(
                selection,
                observation,
                now=materialize(case["now"]),
                maximum_observation_age_seconds=materialize(
                    case["maximumObservationAgeSeconds"]
                ),
            )
        )

    privacy_results: list[dict[str, Any]] = []
    for privacy_class in payload["privacyClasses"]:
        observation = deepcopy(base_observation)
        observation["agentRecords"][0].update(privacy_class["fields"])
        projected = resolve_native_selection(
            base_selection,
            observation,
            now=100,
            maximum_observation_age_seconds=5,
        )
        candidate = {**payload["baseExpected"], **privacy_class["fields"]}
        try:
            validate_native_selection_v1(candidate)
            rejected = False
        except ValueError:
            rejected = True
        privacy_results.append({"projected": projected, "rejected": rejected})

    unknown = {**payload["baseExpected"], **payload["unknownDtoFields"]}
    try:
        validate_native_selection_v1(unknown)
        unknown_rejected = False
    except ValueError:
        unknown_rejected = True
    json.dump(
        {
            "resolveResults": results,
            "privacyResults": privacy_results,
            "unknownRejected": unknown_rejected,
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
