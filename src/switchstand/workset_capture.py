"""Inert, deterministic capture of provider workset structure."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Protocol


@dataclass(frozen=True, order=True)
class ProviderSection:
    provider_section_id: str
    name: str


@dataclass(frozen=True, order=True)
class ProviderPlacement:
    provider_project_id: str
    provider_section_id: str | None = None


@dataclass(frozen=True)
class ProviderProject:
    provider_project_id: str
    name: str
    revision: str
    archived: bool
    sections: tuple[ProviderSection, ...]


@dataclass(frozen=True)
class ProviderTaskStructure:
    provider_work_id: str
    revision: str
    parent_provider_work_id: str | None
    placements: tuple[ProviderPlacement, ...]


class StructureProvider(Protocol):
    def workset_project_ids(self) -> tuple[str, ...]: ...

    async def workset_task(self, provider_work_id: str) -> ProviderTaskStructure: ...

    async def workset_project(self, provider_project_id: str) -> ProviderProject: ...


@dataclass(frozen=True)
class ProviderStructureCapture:
    source_candidate: str
    projects: tuple[ProviderProject, ...]
    tasks: tuple[ProviderTaskStructure, ...]

    def validated(self) -> ProviderStructureCapture:
        projects = {row.provider_project_id: row for row in self.projects}
        tasks = {row.provider_work_id: row for row in self.tasks}
        if not self.source_candidate or len(projects) != len(self.projects):
            raise ValueError("provider capture has an invalid source or duplicate project")
        if len(tasks) != len(self.tasks):
            raise ValueError("provider capture contains duplicate work identities")
        if tuple(projects) != tuple(sorted(projects)) or tuple(tasks) != tuple(sorted(tasks)):
            raise ValueError("provider capture is not in canonical identity order")
        section_projects: dict[str, str] = {}
        for project in self.projects:
            if not project.provider_project_id or not project.name or not project.revision:
                raise ValueError("provider project identity, name, and revision are required")
            if tuple(sorted(project.sections)) != project.sections:
                raise ValueError("provider sections are not in canonical order")
            for section in project.sections:
                if (
                    not section.provider_section_id
                    or not section.name
                    or section.provider_section_id in section_projects
                ):
                    raise ValueError("provider section identity is invalid or ambiguous")
                section_projects[section.provider_section_id] = project.provider_project_id
        for task in self.tasks:
            if not task.provider_work_id or not task.revision:
                raise ValueError("provider work identity and revision are required")
            if task.parent_provider_work_id is not None and task.parent_provider_work_id not in tasks:
                raise ValueError("parent points outside the captured corpus")
            if (
                len(set(task.placements)) != len(task.placements)
                or tuple(sorted(task.placements)) != task.placements
            ):
                raise ValueError("provider memberships are duplicated or not in canonical order")
            for placement in task.placements:
                if placement.provider_project_id not in projects:
                    raise ValueError("membership points to an uncaptured project")
                if (
                    placement.provider_section_id is not None
                    and section_projects.get(placement.provider_section_id)
                    != placement.provider_project_id
                ):
                    raise ValueError("membership section does not belong to its project")
        return self

    def digest(self) -> str:
        payload = json.dumps(
            asdict(self), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
        return hashlib.sha256(payload).hexdigest()


async def capture_structure(
    provider: StructureProvider,
    provider_work_ids: tuple[str, ...],
    source_candidate: str,
) -> ProviderStructureCapture:
    """Read provider structure twice and return only an exact stable capture."""
    work_ids = tuple(sorted(provider_work_ids))
    if not work_ids or len(set(work_ids)) != len(work_ids):
        raise ValueError("provider work identities must be nonempty and unique")
    first_tasks = tuple([await provider.workset_task(value) for value in work_ids])
    project_ids = tuple(sorted(
        set(provider.workset_project_ids())
        | {
            placement.provider_project_id
            for task in first_tasks
            for placement in task.placements
        }
    ))
    first_projects = tuple([await provider.workset_project(value) for value in project_ids])
    second_tasks = tuple([await provider.workset_task(value) for value in work_ids])
    second_projects = tuple([await provider.workset_project(value) for value in project_ids])
    if (
        tuple(row.provider_work_id for row in first_tasks) != work_ids
        or tuple(row.provider_project_id for row in first_projects) != project_ids
        or first_tasks != second_tasks
        or first_projects != second_projects
    ):
        raise ValueError("provider structure changed during capture")
    return ProviderStructureCapture(
        source_candidate, first_projects, first_tasks
    ).validated()
