from uuid import uuid4

import httpx
import pytest

from switchstand.core import ProviderError, UnknownEffect
from switchstand.provider import PROJECT
from switchstand.test_create_provider import TestCreateAsanaProvider

TEST_PROJECT = "999001"
CORRELATION_FIELD = "999002"
PARENT = "100"
CREATED = "200"


def response(method, path, payload):
    request = httpx.Request(method, f"https://example.test{path}")
    return httpx.Response(200, request=request, json=payload)


class Client:
    def __init__(self, operation_id):
        self.operation_id = operation_id
        self.create_json = None
        self.create_calls = 0
        self.search_rows = [{"gid": CREATED}]
        self.fail_created_readback_once = False
        self.parent_project = TEST_PROJECT

    async def request(self, method, path, json):
        assert method == "POST" and path == "/tasks"
        self.create_calls += 1
        self.create_json = json
        return response(method, path, {"data": {"gid": CREATED}})

    async def get(self, path, params=None):
        if path == f"/tasks/{PARENT}":
            return response("GET", path, {"data": {
                "gid": PARENT, "name": "Parent", "notes": "", "completed": False,
                "modified_at": "r1", "parent": None, "custom_fields": [],
                "memberships": [{"project": {"gid": self.parent_project}}],
            }})
        if path == f"/tasks/{CREATED}":
            if self.fail_created_readback_once:
                self.fail_created_readback_once = False
                request = httpx.Request("GET", f"https://example.test{path}")
                raise httpx.ReadTimeout("injected lost verification response", request=request)
            return response("GET", path, {"data": {
                "gid": CREATED, "name": "Created", "notes": "notes", "completed": False,
                "modified_at": "r2", "parent": {"gid": PARENT},
                "memberships": [{"project": {"gid": TEST_PROJECT}}],
                "custom_fields": [{
                    "gid": CORRELATION_FIELD, "enabled": True,
                    "resource_subtype": "text", "text_value": str(self.operation_id),
                }],
            }})
        if path.endswith("/tasks/search"):
            assert params[f"custom_fields.{CORRELATION_FIELD}.value"] == str(self.operation_id)
            assert params["projects.any"] == TEST_PROJECT
            return response("GET", path, {"data": self.search_rows, "next_page": None})
        raise AssertionError(path)


async def test_test_provider_create_and_recovery_bind_exact_correlation():
    operation_id = uuid4()
    client = Client(operation_id)
    provider = TestCreateAsanaProvider(client, TEST_PROJECT, CORRELATION_FIELD)

    assert provider.recovery_identity() == (
        "asana-custom-field-v1:1200569426771227:999001:999002"
    )
    assert provider.recovery_identity() != TestCreateAsanaProvider(
        Client(operation_id), TEST_PROJECT, "999003"
    ).recovery_identity()
    created = await provider.create_child(PARENT, "Created", "notes", operation_id)
    assert created == CREATED
    assert client.create_json == {"data": {
        "workspace": "1200569426771227", "name": "Created", "notes": "notes",
        "parent": PARENT, "projects": [TEST_PROJECT],
        "custom_fields": {CORRELATION_FIELD: str(operation_id)},
    }}
    assert await provider.recover_created(PARENT, operation_id) == CREATED
    assert client.create_calls == 1


async def test_committed_create_failed_readback_recovers_without_second_post():
    operation_id = uuid4()
    client = Client(operation_id)
    client.fail_created_readback_once = True
    provider = TestCreateAsanaProvider(client, TEST_PROJECT, CORRELATION_FIELD)

    with pytest.raises(UnknownEffect, match="readback unknown"):
        await provider.create_child(PARENT, "Created", "notes", operation_id)
    assert client.create_calls == 1
    assert await provider.recover_created(PARENT, operation_id) == CREATED
    assert client.create_calls == 1


async def test_test_provider_denies_production_parent_before_create():
    operation_id = uuid4()
    client = Client(operation_id)
    client.parent_project = PROJECT
    provider = TestCreateAsanaProvider(client, TEST_PROJECT, CORRELATION_FIELD)

    source = await provider.source_task(PARENT)
    assert source is not None and source.canonical is False
    with pytest.raises(ProviderError, match="create parent denied"):
        await provider.create_child(PARENT, "Created", "notes", operation_id)
    assert client.create_calls == 0


async def test_test_provider_rejects_ambiguous_or_production_correlation():
    with pytest.raises(ValueError, match="invalid test project"):
        TestCreateAsanaProvider(Client(uuid4()), PROJECT, CORRELATION_FIELD)

    operation_id = uuid4()
    client = Client(operation_id)
    client.search_rows = [{"gid": CREATED}, {"gid": "201"}]
    provider = TestCreateAsanaProvider(client, TEST_PROJECT, CORRELATION_FIELD)
    with pytest.raises(ProviderError, match="correlation conflict"):
        await provider.recover_created(PARENT, operation_id)
