from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

REPOSITORY = "marcogallotta/switchstand"
RELEASE_TAG = "switchstand-repository-bundle-current"
BUNDLE_NAME = "switchstand-current.bundle"
MANIFEST_NAME = "switchstand-current.manifest.json"
CHECKSUM_NAME = "switchstand-current.bundle.sha256"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{RELEASE_TAG}"
REFS_API = f"https://api.github.com/repos/{REPOSITORY}/git/matching-refs/heads/"
GITHUB_TOKEN_ENV = "SWITCHSTAND_REPOSITORY_BUNDLE_GITHUB_TOKEN"


class RepositoryBundleResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["current", "refresh_pending", "unavailable"]
    repository: str = REPOSITORY
    snapshot_digest: str | None = None
    bundle_sha256: str | None = None
    bundle_url: str | None = None
    refs: dict[str, str] = Field(default_factory=dict)
    required_sha: str | None = None
    required_sha_is_head: bool | None = None
    reason: str | None = None


def canonical_ref_map_digest(refs: dict[str, str]) -> str:
    canonical = "".join(f"{name} {refs[name]}\n" for name in sorted(refs))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _asset(release: dict[str, Any], name: str) -> dict[str, Any] | None:
    found = [item for item in release.get("assets", []) if item.get("name") == name]
    return found[0] if len(found) == 1 else None


def _pending(required_sha: str | None, reason: str) -> RepositoryBundleResolution:
    return RepositoryBundleResolution(
        status="refresh_pending", required_sha=required_sha, reason=reason
    )


def _grounding_is_current(
    manifest_refs: dict[str, str],
    authoritative_refs: dict[str, str],
    required_sha: str | None,
) -> bool:
    if required_sha is None:
        manifest_main = manifest_refs.get("refs/heads/main")
        return (
            manifest_main is not None
            and authoritative_refs.get("refs/heads/main") == manifest_main
        )
    return any(
        sha == required_sha and authoritative_refs.get(ref) == required_sha
        for ref, sha in manifest_refs.items()
    )


def _is_bundle_api_url(url: str, *, release: bool = False) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "api.github.com" or parsed.fragment:
        return False
    if release:
        return parsed.path == urlsplit(RELEASE_API).path and not parsed.query
    return parsed.path == urlsplit(REFS_API).path


async def _api_get(
    http: httpx.AsyncClient,
    url: str,
    token: str,
    *,
    release: bool = False,
) -> httpx.Response:
    if not _is_bundle_api_url(url, release=release):
        raise ValueError("foreign GitHub API URL")
    return await http.get(url, headers={"Authorization": f"Bearer {token}"}, follow_redirects=False)


async def _authoritative_refs(http: httpx.AsyncClient, token: str) -> dict[str, str]:
    refs: dict[str, str] = {}
    next_url: str | None = REFS_API
    seen_urls: set[str] = set()
    while next_url is not None:
        if next_url in seen_urls:
            raise ValueError("cyclic refs pagination")
        seen_urls.add(next_url)
        response = await _api_get(http, next_url, token)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise TypeError("refs response must be a list")
        for item in cast(list[dict[str, Any]], payload):
            ref = str(item["ref"])
            if not ref.startswith("refs/heads/"):
                continue
            if ref in refs:
                raise ValueError("duplicate ref across pages")
            refs[ref] = str(item["object"]["sha"])
        next_link = response.links.get("next")
        next_url = str(next_link["url"]) if next_link is not None else None
    return refs


async def resolve_repository_bundle(
    required_sha: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> RepositoryBundleResolution:
    token = os.getenv(GITHUB_TOKEN_ENV, "").strip()
    if not token:
        return RepositoryBundleResolution(
            status="unavailable", required_sha=required_sha, reason="github_unavailable"
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
            return _pending(required_sha, "cache_missing")
        release_response.raise_for_status()
        release = release_response.json()
        bundle_asset = _asset(release, BUNDLE_NAME)
        manifest_asset = _asset(release, MANIFEST_NAME)
        checksum_asset = _asset(release, CHECKSUM_NAME)
        if bundle_asset is None or manifest_asset is None or checksum_asset is None:
            return _pending(required_sha, "cache_transition")

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

        authoritative = await _authoritative_refs(http, token)
        manifest_refs = {str(k): str(v) for k, v in manifest.get("refs", {}).items()}
        snapshot_digest = canonical_ref_map_digest(manifest_refs)
        expected_checksum = str(manifest.get("bundle_sha256", ""))
        asset_digest = str(bundle_asset.get("digest") or "")
        if manifest.get("repository") != REPOSITORY:
            return RepositoryBundleResolution(
                status="unavailable", required_sha=required_sha, reason="repository_mismatch"
            )
        if not expected_checksum or checksum != expected_checksum:
            return _pending(required_sha, "checksum_transition")
        if asset_digest != f"sha256:{expected_checksum}":
            return _pending(required_sha, "bundle_transition")
        if manifest.get("snapshot_digest") != snapshot_digest:
            return _pending(required_sha, "manifest_digest_mismatch")
        if not _grounding_is_current(manifest_refs, authoritative, required_sha):
            reason = "required_sha_unproved" if required_sha else "refs_advanced"
            return _pending(required_sha, reason)
        return RepositoryBundleResolution(
            status="current",
            snapshot_digest=snapshot_digest,
            bundle_sha256=expected_checksum,
            bundle_url=str(bundle_asset["browser_download_url"]),
            refs=manifest_refs,
            required_sha=required_sha,
            required_sha_is_head=True if required_sha else None,
        )
    except httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError:
        return RepositoryBundleResolution(
            status="unavailable", required_sha=required_sha, reason="github_unavailable"
        )
    finally:
        if owned:
            await http.aclose()
