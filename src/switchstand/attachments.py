"""Authorized provider-neutral attachment reads over durable WorkIds."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from .contracts import (
    LaunchAuthority,
    WorkAttachment,
    WorkAttachmentRequest,
    WorkAttachmentResult,
    WorkAttachmentsRequest,
    WorkAttachmentsResult,
)
from .core import Handle, ProviderError, ProviderWork
from .state import AttachmentHandle


@dataclass(frozen=True)
class ProviderAttachment:
    provider_attachment_id: str
    provider_work_id: str
    name: str
    download_url: str | None
    view_url: str | None


@dataclass(frozen=True)
class ProviderAttachmentPage:
    attachments: tuple[ProviderAttachment, ...]
    next_cursor: str | None


class AttachmentProvider(Protocol):
    async def get(self, provider_work_id: str) -> ProviderWork | None: ...

    async def list_attachments(
        self, provider_work_id: str, cursor: str | None, limit: int,
    ) -> ProviderAttachmentPage: ...

    async def get_attachment(
        self, provider_attachment_id: str,
    ) -> ProviderAttachment | None: ...


class AttachmentState(Protocol):
    async def get(self, work_id: UUID) -> Handle | None: ...

    async def bind_attachment(
        self, work_id: UUID, provider: str, provider_work_id: str, provider_attachment_id: str,
    ) -> AttachmentHandle: ...

    async def get_attachment(
        self, work_id: UUID, attachment_id: UUID,
    ) -> AttachmentHandle | None: ...


class WorkAttachments:
    def __init__(
        self,
        authority: LaunchAuthority,
        state: AttachmentState,
        providers: dict[str, AttachmentProvider],
    ) -> None:
        self.authority = authority
        self.state = state
        self.providers = providers

    @staticmethod
    def _item(
        work_id: UUID, attachment_id: UUID, attachment: ProviderAttachment,
    ) -> WorkAttachment:
        return WorkAttachment(
            id=attachment_id,
            work_id=work_id,
            name=attachment.name,
            download_url=attachment.download_url,
            view_url=attachment.view_url,
        )

    async def list(self, request: WorkAttachmentsRequest) -> WorkAttachmentsResult:
        if not self.authority.can_read(request.work_id):
            return WorkAttachmentsResult(status="denied")
        try:
            handle = await self.state.get(request.work_id)
            if handle is None:
                return WorkAttachmentsResult(status="unknown")
            provider = self.providers.get(handle.provider)
            if provider is None:
                return WorkAttachmentsResult(status="provider_error")
            before = await provider.get(handle.provider_work_id)
            if before is None:
                return WorkAttachmentsResult(status="unknown")
            if not before.canonical:
                return WorkAttachmentsResult(status="denied")
            if before.revision != request.observed_revision:
                return WorkAttachmentsResult(
                    status="stale", work_id=request.work_id, revision=before.revision
                )
            page = await provider.list_attachments(
                handle.provider_work_id, request.cursor, request.limit
            )
            if len(page.attachments) > request.limit or any(
                item.provider_work_id != handle.provider_work_id
                for item in page.attachments
            ):
                return WorkAttachmentsResult(status="provider_error")
            after = await provider.get(handle.provider_work_id)
            if after is None:
                return WorkAttachmentsResult(status="unknown")
            if not after.canonical:
                return WorkAttachmentsResult(status="denied")
            if after.revision != before.revision:
                return WorkAttachmentsResult(
                    status="stale", work_id=request.work_id, revision=after.revision
                )
            attachments: list[WorkAttachment] = []
            for item in page.attachments:
                binding = await self.state.bind_attachment(
                    request.work_id,
                    handle.provider,
                    handle.provider_work_id,
                    item.provider_attachment_id,
                )
                attachments.append(self._item(request.work_id, binding.id, item))
            return WorkAttachmentsResult(
                status="ok",
                work_id=request.work_id,
                revision=after.revision,
                attachments=tuple(attachments),
                next_cursor=page.next_cursor,
            )
        except (ProviderError, TypeError, ValueError):
            return WorkAttachmentsResult(status="provider_error")

    async def get(self, request: WorkAttachmentRequest) -> WorkAttachmentResult:
        if not self.authority.can_read(request.work_id):
            return WorkAttachmentResult(status="denied")
        try:
            handle = await self.state.get(request.work_id)
            if handle is None:
                return WorkAttachmentResult(status="unknown")
            binding = await self.state.get_attachment(
                request.work_id, request.attachment_id
            )
            if (
                binding is None
                or binding.provider != handle.provider
                or binding.provider_work_id != handle.provider_work_id
            ):
                return WorkAttachmentResult(status="denied")
            provider = self.providers.get(handle.provider)
            if provider is None:
                return WorkAttachmentResult(status="provider_error")
            before = await provider.get(handle.provider_work_id)
            if before is None:
                return WorkAttachmentResult(status="unknown")
            if not before.canonical:
                return WorkAttachmentResult(status="denied")
            if before.revision != request.observed_revision:
                return WorkAttachmentResult(
                    status="stale", work_id=request.work_id, revision=before.revision
                )
            attachment = await provider.get_attachment(binding.provider_attachment_id)
            if attachment is None:
                return WorkAttachmentResult(status="unknown")
            if attachment.provider_work_id != handle.provider_work_id:
                return WorkAttachmentResult(status="denied")
            after = await provider.get(handle.provider_work_id)
            if after is None:
                return WorkAttachmentResult(status="unknown")
            if not after.canonical:
                return WorkAttachmentResult(status="denied")
            if after.revision != before.revision:
                return WorkAttachmentResult(
                    status="stale", work_id=request.work_id, revision=after.revision
                )
            return WorkAttachmentResult(
                status="ok",
                work_id=request.work_id,
                revision=after.revision,
                item=self._item(request.work_id, request.attachment_id, attachment),
            )
        except (ProviderError, TypeError, ValueError):
            return WorkAttachmentResult(status="provider_error")
