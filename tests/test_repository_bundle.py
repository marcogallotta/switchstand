import hashlib
import subprocess
from pathlib import Path

import httpx
import pytest
from chatgpt_fixture import service
from mcp.types import ResourceLink

from switchstand import repository_bundle
from switchstand.chatgpt_mcp import build_ordinary_tools
from switchstand.repository_bundle import (
    BUNDLE_NAME,
    CHECKSUM_NAME,
    GITHUB_TOKEN_ENV,
    MANIFEST_NAME,
    REFS_API,
    RELEASE_API,
    RepositoryBundleResolution,
    canonical_ref_map_digest,
    resolve_repository_bundle,
)

REFS = {
    "refs/heads/main": "a" * 40,
    "refs/heads/review": "b" * 40,
}
CHECKSUM = "c" * 64
TOKEN = "repository-bundle-token"


@pytest.fixture(autouse=True)
def repository_bundle_token(monkeypatch):
    monkeypatch.setenv(GITHUB_TOKEN_ENV, TOKEN)


def client_for(
    refs=REFS,
    bundle_digest=f"sha256:{CHECKSUM}",
    manifest_digest=None,
    manifest_checksum=CHECKSUM,
    downloaded_checksum=CHECKSUM,
    authoritative_pages=None,
    foreign_next_url=None,
    redirect_manifest=False,
    seen=None,
):
    manifest_url = "https://example.invalid/manifest"
    redirected_manifest_url = "https://objects.example.invalid/manifest"
    checksum_url = "https://example.invalid/checksum"
    bundle_url = "https://example.invalid/bundle"
    manifest = {
        "repository": "marcogallotta/switchstand",
        "refs": refs,
        "snapshot_digest": manifest_digest or canonical_ref_map_digest(refs),
        "bundle_sha256": manifest_checksum,
    }
    release = {
        "assets": [
            {"name": BUNDLE_NAME, "browser_download_url": bundle_url, "digest": bundle_digest},
            {"name": MANIFEST_NAME, "browser_download_url": manifest_url},
            {"name": CHECKSUM_NAME, "browser_download_url": checksum_url},
        ]
    }
    pages = authoritative_pages or [REFS]
    refs_urls = [REFS_API, *(f"{REFS_API}?page={page}" for page in range(2, len(pages) + 1))]

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if str(request.url) == RELEASE_API:
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
            return httpx.Response(200, json=release)
        if str(request.url) == manifest_url:
            assert "Authorization" not in request.headers
            if redirect_manifest:
                return httpx.Response(302, headers={"Location": redirected_manifest_url})
            return httpx.Response(200, json=manifest)
        if str(request.url) == redirected_manifest_url:
            assert "Authorization" not in request.headers
            return httpx.Response(200, json=manifest)
        if str(request.url) == checksum_url:
            assert "Authorization" not in request.headers
            return httpx.Response(200, text=f"{downloaded_checksum}  {BUNDLE_NAME}\n")
        if str(request.url) in refs_urls:
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
            page = refs_urls.index(str(request.url))
            payload = [{"ref": ref, "object": {"sha": sha}} for ref, sha in pages[page].items()]
            next_url = refs_urls[page + 1] if page + 1 < len(refs_urls) else foreign_next_url
            headers = {"Link": f'<{next_url}>; rel="next"'} if next_url else None
            return httpx.Response(200, json=payload, headers=headers)
        raise AssertionError(str(request.url))

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def test_canonical_digest_is_order_independent():
    reversed_refs = dict(reversed(list(REFS.items())))
    assert canonical_ref_map_digest(REFS) == canonical_ref_map_digest(reversed_refs)
    expected = hashlib.sha256(
        "".join(f"{key} {REFS[key]}\n" for key in sorted(REFS)).encode()
    ).hexdigest()
    assert canonical_ref_map_digest(REFS) == expected


async def test_resolver_returns_current_only_on_exact_ref_and_asset_match():
    async with client_for() as client:
        result = await resolve_repository_bundle("b" * 40, client)
    assert result.status == "current"
    assert result.required_sha_is_head is True
    assert result.refs == REFS
    assert result.bundle_sha256 == CHECKSUM

    async with client_for() as client:
        non_head = await resolve_repository_bundle("d" * 40, client)
    assert (non_head.status, non_head.reason) == ("refresh_pending", "required_sha_unproved")
    assert non_head.required_sha_is_head is None


