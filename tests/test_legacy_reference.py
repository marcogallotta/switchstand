import pytest

from switchstand.task_ref import ProviderTaskReference, parse_legacy_task_reference


@pytest.mark.parametrize(
    "value",
    [
        "1218242783900077",
        "https://app.asana.com/0/0/1218242783900077/f",
        "https://app.asana.com/0/123/1218242783900077",
        "https://app.asana.com/1/123/project/456/task/1218242783900077",
    ],
)
def test_legacy_task_reference_uses_the_existing_asana_task_grammar(value):
    assert parse_legacy_task_reference(value) == ProviderTaskReference(
        provider="asana", provider_work_id="1218242783900077"
    )


@pytest.mark.parametrize(
    "value",
    [
        "not-an-id",
        "http://app.asana.com/0/0/1218242783900077/f",
        "https://example.com/0/0/1218242783900077/f",
        "https://app.asana.com/0/0/not-an-id/f",
    ],
)
def test_legacy_task_reference_rejects_everything_the_existing_grammar_rejects(value):
    with pytest.raises(ValueError):
        parse_legacy_task_reference(value)
