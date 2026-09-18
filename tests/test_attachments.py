from uuid import UUID, uuid4

import httpx
import pytest

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
from switchstand.core import Handle, ProviderError, ProviderWork
from switchstand.provider import AsanaProvider
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
        if self.list_calls and self.after_revision is not None:
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


def attachment(gid="attachment-1", parent="task-1", **changes):
    value = {
        "gid": gid,
        "name": "evidence.txt",
        "parent": {"gid": parent},
        "download_url": "https://download.example/evidence",
        "view_url": "https://view.example/evidence",
    }
    return value | changes


def asana_provider(*payloads):
    pending = list(payloads)
    requests: list[httpx.Request] = []

    def answer(request):
        requests.append(request)
        payload = pending.pop(0)
        status = 200
        if isinstance(payload, tuple):
            status, payload = payload
        return httpx.Response(status, request=request, json=payload)

    client = httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0",
        transport=httpx.MockTransport(answer),
    )
    return AsanaProvider(client), requests


async def test_asana_attachment_list_uses_exact_parent_cursor_and_safe_fields():
    provider, requests = asana_provider({
        "data": [attachment()],
        "next_page": {"offset": "more"},
    })
    page = await provider.list_attachments("task-1", "cursor", 25)
    assert page.attachments[0].provider_attachment_id == "attachment-1"
    assert page.attachments[0].provider_work_id == "task-1"
    assert page.next_cursor == "more"
    request = requests[0]
    assert request.url.path.endswith("/attachments")
    assert request.url.params["parent"] == "task-1"
    assert request.url.params["offset"] == "cursor"
    assert request.url.params["limit"] == "25"


@pytest.mark.parametrize("problem", ["wrong_parent", "duplicate", "bad_cursor", "unsafe_url"])
async def test_asana_attachment_list_rejects_invalid_provider_evidence(problem):
    rows = [attachment()]
    next_page: object = None
    if problem == "wrong_parent":
        rows = [attachment(parent="task-2")]
    elif problem == "duplicate":
        rows = [attachment(), attachment()]
    elif problem == "bad_cursor":
        next_page = {"offset": ""}
    else:
        rows = [attachment(download_url="http://unsafe.example/evidence")]
    provider, _requests = asana_provider({"data": rows, "next_page": next_page})
    with pytest.raises(ProviderError):
        await provider.list_attachments("task-1", None, 50)


async def test_asana_exact_attachment_read_validates_identity_and_https_pointer():
    provider, requests = asana_provider({"data": attachment()})
    value = await provider.get_attachment("attachment-1")
    assert value is not None and value.provider_work_id == "task-1"
    assert requests[0].url.path.endswith("/attachments/attachment-1")

    mismatch, _ = asana_provider({"data": attachment(gid="other")})
    with pytest.raises(ProviderError):
        await mismatch.get_attachment("attachment-1")

    missing, _ = asana_provider((404, {"data": None}))
    assert await missing.get_attachment("attachment-1") is None
