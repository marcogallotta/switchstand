"""Provider-neutral work discovery over admitted backend scope."""

from dataclasses import dataclass
from typing import Protocol

from .contracts import Routing, WorkSearchItem, WorkSearchRequest, WorkSearchResult
from .core import Handle, ProviderError


@dataclass(frozen=True)
class ProviderSearchItem:
    provider_work_id: str
    title: str
    completed: bool
    revision: str
    routing: Routing


@dataclass(frozen=True)
class ProviderSearchPage:
    items: tuple[ProviderSearchItem, ...]
    next_cursor: str | None


class DiscoveryProvider(Protocol):
    async def search_work(
        self, text: str | None, completed: bool | None, cursor: str | None, limit: int,
    ) -> ProviderSearchPage: ...


class DiscoveryState(Protocol):
    async def bind(self, provider: str, provider_work_id: str) -> Handle: ...


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
                ))
            return WorkSearchResult(
                status="ok", items=tuple(items), next_cursor=page.next_cursor
            )
        except (ProviderError, TypeError, ValueError):
            return WorkSearchResult(status="provider_error")
