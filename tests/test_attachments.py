from uuid import UUID, uuid4

from switchstand.attachments import (
    ProviderAttachment,
    ProviderAttachmentPage,
    WorkAttachments,
)
from switchstand.contracts import (
    LaunchAuthority,
    Routing,
    WorkAttachmentRequest,
    WorkAttachmentsRequest,
)
from switchstand.core import Handle, ProviderWork
from switchstand.state import AttachmentHandle

WORK_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ID = UUID("00000000-0000-0000-0000-000000000002")


class MemoryState:
    def __init__(self):
        self.handles = {
            WORK_ID: Handle(WORK_ID, "asana", "task-1"),
            OTHER_ID: Handle(OTHER_ID, "asana", "task-2"),
        }
        self.by_source: dict[tuple[UUID, str], AttachmentHandle] = {}
        self.by_id: dict[tuple[UUID, UUID], AttachmentHandle] = {}

    async def get(self, work_id):
        return self.handles.get(work_id)

    async def bind_attachment(
        self, work_id, provider, provider_work_id, provider_attachment_id
    ):
        handle = self.handles.get(work_id)
        if (
            handle is None or handle.provider != provider
            or handle.provider_work_id != provider_work_id
        ):
            raise ValueError("attachment target does not match work binding")
        key = work_id, provider_attachment_id
        if key not in self.by_source:
            value = AttachmentHandle(
                uuid4(), work_id, provider, provider_work_id, provider_attachment_id
            )
            self.by_source[key] = value
            self.by_id[work_id, value.id] = value
        return self.by_source[key]

    async def get_attachment(self, work_id, attachment_id):
        return self.by_id.get((work_id, attachment_id))


class FakeProvider:
    def __init__(self):
        self.revision = "r1"
        self.canonical = True
        self.after_revision: str | None = None
        self.list_calls = 0
        self.get_attachment_calls = 0
        self.wrong_parent = False

    async def get(self, provider_work_id):
        revision = self.revision
        if (self.list_calls or self.get_attachment_calls) and self.after_revision is not None:
            revision = self.after_revision
        return ProviderWork(
            "Task", "Notes", False, revision, Routing(priority="P0"), self.canonical
        )

    async def list_attachments(self, provider_work_id, cursor, limit):
        self.list_calls += 1
        parent = "other-task" if self.wrong_parent else provider_work_id
        return ProviderAttachmentPage(
            attachments=(
                ProviderAttachment(
                    "attachment-1", parent, "evidence.txt",
                    "https://download.example/evidence", "https://view.example/evidence",
                ),
            ),
            next_cursor="next-page",
        )

    async def get_attachment(self, provider_attachment_id):
        self.get_attachment_calls += 1
        parent = "other-task" if self.wrong_parent else "task-1"
        return ProviderAttachment(
            provider_attachment_id, parent, "evidence.txt",
            "https://download.example/evidence", "https://view.example/evidence",
        )


async def test_authorized_attachment_list_returns_stable_opaque_identity():
    provider, state = FakeProvider(), MemoryState()
    subject = WorkAttachments(
        LaunchAuthority(active_work_id=WORK_ID), state, {"asana": provider}
    )
    request = WorkAttachmentsRequest(
        api_version="1", work_id=WORK_ID, observed_revision="r1", limit=10
    )
    first = await subject.list(request)
    second = await subject.list(request)
    assert first.status == second.status == "ok"
    assert first.next_cursor == second.next_cursor == "next-page"
    assert first.attachments[0].id == second.attachments[0].id
    assert first.attachments[0].work_id == WORK_ID
    assert first.attachments[0].name == "evidence.txt"
    assert first.attachments[0].download_url == "https://download.example/evidence"
    rendered = first.model_dump(mode="json")
    assert "attachment-1" not in str(rendered)
    assert "provider_attachment_id" not in str(rendered)


