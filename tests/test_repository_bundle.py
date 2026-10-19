import hashlib
import json
import os
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
    asset_names,
    canonical_ref_map_digest,
    resolve_repository_bundle,
    valid_branch,
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
    refs=None,
    branch="main",
    bundle_digest=f"sha256:{CHECKSUM}",
    manifest_digest=None,
    manifest_checksum=CHECKSUM,
    downloaded_checksum=CHECKSUM,
    authoritative_pages=None,
    redirect_manifest=False,
    provenance=None,
    seen=None,
):
    refs = refs if refs is not None else {f"refs/heads/{branch}": REFS[f"refs/heads/{branch}"]}
    bundle_name, manifest_name, checksum_name = asset_names(branch)
    manifest_url = "https://example.invalid/manifest"
    redirected_manifest_url = "https://objects.example.invalid/manifest"
    checksum_url = "https://example.invalid/checksum"
    bundle_url = "https://example.invalid/bundle"
    manifest = {
        "repository": "marcogallotta/switchstand",
        "refs": refs,
        "snapshot_digest": manifest_digest or canonical_ref_map_digest(refs),
        "bundle_sha256": manifest_checksum,
        "provenance": provenance or {},
    }
    release = {
        "assets": [
            {"name": bundle_name, "browser_download_url": bundle_url, "digest": bundle_digest},
            {"name": manifest_name, "browser_download_url": manifest_url},
            {"name": checksum_name, "browser_download_url": checksum_url},
        ]
    }
    pages = authoritative_pages or [REFS]
    authoritative = dict(item for page in pages for item in page.items())

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
        if str(request.url) == REFS_API + branch:
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
            ref = f"refs/heads/{branch}"
            if ref not in authoritative:
                return httpx.Response(404)
            return httpx.Response(200, json={"ref": ref, "object": {"sha": authoritative[ref]}})
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
    async with client_for(branch="review") as client:
        result = await resolve_repository_bundle("b" * 40, client, branch="review")
    assert result.status == "current"
    assert result.required_sha_is_head is True
    assert result.refs == {f"refs/heads/{result.branch}": REFS[f"refs/heads/{result.branch}"]}
    assert result.bundle_sha256 == CHECKSUM

    async with client_for() as client:
        non_head = await resolve_repository_bundle("d" * 40, client)
    assert (non_head.status, non_head.reason) == ("refresh_pending", "required_sha_unproved")
    assert non_head.required_sha_is_head is None


async def test_resolver_rejects_stale_refs_and_asset_race():
    stale = {"refs/heads/main": "0" * 40}
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
    assert result.refs == {f"refs/heads/{result.branch}": REFS[f"refs/heads/{result.branch}"]}


async def test_resolver_requires_requested_sha_on_an_unchanged_authoritative_ref():
    authoritative = {
        "refs/heads/main": "d" * 40,
        "refs/heads/review": REFS["refs/heads/review"],
    }
    async with client_for(branch="review", authoritative_pages=[authoritative]) as client:
        result = await resolve_repository_bundle(REFS["refs/heads/review"], client, branch="review")
    assert result.status == "current"
    assert result.required_sha_is_head is True

    authoritative["refs/heads/review"] = "e" * 40
    async with client_for(branch="review", authoritative_pages=[authoritative]) as client:
        result = await resolve_repository_bundle(REFS["refs/heads/review"], client, branch="review")
    assert (result.status, result.reason) == ("refresh_pending", "required_sha_unproved")


async def test_resolver_checks_only_selected_ref_and_rejects_legacy_all_heads():
    requests = []
    async with client_for(seen=requests) as client:
        result = await resolve_repository_bundle(client=client)
    assert result.status == "current"
    assert [str(r.url) for r in requests if "/git/" in str(r.url)] == [REFS_API + "main"]
    async with client_for(refs=REFS) as client:
        result = await resolve_repository_bundle(client=client)
    assert (result.status, result.reason) == ("refresh_pending", "branch_scope_mismatch")
    assert "workflow_dispatch" in result.next_action
    assert "branch='main'" in result.next_action
    assert "does not start a build" in result.next_action


@pytest.mark.parametrize("branch", ["*", "main,review", "main review", "--all", "a..b", "a:b", "a.lock", "a/", ".a", "a\nb", "a@{b", "a\\b"])
async def test_invalid_branch_never_reaches_network(branch):
    assert not valid_branch(branch)
    async with client_for() as client:
        result = await resolve_repository_bundle(client=client, branch=branch)
    assert (result.status, result.reason) == ("unavailable", "invalid_branch")


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


@pytest.mark.parametrize("marker", ["valid", "absent", "foreign", "invalid_run"])
async def test_artifact_locator_requires_matching_publisher_marker(marker):
    name = f"switchstand-current-{REFS['refs/heads/main']}-run-123-2"
    provenance = {"run_id": "123", "run_attempt": "2", "artifact_name": name}
    if marker == "absent":
        provenance.pop("artifact_name")
    elif marker == "foreign":
        provenance["artifact_name"] = "https://foreign.invalid/asset"
    elif marker == "invalid_run":
        provenance["run_id"] = "../../foreign"
    result = await resolve_repository_bundle(client=client_for(provenance=provenance))
    assert result.status == "current"
    assert result.actions_artifact == ({
        "name": name, "run_id": "123",
        "run_url": "https://github.com/marcogallotta/switchstand/actions/runs/123",
    } if marker == "valid" else None)