async def test_resolver_rejects_stale_refs_and_asset_race():
    stale = {"refs/heads/main": "0" * 40, "refs/heads/review": "b" * 40}
    async with client_for(refs=stale) as client:
        result = await resolve_repository_bundle(client=client)
    assert (result.status, result.reason) == ("refresh_pending", "refs_advanced")

    async with client_for(bundle_digest="sha256:" + "d" * 64) as client:
        result = await resolve_repository_bundle(client=client)
    assert (result.status, result.reason) == ("refresh_pending", "bundle_transition")


async def test_resolver_rejects_manifest_and_checksum_transitions():
    async with client_for(manifest_digest="d" * 64) as client:
        result = await resolve_repository_bundle(client=client)
    assert (result.status, result.reason) == (
        "refresh_pending",
        "manifest_digest_mismatch",
    )

    async with client_for(downloaded_checksum="d" * 64) as client:
        result = await resolve_repository_bundle(client=client)
    assert (result.status, result.reason) == ("refresh_pending", "checksum_transition")


async def test_resolver_accepts_unrelated_branch_drift_for_current_main():
    authoritative = {
        "refs/heads/main": REFS["refs/heads/main"],
        "refs/heads/review": "d" * 40,
        "refs/heads/new-review": "e" * 40,
    }
    async with client_for(authoritative_pages=[authoritative]) as client:
        result = await resolve_repository_bundle(client=client)
    assert result.status == "current"
    assert result.refs == REFS


async def test_resolver_requires_requested_sha_on_an_unchanged_authoritative_ref():
    authoritative = {
        "refs/heads/main": "d" * 40,
        "refs/heads/review": REFS["refs/heads/review"],
    }
    async with client_for(authoritative_pages=[authoritative]) as client:
        result = await resolve_repository_bundle(REFS["refs/heads/review"], client)
    assert result.status == "current"
    assert result.required_sha_is_head is True

    authoritative["refs/heads/review"] = "e" * 40
    async with client_for(authoritative_pages=[authoritative]) as client:
        result = await resolve_repository_bundle(REFS["refs/heads/review"], client)
    assert (result.status, result.reason) == ("refresh_pending", "required_sha_unproved")


async def test_resolver_consumes_every_ref_page_before_accepting_current():
    pages = [
        {"refs/heads/main": REFS["refs/heads/main"]},
        {"refs/heads/review": REFS["refs/heads/review"]},
    ]
    async with client_for(authoritative_pages=pages) as client:
        result = await resolve_repository_bundle(client=client)
    assert result.status == "current"
    assert result.refs == REFS


async def test_resolver_accepts_unrelated_drift_on_later_ref_page():
    pages = [
        {"refs/heads/main": REFS["refs/heads/main"]},
        {"refs/heads/review": "d" * 40},
    ]
    async with client_for(authoritative_pages=pages) as client:
        result = await resolve_repository_bundle(client=client)
    assert result.status == "current"


@pytest.mark.parametrize("token", [None, " \t"])
async def test_resolver_fails_before_network_without_dedicated_token(monkeypatch, token):
    if token is None:
        monkeypatch.delenv(GITHUB_TOKEN_ENV)
    else:
        monkeypatch.setenv(GITHUB_TOKEN_ENV, token)
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await resolve_repository_bundle(client=client)
    assert (result.status, result.reason) == ("unavailable", "github_unavailable")
    assert requests == []


async def test_resolver_rejects_foreign_ref_pagination_without_leaking_token():
    requests: list[httpx.Request] = []
    foreign = "https://example.invalid/refs?page=2"
    async with client_for(foreign_next_url=foreign, seen=requests) as client:
        result = await resolve_repository_bundle(client=client)
    assert (result.status, result.reason) == ("unavailable", "github_unavailable")
    assert all(str(request.url) != foreign for request in requests)


async def test_resolver_does_not_follow_authenticated_api_redirect():
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://example.invalid/token-leak"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await resolve_repository_bundle(client=client)
    assert (result.status, result.reason) == ("unavailable", "github_unavailable")
    assert [str(request.url) for request in requests] == [RELEASE_API]


