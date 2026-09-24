"""Provider-neutral work discovery over admitted backend scope."""

from dataclasses import dataclass
from typing import Literal, Protocol

from .contracts import Routing, WorkContext, WorkSearchItem, WorkSearchRequest, WorkSearchResult
from .core import Handle, ProviderError


@dataclass(frozen=True)
class ProviderSearchItem:
    provider_work_id: str
    title: str
    completed: bool
    revision: str
    routing: Routing
    context: WorkContext


@dataclass(frozen=True)
class ProviderSearchPage:
    items: tuple[ProviderSearchItem, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class ProviderStructure:
    status: Literal["ok", "stale"]
    revision: str
    parent: ProviderSearchItem | None = None
    children: tuple[ProviderSearchItem, ...] = ()


@dataclass(frozen=True)
class DiscoveredStructure:
    status: Literal["ok", "stale"]
    revision: str
    parent: WorkSearchItem | None = None
    children: tuple[WorkSearchItem, ...] = ()


class DiscoveryProvider(Protocol):
    async def search_work(
        self, text: str | None, completed: bool | None, cursor: str | None, limit: int,
    ) -> ProviderSearchPage: ...

    async def structure_work(
        self, provider_work_id: str, observed_revision: str,
    ) -> ProviderStructure: ...


class DiscoveryState(Protocol):
    async def bind(self, provider: str, provider_work_id: str) -> Handle: ...
    async def bind_many(
        self, provider: str, provider_work_ids: tuple[str, ...]
    ) -> tuple[Handle, ...]: ...


class WorkDiscovery:
    """Bind admitted provider search results to stable WorkIds before returning them."""

    def __init__(
        self, provider_name: str, provider: DiscoveryProvider, state: DiscoveryState,
    ) -> None:
        if not provider_name:
            raise ValueError("provider name must be non-empty")
        self.provider_name = provider_name
        self.provider = provider
        self.state = state

    async def search(self, request: WorkSearchRequest) -> WorkSearchResult:
        try:
            page = await self.provider.search_work(
                request.text, request.completed, request.cursor, request.limit
            )
            items: list[WorkSearchItem] = []
            for candidate in page.items:
                handle = await self.state.bind(
                    self.provider_name, candidate.provider_work_id
                )
                items.append(WorkSearchItem(
                    id=handle.id,
                    title=candidate.title,
                    completed=candidate.completed,
                    revision=candidate.revision,
                    routing=candidate.routing,
                    context=candidate.context,
                ))
            return WorkSearchResult(
                status="ok", items=tuple(items), next_cursor=page.next_cursor
            )
        except (ProviderError, TypeError, ValueError):
            return WorkSearchResult(status="provider_error")

    async def structure(
        self, provider_work_id: str, observed_revision: str,
    ) -> DiscoveredStructure | None:
        """Bind relations only after the provider verifies one complete snapshot."""
        try:
            result = await self.provider.structure_work(
                provider_work_id, observed_revision
            )
            if result.status == "stale":
                return DiscoveredStructure(status="stale", revision=result.revision)

            relations = ((result.parent,) if result.parent is not None else ()) + result.children
            relation_ids = tuple(candidate.provider_work_id for candidate in relations)
            if provider_work_id in relation_ids or len(set(relation_ids)) != len(relation_ids):
                return None
            handles = await self.state.bind_many(
                self.provider_name, relation_ids,
            )

            def project(candidate: ProviderSearchItem, handle: Handle) -> WorkSearchItem:
                return WorkSearchItem(
                    id=handle.id, title=candidate.title,
                    completed=candidate.completed, revision=candidate.revision,
                    routing=candidate.routing, context=candidate.context,
                )

            items = tuple(project(candidate, handle) for candidate, handle in zip(
                relations, handles, strict=True
            ))
            parent = items[0] if result.parent is not None else None
            children = items[1:] if result.parent is not None else items
            return DiscoveredStructure(
                status="ok", revision=result.revision,
                parent=parent, children=children,
            )
        except (ProviderError, TypeError, ValueError):
            return None
