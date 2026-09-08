import pytest

from switchstand.provision import asana_task_id


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1218242783900077", "1218242783900077"),
        ("https://app.asana.com/0/0/1218242783900077/f", "1218242783900077"),
        ("https://app.asana.com/0/123/1218242783900077", "1218242783900077"),
        (
            "https://app.asana.com/1/123/project/456/task/1218242783900077",
            "1218242783900077",
        ),
    ],
)
def test_asana_task_id_accepts_ids_and_task_urls(value, expected):
    assert asana_task_id(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "not-an-id",
        "http://app.asana.com/0/0/1218242783900077/f",
        "https://example.com/0/0/1218242783900077/f",
        "https://app.asana.com/0/0/not-an-id/f",
    ],
)
def test_asana_task_id_rejects_ambiguous_input(value):
    with pytest.raises(ValueError):
        asana_task_id(value)