async def test_asset_redirects_remain_unauthenticated():
    requests: list[httpx.Request] = []
    async with client_for(redirect_manifest=True, seen=requests) as client:
        result = await resolve_repository_bundle(client=client)
    assert result.status == "current"
    assert all(
        "Authorization" not in request.headers
        for request in requests
        if request.url.host != "api.github.com"
    )


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ("git", *args), cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_git_bundle_materializes_main_and_multiple_heads(tmp_path: Path):
    source = tmp_path / "source"
    _git("init", str(source))
    _git("-C", str(source), "config", "user.email", "test@example.com")
    _git("-C", str(source), "config", "user.name", "Test")
    (source / "file").write_text("main\n")
    _git("-C", str(source), "add", "file")
    _git("-C", str(source), "commit", "-m", "main")
    _git("-C", str(source), "branch", "-M", "main")
    _git("-C", str(source), "checkout", "-b", "review")
    (source / "file").write_text("review\n")
    _git("-C", str(source), "commit", "-am", "review")
    review_sha = _git("-C", str(source), "rev-parse", "HEAD")

    authority = tmp_path / "authority.git"
    snapshot = tmp_path / "snapshot.git"
    bundle = tmp_path / "current.bundle"
    materialized = tmp_path / "materialized"
    _git("clone", "--bare", str(source), str(authority))
    _git("init", "--bare", str(snapshot))
    _git("-C", str(snapshot), "remote", "add", "origin", str(authority))
    _git("-C", str(snapshot), "fetch", "origin", "+refs/heads/*:refs/heads/*")
    _git("-C", str(snapshot), "bundle", "create", str(bundle), "--branches")
    verify = tmp_path / "verify.git"
    _git("init", "--bare", str(verify))
    _git("-C", str(verify), "bundle", "verify", str(bundle))
    _git("clone", "--branch", "main", str(bundle), str(materialized))

    assert _git("-C", str(materialized), "rev-parse", "refs/remotes/origin/review") == review_sha


def test_workflow_bundle_job_uses_reviewed_default_branch_boundary():
    quality = Path(".github/workflows/quality.yml").read_text()
    workflow = Path(".github/workflows/repository-bundle.yml").read_text()
    assert "repository-bundle:" not in quality
    assert "workflow_run:" in workflow
    assert "workflows: [Quality]" in workflow
    assert "branches:" not in workflow
    job = workflow.split("\n  repository-bundle:\n", 1)[1]
    assert "vars.SWITCHSTAND_REPOSITORY_BUNDLE_PUBLISH_ENABLED == 'true'" in job
    assert "github.event.workflow_run.event == 'push'" in job
    assert "workflow_run.conclusion" not in job
    assert "\n    needs:" not in job
    assert "cancel-in-progress: false" in job
    assert "git -C repository.git fetch --prune origin '+refs/heads/*:refs/heads/*'" in job


async def test_mcp_returns_resource_link_and_typed_refresh(monkeypatch):
    async def current(required_sha=None):
        return RepositoryBundleResolution(
            status="current",
            bundle_url="https://example.invalid/bundle",
            bundle_sha256=CHECKSUM,
            snapshot_digest="e" * 64,
            refs=REFS,
            required_sha=required_sha,
            required_sha_is_head=True,
        )

    monkeypatch.setattr(repository_bundle, "resolve_repository_bundle", current)
    tool = dict(build_ordinary_tools(service()))["repository_bundle_get"]
    result = await tool("1", "b" * 40)
    assert result.structured_content["status"] == "current"
    links = [item for item in result.content if isinstance(item, ResourceLink)]
    assert len(links) == 1
    assert str(links[0].uri) == "https://example.invalid/bundle"

    async def pending(required_sha=None):
        return RepositoryBundleResolution(
            status="refresh_pending", required_sha=required_sha, reason="refs_advanced"
        )

    monkeypatch.setattr(repository_bundle, "resolve_repository_bundle", pending)
    result = await tool("1", "b" * 40)
    assert result.structured_content["status"] == "refresh_pending"
    assert not any(isinstance(item, ResourceLink) for item in result.content)