@pytest.mark.parametrize("branch", ["main", "review"])
def test_workflow_exports_one_branch_with_original_history(tmp_path: Path, branch):
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

    main_sha = _git("-C", str(source), "rev-parse", "main")
    _git("-C", str(source), "checkout", "--orphan", "unrelated")
    (source / "file").write_bytes(os.urandom(65536))
    _git("-C", str(source), "add", "file")
    _git("-C", str(source), "commit", "-m", "unrelated history")
    unrelated_sha = _git("-C", str(source), "rev-parse", "HEAD")
    authority = tmp_path / "authority.git"
    _git("clone", "--bare", str(source), str(authority))
    # Execute the actual publisher script with a process-local Git URL rewrite.
    workflow = Path(".github/workflows/repository-bundle.yml").read_text()
    block = workflow.split("        run: |\n", 1)[1].split("      - name:", 1)[0]
    script = "\n".join(line[10:] for line in block.splitlines())
    env = dict(os.environ, BRANCH=branch, REPOSITORY="test/repo", EVENT_SHA=review_sha,
               RUN_ID="1", RUN_ATTEMPT="1", GITHUB_ENV=str(tmp_path / "github.env"),
               BUNDLE_NAME=BUNDLE_NAME, MANIFEST_NAME=MANIFEST_NAME, CHECKSUM_NAME=CHECKSUM_NAME,
               GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"url.{authority}.insteadOf",
               GIT_CONFIG_VALUE_0="https://github.com/test/repo.git")
    subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, check=True, capture_output=True)
    bundle = tmp_path / asset_names(branch)[0]
    manifest = json.loads((tmp_path / asset_names(branch)[1]).read_text())
    expected_sha = main_sha if branch == "main" else review_sha
    artifact_name = f"{bundle.stem}-{expected_sha}-run-1-1"
    assert manifest["provenance"]["artifact_name"] == artifact_name
    assert f"ARTIFACT_NAME={artifact_name}\n" in (tmp_path / "github.env").read_text()
    upload = workflow.split("      - name: Upload repository bundle ZIP mirror", 1)[1].split("      - name:", 1)[0]
    assert "actions/upload-artifact@v7" in upload
    assert all(f"${{{{ env.{key} }}}}" in upload for key in ("BUNDLE_NAME", "MANIFEST_NAME", "CHECKSUM_NAME"))
    assert "if-no-files-found: error" in upload
    assert _git("bundle", "list-heads", str(bundle)) == f"{expected_sha} refs/heads/{branch}"
    materialized = tmp_path / "materialized"
    assert _git("-C", str(materialized), "rev-parse", "HEAD") == expected_sha
    assert _git("-C", str(materialized), "rev-parse", f"{main_sha}^{{commit}}") == main_sha
    assert _git("-C", str(materialized), "rev-list", "--count", "HEAD") == ("1" if branch == "main" else "2")
    absent = subprocess.run(["git", "-C", str(materialized), "cat-file", "-e", unrelated_sha], capture_output=True, check=False)
    assert absent.returncode != 0


def test_workflow_bundle_job_uses_reviewed_default_branch_boundary():
    quality = Path(".github/workflows/quality.yml").read_text()
    workflow = Path(".github/workflows/repository-bundle.yml").read_text()
    assert "repository-bundle:" not in quality
    assert "workflow_run:" in workflow
    assert "workflows: [Quality]" in workflow
    assert "workflow_dispatch:" in workflow
    assert "default: main" in workflow
    assert "inputs.branch" in workflow
    assert "branches:" not in workflow
    job = workflow.split("\n  repository-bundle:\n", 1)[1]
    assert "vars.SWITCHSTAND_REPOSITORY_BUNDLE_PUBLISH_ENABLED == 'true'" in job
    assert "github.event.workflow_run.event == 'push'" in job
    assert "workflow_run.conclusion" not in job
    assert "\n    needs:" not in job
    assert "cancel-in-progress: false" in job
    assert 'git -C repository.git fetch --no-tags origin "+$ref:$ref"' in job
    assert "--branches" not in job
    assert "refs/heads/*" not in job


async def test_mcp_returns_resource_link_and_typed_refresh(monkeypatch):
    async def current(required_sha=None, *, branch="main"):
        return RepositoryBundleResolution(
            status="current",
            bundle_url="https://example.invalid/bundle",
            bundle_sha256=CHECKSUM,
            snapshot_digest="e" * 64,
            refs={"refs/heads/main": REFS["refs/heads/main"]},
            branch=branch,
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

    async def pending(required_sha=None, *, branch="main"):
        return RepositoryBundleResolution(
            status="refresh_pending", required_sha=required_sha, reason="refs_advanced"
        )

    monkeypatch.setattr(repository_bundle, "resolve_repository_bundle", pending)
    result = await tool("1", "b" * 40)
    assert result.structured_content["status"] == "refresh_pending"
    assert not any(isinstance(item, ResourceLink) for item in result.content)
