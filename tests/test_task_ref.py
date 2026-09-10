import pytest

from switchstand.task_ref import split_asana_launch_spec


def test_launch_spec_is_removed_and_references_are_normalized():
    notes = "Human context\n<!-- switchstand-launch-spec\n" + (
        '{"version":"1","required_references":['
        '"1218242783900077","https://app.asana.com/0/0/1218242783900088/f"]}'
    ) + "\n-->\n"
    assert split_asana_launch_spec(notes) == (
        "Human context",
        ("1218242783900077", "1218242783900088"),
    )


def test_notes_without_launch_spec_are_unchanged():
    assert split_asana_launch_spec("Human context\n") == ("Human context\n", ())


@pytest.mark.parametrize(
    "notes",
    [
        '<!-- switchstand-launch-spec\n{"version":"2"}\n-->',
        '<!-- switchstand-launch-spec\n{"version":"1","extra":true}\n-->',
        '<!-- switchstand-launch-spec\n{"version":"1","required_references":[""]}\n-->',
        "<!-- switchstand-launch-spec\n{}",
        "x <!-- switchstand-launch-spec\n{}\n-->",
    ],
)
def test_invalid_launch_spec_fails_closed(notes):
    with pytest.raises(ValueError, match="LaunchSpec"):
        split_asana_launch_spec(notes)
