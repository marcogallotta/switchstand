import httpx
import pytest

from switchstand.core import ProviderError
from switchstand.provider import AsanaProvider


def attachment(gid="2001", parent="1001", **changes):
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
    page = await provider.list_attachments("1001", "cursor", 25)
    assert page.attachments[0].provider_attachment_id == "2001"
    assert page.attachments[0].provider_work_id == "1001"
    assert page.next_cursor == "more"
    request = requests[0]
    assert request.url.path.endswith("/attachments")
    assert request.url.params["parent"] == "1001"
    assert request.url.params["offset"] == "cursor"
    assert request.url.params["limit"] == "25"


@pytest.mark.parametrize(
    "problem",
    ["wrong_parent", "duplicate", "bad_cursor", "unsafe_url", "malformed_https", "credential_url"],
)
async def test_asana_attachment_list_rejects_invalid_provider_evidence(problem):
    rows = [attachment()]
    next_page: object = None
    if problem == "wrong_parent":
        rows = [attachment(parent="1002")]
    elif problem == "duplicate":
        rows = [attachment(), attachment()]
    elif problem == "bad_cursor":
        next_page = {"offset": ""}
    elif problem == "unsafe_url":
        rows = [attachment(download_url="http://unsafe.example/evidence")]
    elif problem == "malformed_https":
        rows = [attachment(download_url="https:///missing-host")]
    else:
        rows = [attachment(view_url="https://user:pass@view.example/evidence")]
    provider, _requests = asana_provider({"data": rows, "next_page": next_page})
    with pytest.raises(ProviderError):
        await provider.list_attachments("1001", None, 50)


async def test_asana_exact_attachment_read_validates_identity_and_https_pointer():
    provider, requests = asana_provider({"data": attachment()})
    value = await provider.get_attachment("2001")
    assert value is not None and value.provider_work_id == "1001"
    assert requests[0].url.path.endswith("/attachments/2001")

    mismatch, _ = asana_provider({"data": attachment(gid="2002")})
    with pytest.raises(ProviderError):
        await mismatch.get_attachment("2001")

    missing, _ = asana_provider((404, {"data": None}))
    assert await missing.get_attachment("2001") is None


@pytest.mark.parametrize(
    "cursor, limit",
    [("", 50), ("x" * 1025, 50), (None, 0), (None, 101)],
)
async def test_asana_attachment_list_rejects_unbounded_cursor_or_limit(cursor, limit):
    provider, requests = asana_provider({"data": [], "next_page": None})
    with pytest.raises(ProviderError):
        await provider.list_attachments("1001", cursor, limit)
    assert requests == []


async def test_asana_attachment_path_rejects_non_gid_before_request():
    provider, requests = asana_provider({"data": attachment()})
    with pytest.raises(ProviderError):
        await provider.get_attachment("../tasks/999")
    assert requests == []


async def test_asana_attachment_payload_requires_numeric_provider_ids():
    bad_attachment, _ = asana_provider({"data": attachment(gid="not-a-gid")})
    with pytest.raises(ProviderError):
        await bad_attachment.get_attachment("2001")

    bad_parent, _ = asana_provider({"data": [attachment(parent="not-a-gid")], "next_page": None})
    with pytest.raises(ProviderError):
        await bad_parent.list_attachments("1001", None, 50)
