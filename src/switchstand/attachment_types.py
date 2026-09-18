"""Provider-internal attachment metadata for backend adapters."""

from dataclasses import dataclass


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
