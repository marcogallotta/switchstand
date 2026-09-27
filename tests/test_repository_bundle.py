from pathlib import Path
import subprocess

import httpx
import pytest
from mcp.types import ResourceLink

import switchstand.chatgpt_mcp as chatgpt_mcp
from chatgpt_fixture import service
from switchstand.chatgpt_mcp import build_ordinary_tools
from switchstand.repository_bundle import (
    RepositoryBundleState,
    ref_snapshot_digest,
    resolve_repository_bundle,
)

MAIN = "1" * 40
FEATURE = "2" * 40
BUNDLE_SHA = "a" * 64
REFS = {"refs/heads/main": MAIN, "refs/heads/feature": FEATURE}


def _manifest(refs=REFS):
    return {
        "schema_version": "1",
        "repository": "marcogallotta/switchstand",
        "refs": refs,
        "snapshot_sha256": ref_snapshot_digest(refs),
        "bundle_sha256": BUNDLE_SHA,
        "event_sha": FEATURE,
        "run_id": "123",
        "run_attempt": "1",
    }


def _transport(current_refs=REFS, *, bundle_digest=f"sha256:{BUNDLE_SHA}"):
    manifest = _manifest()

    def handler(request):
        path = request.url.path
        if path.endswith("/releases/tags/repository-bundle-current"):
            return httpx.Response(200, request=request, json={"assets": [
                {
                    "name": "switchstand-current.bundle",
                    "browser_download_url": "https://cache.example/bundle",
                    "digest": bundle_digest,
                    "size": 123,
                },
                {
                    "name": "switchstand-current.json",
                    "browser_download_url": "https://cache.example/manifest",
                    "digest": "sha256:" + "b" * 64,
                    "size": 456,
                },
            ]})
        if request.url.host == "cache.example" and path == "/manifest":
            return httpx.Response(200, request=request, json=manifest)
        if path.endswith("/branches"):
            branches = [
                {"name": ref.removeprefix("refs/heads/"), "commit": {"sha": sha}}
                for ref, sha in current_refs.items()
            ]
            return httpx.Response(200, request=request, json=branches)
        raise AssertionError(f"unexpected request: {request.url}")

    return httpx.MockTransport(handler)


async def test_repository_bundle_resolver_is_current_only_on_exact_ref_and_digest_match():
    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=_transport()
    ) as client:
        result = await resolve_repository_bundle(FEATURE, client=client)

    assert result.status == "current"
    assert result.required_sha == FEATURE
    assert result.snapshot_sha256 == ref_snapshot_digest(REFS)
    assert result.bundle_sha256 == BUNDLE_SHA
    assert result.bundle_url == "https://cache.example/bundle"
    assert result.ref_count == 2


@pytest.mark.parametrize(
    ("current_refs", "bundle_digest", "reason"),
    [
        (
            REFS | {"refs/heads/feature": "3" * 40},
            f"sha256:{BUNDLE_SHA}",
            "authoritative_refs_advanced",
        ),
        (REFS, "sha256:" + "c" * 64, "bundle_manifest_digest_mismatch"),
    ],
)
async def test_repository_bundle_resolver_fails_closed_on_stale_or_replacement_race(
    current_refs, bundle_digest, reason
):
    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=_transport(current_refs, bundle_digest=bundle_digest),
    ) as client:
        result = await resolve_repository_bundle(client=client)

    assert result.status == "refresh_pending"
    assert result.reason == reason
    assert result.bundle_url is None


async def test_repository_bundle_tool_returns_resource_link_and_does_not_claim_containment(
    monkeypatch,
):
    async def current(required_sha):
        return RepositoryBundleState(
            status="current",
            required_sha=required_sha,
            snapshot_sha256=ref_snapshot_digest(REFS),
            bundle_sha256=BUNDLE_SHA,
            bundle_url="https://cache.example/bundle",
            bundle_size=123,
            ref_count=2,
        )

    monkeypatch.setattr(chatgpt_mcp, "resolve_repository_bundle", current)
    tool = dict(build_ordinary_tools(service()))["repository_bundle_get"]
    result = await tool("1", FEATURE)

    links = [item for item in result.content if isinstance(item, ResourceLink)]
    assert len(links) == 1 and str(links[0].uri) == "https://cache.example/bundle"
    assert result.structured_content["required_sha"] == FEATURE
    assert "local checkout target" in result.content[0].text
    assert "contains" not in result.content[0].text.lower()


def _git(*args, cwd=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def test_one_bundle_materializes_multiple_branch_heads_from_empty_repo(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    snapshot = tmp_path / "snapshot.git"
    materialized = tmp_path / "materialized.git"
    bundle = tmp_path / "current.bundle"

    _git("init", "--bare", str(origin))
    _git("init", str(work))
    _git("config", "user.email", "test@example.com", cwd=work)
    _git("config", "user.name", "Test", cwd=work)
    (work / "file.txt").write_text("main")
    _git("add", "file.txt", cwd=work)
    _git("commit", "-m", "main", cwd=work)
    _git("branch", "-M", "main", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)
    _git("push", "origin", "main", cwd=work)
    _git("switch", "-c", "feature", cwd=work)
    (work / "file.txt").write_text("feature")
    _git("commit", "-am", "feature", cwd=work)
    feature_sha = _git("rev-parse", "HEAD", cwd=work)
    _git("push", "origin", "feature", cwd=work)

    _git("init", "--bare", str(snapshot))
    _git("-C", str(snapshot), "remote", "add", "origin", str(origin))
    _git("-C", str(snapshot), "fetch", "--prune", "origin",
         "+refs/heads/*:refs/heads/*")
    _git("-C", str(snapshot), "bundle", "create", str(bundle), "--branches")
    _git("-C", str(snapshot), "bundle", "verify", str(bundle))
    heads = _git("-C", str(snapshot), "bundle", "list-heads", str(bundle))
    assert "refs/heads/main" in heads and "refs/heads/feature" in heads

    _git("init", "--bare", str(materialized))
    _git("-C", str(materialized), "fetch", str(bundle),
         "+refs/heads/*:refs/heads/*")
    assert _git("-C", str(materialized), "rev-parse", "refs/heads/feature") == feature_sha
    _git("-C", str(materialized), "cat-file", "-e", f"{feature_sha}^{{commit}}")


def test_quality_workflow_keeps_bundle_publication_independent_and_branch_only():
    workflow = Path(".github/workflows/quality.yml").read_text()
    section = workflow.split("\n  repository-bundle:\n", 1)[1]
    assert "github.event_name == 'push'" in section
    assert "startsWith(github.ref, 'refs/heads/')" in section
    assert "needs:" not in section
    assert "git -C repository.git fetch --prune origin '+refs/heads/*:refs/heads/*'" in section
    assert "gh release upload" in section
