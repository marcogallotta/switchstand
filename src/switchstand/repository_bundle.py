from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

REPOSITORY = "marcogallotta/switchstand"
RELEASE_TAG = "switchstand-repository-bundle-current"
BUNDLE_NAME = "switchstand-current.bundle"
MANIFEST_NAME = "switchstand-current.manifest.json"
CHECKSUM_NAME = "switchstand-current.bundle.sha256"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{RELEASE_TAG}"
REFS_API = f"https://api.github.com/repos/{REPOSITORY}/git/ref/heads/"
GITHUB_TOKEN_ENV = "SWITCHSTAND_REPOSITORY_BUNDLE_GITHUB_TOKEN"


class RepositoryBundleResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["current", "refresh_pending", "unavailable"]
    repository: str = REPOSITORY
    branch: str = "main"
    snapshot_digest: str | None = None
    bundle_sha256: str | None = None
    bundle_url: str | None = None
    refs: dict[str, str] = Field(default_factory=dict)
    required_sha: str | None = None
    required_sha_is_head: bool | None = None
    reason: str | None = None
    next_action: str | None = None
    actions_artifact: dict[str, str] | None = None


def canonical_ref_map_digest(refs: dict[str, str]) -> str:
    canonical = "".join(f"{name} {refs[name]}\n" for name in sorted(refs))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _asset(release: dict[str, Any], name: str) -> dict[str, Any] | None:
    found = [item for item in release.get("assets", []) if item.get("name") == name]
    return found[0] if len(found) == 1 else None


def _pending(required_sha: str | None, reason: str, branch: str) -> RepositoryBundleResolution:
    return RepositoryBundleResolution(
        status="refresh_pending", required_sha=required_sha, reason=reason, branch=branch,
        next_action=(
            f"No verified current single-branch artifact is available for {branch!r}. "
            "An authorized agent/operator can run the existing repository-bundle.yml "
            f"workflow_dispatch with branch={branch!r}, then retry after publication. "
            "This request does not start a build."
        ),
    )


def valid_branch(branch: str) -> bool:
    return bool(branch) and branch != "@" and not (
        branch.startswith(("-", "/")) or branch.endswith(("/", "."))
        or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
        or any(token in branch for token in ("..", "//", "@{", ","))
        or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", branch)
    )


def asset_names(branch: str) -> tuple[str, str, str]:
    stem = "switchstand-current" if branch == "main" else (
        "switchstand-branch-" + hashlib.sha256(branch.encode()).hexdigest()
    )
    return f"{stem}.bundle", f"{stem}.manifest.json", f"{stem}.bundle.sha256"


def _is_bundle_api_url(url: str, *, release: bool = False, branch: str = "main") -> bool:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "api.github.com" or parsed.fragment:
        return False
    if release:
        return parsed.path == urlsplit(RELEASE_API).path and not parsed.query
    return parsed.path == urlsplit(REFS_API + quote(branch, safe="/")).path and not parsed.query


async def _api_get(
    http: httpx.AsyncClient,
    url: str,
    token: str,
    *,
    release: bool = False,
    branch: str = "main",
) -> httpx.Response:
    if not _is_bundle_api_url(url, release=release, branch=branch):
        raise ValueError("foreign GitHub API URL")
    return await http.get(url, headers={"Authorization": f"Bearer {token}"}, follow_redirects=False)


