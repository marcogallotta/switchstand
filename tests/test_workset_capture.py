import pytest

from switchstand.workset_capture import (
    ProviderPlacement,
    ProviderProject,
    ProviderSection,
    ProviderStructureCapture,
    ProviderTaskStructure,
    capture_structure,
)


class Provider:
    def __init__(self, *, drift: bool = False, wrong_identity: bool = False):
        self.drift = drift
        self.wrong_identity = wrong_identity
        self.task_reads = 0

    def workset_project_ids(self):
        return ("project-empty", "project-role")

    async def workset_task(self, provider_work_id):
        self.task_reads += 1
        identity = "forged" if self.wrong_identity and provider_work_id == "master" else provider_work_id
        revision = "changed" if self.drift and self.task_reads > 2 else "r1"
        return ProviderTaskStructure(
            identity,
            revision,
            "master" if provider_work_id == "child" else None,
            (ProviderPlacement("project-role", "section-current"),),
        )

    async def workset_project(self, provider_project_id):
        sections = (
            (ProviderSection("section-current", "CURRENT"),)
            if provider_project_id == "project-role"
            else ()
        )
        return ProviderProject(provider_project_id, provider_project_id, "p1", False, sections)


async def test_capture_is_stable_canonical_and_includes_empty_configured_projects():
    capture = await capture_structure(Provider(), ("master", "child"), "candidate-sha")

    assert [row.provider_work_id for row in capture.tasks] == ["child", "master"]
    assert [row.provider_project_id for row in capture.projects] == [
        "project-empty", "project-role",
    ]
    assert capture.tasks[0].parent_provider_work_id == "master"
    assert capture.digest() == capture.digest() and len(capture.digest()) == 64


@pytest.mark.parametrize("provider", [Provider(drift=True), Provider(wrong_identity=True)])
async def test_capture_rejects_drift_or_wrong_provider_identity(provider):
    with pytest.raises(ValueError, match="changed during capture"):
        await capture_structure(provider, ("master", "child"), "candidate-sha")


def test_capture_rejects_section_membership_from_another_project():
    capture = ProviderStructureCapture(
        "candidate-sha",
        (
            ProviderProject("project-a", "A", "p1", False, (ProviderSection("section-a", "A"),)),
            ProviderProject("project-b", "B", "p2", False, ()),
        ),
        (
            ProviderTaskStructure(
                "task", "r1", None, (ProviderPlacement("project-b", "section-a"),)
            ),
        ),
    )

    with pytest.raises(ValueError, match="does not belong"):
        capture.validated()
