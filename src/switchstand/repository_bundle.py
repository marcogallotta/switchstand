from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

REPOSITORY = "marcogallotta/switchstand"
RELEASE_TAG = "switchstand-repository-bundle-current"
BUNDLE_NAME = "switchstand-current.bundle"
MANIFEST_NAME = "switchstand-current.manifest.json"
CHECKSUM_NAME = "switchstand-current.bundle.sha256"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{RELEASE_TAG}"
REFS_API = f"https://api.github.com/repos/{REPOSITORY}/git/matching-refs/heads/"


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


async def resolve_repository_bundle(
    required_sha: str | None = None, client: httpx.AsyncClient | None = None,
) -> RepositoryBundleResolution:
    owned = client is None
    http = client or httpx.AsyncClient(
        follow_redirects=True,
        timeout=10.0,
        trust_env=False,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        release_response = await http.get(RELEASE_API)
        if release_response.status_code == 404:
            return _pending(required_sha, "cache_missing")
        release_response.raise_for_status()
        release = release_response.json()
        bundle_asset = _asset(release, BUNDLE_NAME)
        manifest_asset = _asset(release, MANIFEST_NAME)
        checksum_asset = _asset(release, CHECKSUM_NAME)
        if bundle_asset is None or manifest_asset is None or checksum_asset is None:
            return _pending(required_sha, "cache_transition")

        manifest_response = await http.get(str(manifest_asset["browser_download_url"]))
        manifest_response.raise_for_status()
        manifest = manifest_response.json()
        checksum_response = await http.get(str(checksum_asset["browser_download_url"]))
        checksum_response.raise_for_status()
        checksum = checksum_response.text.strip().split()[0]

        refs_response = await http.get(REFS_API)
        refs_response.raise_for_status()
        authoritative = {
            str(item["ref"]): str(item["object"]["sha"])
            for item in refs_response.json()
            if str(item.get("ref", "")).startswith("refs/heads/")
        }
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
        if authoritative != manifest_refs:
            return _pending(required_sha, "refs_advanced")
        return RepositoryBundleResolution(
            status="current",
            snapshot_digest=snapshot_digest,
            bundle_sha256=expected_checksum,
            bundle_url=str(bundle_asset["browser_download_url"]),
            refs=manifest_refs,
            required_sha=required_sha,
            required_sha_is_head=(
                True if required_sha and required_sha in manifest_refs.values() else None
            ),
        )
    except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return RepositoryBundleResolution(
            status="unavailable", required_sha=required_sha, reason="github_unavailable"
        )
    finally:
        if owned:
            await http.aclose()