async def resolve_repository_bundle(
    required_sha: str | None = None,
    client: httpx.AsyncClient | None = None,
    *,
    branch: str = "main",
) -> RepositoryBundleResolution:
    if not valid_branch(branch):
        return RepositoryBundleResolution(status="unavailable", reason="invalid_branch")
    ref = f"refs/heads/{branch}"
    bundle_name, manifest_name, checksum_name = asset_names(branch)
    token = os.getenv(GITHUB_TOKEN_ENV, "").strip()
    if not token:
        return RepositoryBundleResolution(
            status="unavailable", branch=branch, required_sha=required_sha, reason="github_unavailable"
        )
    owned = client is None
    http = client or httpx.AsyncClient(
        follow_redirects=False,
        timeout=10.0,
        trust_env=False,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        release_response = await _api_get(http, RELEASE_API, token, release=True)
        if release_response.status_code == 404:
            return _pending(required_sha, "cache_missing", branch)
        release_response.raise_for_status()
        release = release_response.json()
        bundle_asset = _asset(release, bundle_name)
        manifest_asset = _asset(release, manifest_name)
        checksum_asset = _asset(release, checksum_name)
        if bundle_asset is None or manifest_asset is None or checksum_asset is None:
            return _pending(required_sha, "cache_transition", branch)

        manifest_response = await http.get(
            str(manifest_asset["browser_download_url"]), follow_redirects=True
        )
        manifest_response.raise_for_status()
        manifest = manifest_response.json()
        checksum_response = await http.get(
            str(checksum_asset["browser_download_url"]), follow_redirects=True
        )
        checksum_response.raise_for_status()
        checksum = checksum_response.text.strip().split()[0]

        # Exact ref lookup: no enumeration or all-branch fallback.
        ref_response = await _api_get(http, REFS_API + quote(branch, safe="/"), token, branch=branch)
        if ref_response.status_code == 404:
            return _pending(required_sha, "branch_missing", branch)
        ref_response.raise_for_status()
        authoritative = ref_response.json()
        authoritative_sha = str(authoritative["object"]["sha"])
        manifest_refs = {str(k): str(v) for k, v in manifest.get("refs", {}).items()}
        if set(manifest_refs) != {ref}:
            return _pending(required_sha, "branch_scope_mismatch", branch)
        snapshot_digest = canonical_ref_map_digest(manifest_refs)
        expected_checksum = str(manifest.get("bundle_sha256", ""))
        asset_digest = str(bundle_asset.get("digest") or "")
        if manifest.get("repository") != REPOSITORY:
            return RepositoryBundleResolution(
                status="unavailable", required_sha=required_sha, reason="repository_mismatch"
            )
        if not expected_checksum or checksum != expected_checksum:
            return _pending(required_sha, "checksum_transition", branch)
        if asset_digest != f"sha256:{expected_checksum}":
            return _pending(required_sha, "bundle_transition", branch)
        if manifest.get("snapshot_digest") != snapshot_digest:
            return _pending(required_sha, "manifest_digest_mismatch", branch)
        if (
            authoritative.get("ref") != ref or manifest_refs[ref] != authoritative_sha
            or (required_sha is not None and required_sha != authoritative_sha)
        ):
            reason = "required_sha_unproved" if required_sha else "refs_advanced"
            return _pending(required_sha, reason, branch)
        provenance = manifest.get("provenance", {})
        run_id, attempt = str(provenance.get("run_id", "")), str(provenance.get("run_attempt", ""))
        artifact_name = f"{bundle_name.removesuffix('.bundle')}-{authoritative_sha}-run-{run_id}-{attempt}"
        mirror = None
        if (
            re.fullmatch(r"[1-9][0-9]*", run_id)
            and re.fullmatch(r"[1-9][0-9]*", attempt)
            and provenance.get("artifact_name") == artifact_name
        ):
            mirror = {"name": artifact_name, "run_id": run_id,
                      "run_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"}
        return RepositoryBundleResolution(
            status="current",
            branch=branch,
            snapshot_digest=snapshot_digest,
            bundle_sha256=expected_checksum,
            bundle_url=str(bundle_asset["browser_download_url"]),
            refs=manifest_refs,
            required_sha=required_sha,
            required_sha_is_head=True if required_sha else None,
            actions_artifact=mirror,
        )
    except httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError:
        return RepositoryBundleResolution(
            status="unavailable", branch=branch, required_sha=required_sha, reason="github_unavailable"
        )
    finally:
        if owned:
            await http.aclose()
