import hashlib
import subprocess
from pathlib import Path

import httpx
from chatgpt_fixture import service
from mcp.types import ResourceLink

from switchstand import repository_bundle
from switchstand.chatgpt_mcp import build_ordinary_tools
from switchstand.repository_bundle import (
    BUNDLE_NAME,
    CHECKSUM_NAME,
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


def client_for(refs=REFS, bundle_digest=f"sha256:{CHECKSUM}"):
    manifest_url = "https://example.invalid/manifest"
    checksum_url = "https://example.invalid/checksum"
    bundle_url = "https://example.invalid/bundle"
    manifest = {
        "repository": "marcogallotta/switchstand",
        "refs": refs,
        "snapshot_digest": canonical_ref_map_digest(refs),
        "bundle_sha256": CHECKSUM,
    }
    release = {
        "assets": [
            {"name": BUNDLE_NAME, "browser_download_url": bundle_url, "digest": bundle_digest},
            {"name": MANIFEST_NAME, "browser_download_url": manifest_url},
            {"name": CHECKSUM_NAME, "browser_download_url": checksum_url},
        ]
    }

    def handle(request: httpx.Request) -> httpx.Response:
        if str(request.url) == RELEASE_API:
            return httpx.Response(200, json=release)
        if str(request.url) == manifest_url:
            return httpx.Response(200, json=manifest)
        if str(request.url) == checksum_url:
            return httpx.Response(200, text=f"{CHECKSUM}  {BUNDLE_NAME}\n")
        if str(request.url) == REFS_API:
            payload = [{"ref": ref, "object": {"sha": sha}} for ref, sha in REFS.items()]
            return httpx.Response(200, json=payload)
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
    assert non_head.status == "current"
    assert non_head.required_sha_is_head is None


async def test_resolver_rejects_stale_refs_and_asset_race():
    stale = {"refs/heads/main": "0" * 40, "refs/heads/review": "b" * 40}
    async with client_for(refs=stale) as client:
        result = await resolve_repository_bundle(client=client)
    assert (result.status, result.reason) == ("refresh_pending", "refs_advanced")

    async with client_for(bundle_digest="sha256:" + "d" * 64) as client:
        result = await resolve_repository_bundle(client=client)
    assert (result.status, result.reason) == ("refresh_pending", "bundle_transition")


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


def test_workflow_bundle_job_is_independent_and_branch_push_only():
    workflow = Path(".github/workflows/quality.yml").read_text()
    job = workflow.split("\n  repository-bundle:\n", 1)[1]
    assert "if: github.event_name == 'push' && startsWith(github.ref, 'refs/heads/')" in job
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