async def test_attachment_list_enforces_authority_canonicality_and_revision():
    provider, state = FakeProvider(), MemoryState()
    subject = WorkAttachments(
        LaunchAuthority(active_work_id=WORK_ID), state, {"asana": provider}
    )
    denied = await subject.list(WorkAttachmentsRequest(
        api_version="1", work_id=OTHER_ID, observed_revision="r1"
    ))
    assert denied.status == "denied" and provider.list_calls == 0

    stale = await subject.list(WorkAttachmentsRequest(
        api_version="1", work_id=WORK_ID, observed_revision="old"
    ))
    assert stale.status == "stale" and stale.revision == "r1"
    assert provider.list_calls == 0

    provider.canonical = False
    removed = await subject.list(WorkAttachmentsRequest(
        api_version="1", work_id=WORK_ID, observed_revision="r1"
    ))
    assert removed.status == "denied" and provider.list_calls == 0


async def test_attachment_page_stale_or_wrong_parent_never_claims_data():
    provider, state = FakeProvider(), MemoryState()
    subject = WorkAttachments(
        LaunchAuthority(active_work_id=WORK_ID), state, {"asana": provider}
    )
    provider.after_revision = "r2"
    stale = await subject.list(WorkAttachmentsRequest(
        api_version="1", work_id=WORK_ID, observed_revision="r1"
    ))
    assert stale.status == "stale" and stale.revision == "r2"
    assert stale.attachments == ()

    provider = FakeProvider()
    provider.wrong_parent = True
    subject = WorkAttachments(
        LaunchAuthority(active_work_id=WORK_ID), state, {"asana": provider}
    )
    wrong = await subject.list(WorkAttachmentsRequest(
        api_version="1", work_id=WORK_ID, observed_revision="r1"
    ))
    assert wrong.status == "provider_error" and wrong.attachments == ()


async def test_exact_attachment_read_requires_bound_id_exact_work_and_current_revision():
    provider, state = FakeProvider(), MemoryState()
    subject = WorkAttachments(
        LaunchAuthority(active_work_id=WORK_ID), state, {"asana": provider}
    )
    page = await subject.list(WorkAttachmentsRequest(
        api_version="1", work_id=WORK_ID, observed_revision="r1"
    ))
    attachment_id = page.attachments[0].id

    exact = await subject.get(WorkAttachmentRequest(
        api_version="1", work_id=WORK_ID, attachment_id=attachment_id,
        observed_revision="r1",
    ))
    assert exact.status == "ok" and exact.item is not None
    assert exact.item.id == attachment_id and exact.item.work_id == WORK_ID

    cross_work = await WorkAttachments(
        LaunchAuthority(active_work_id=OTHER_ID), state, {"asana": provider}
    ).get(WorkAttachmentRequest(
        api_version="1", work_id=OTHER_ID, attachment_id=attachment_id,
        observed_revision="r1",
    ))
    assert cross_work.status == "denied"

    provider.wrong_parent = True
    wrong_parent = await subject.get(WorkAttachmentRequest(
        api_version="1", work_id=WORK_ID, attachment_id=attachment_id,
        observed_revision="r1",
    ))
    assert wrong_parent.status == "denied"


async def test_exact_attachment_read_denies_unauthorized_work_before_provider_io():
    provider, state = FakeProvider(), MemoryState()
    subject = WorkAttachments(
        LaunchAuthority(active_work_id=WORK_ID), state, {"asana": provider}
    )
    denied = await subject.get(WorkAttachmentRequest(
        api_version="1", work_id=OTHER_ID, attachment_id=uuid4(),
        observed_revision="r1",
    ))
    assert denied.status == "denied"
    assert provider.get_attachment_calls == 0


async def test_exact_attachment_read_rechecks_revision_after_provider_fetch():
    provider, state = FakeProvider(), MemoryState()
    subject = WorkAttachments(
        LaunchAuthority(active_work_id=WORK_ID), state, {"asana": provider}
    )
    page = await subject.list(WorkAttachmentsRequest(
        api_version="1", work_id=WORK_ID, observed_revision="r1"
    ))
    attachment_id = page.attachments[0].id

    provider.list_calls = 0
    provider.get_attachment_calls = 0
    provider.after_revision = "r2"
    stale = await subject.get(WorkAttachmentRequest(
        api_version="1", work_id=WORK_ID, attachment_id=attachment_id,
        observed_revision="r1",
    ))
    assert stale.status == "stale" and stale.revision == "r2"
    assert stale.item is None
    assert provider.get_attachment_calls == 1
