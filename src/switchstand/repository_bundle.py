from __future__ import annotations

import hashlib
from typing import Literal

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

REPOSITORY = "marcogallotta/switchstand"
RELEASE_TAG = "repository-bundle-current"
BUNDLE_NAME = "switchstand-current.bundle"
MANIFEST_NAME = "switchstand-current.json"


class RepositoryBundleState(BaseModel):
    status: Literal["current", "refresh_pending", "unavailable"]
    repository: str = REPOSITORY
    required_sha: str | None = None
    snapshot_sha256: str | None = None
    bundle_sha256: str | None = None
    bundle_url: str | None = None
    bundle_size: int | None = None
    ref_count: int | None = None
    reason: str | None = None


class _Manifest(BaseModel):
    schema_version: Literal["1"]
    repository: str
    refs: dict[str, str]
    snapshot_sha256: str
    bundle_sha256: str
    event_sha: str
    run_id: str
    run_attempt: str


class _ReleaseAsset(BaseModel):
    name: str
    browser_download_url: str
    digest: str | None = None
    size: int


class _Release(BaseModel):
    assets: list[_ReleaseAsset]


class _BranchCommit(BaseModel):
    sha: str


class _Branch(BaseModel):
    name: str
    commit: _BranchCommit


_BRANCHES = TypeAdapter(list[_Branch])


def canonical_ref_bytes(refs: dict[str, str]) -> bytes:
    return "".join(f"{name} {refs[name]}\n" for name in sorted(refs)).encode()


def ref_snapshot_digest(refs: dict[str, str]) -> str:
    return hashlib.sha256(canonical_ref_bytes(refs)).hexdigest()


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(char in "0123456789abcdef" for char in value)


def _valid_ref_map(refs: dict[str, str]) -> bool:
    return bool(refs) and all(
        name.startswith("refs/heads/") and _is_hex(sha, 40)
        for name, sha in refs.items()
    )


async def _current_refs(client: httpx.AsyncClient) -> dict[str, str]:
    refs: dict[str, str] = {}
    url: str | None = f"/repos/{REPOSITORY}/branches?per_page=100"
    while url is not None:
        response = await client.get(url)
        response.raise_for_status()
        branches = _BRANCHES.validate_python(response.json())
        refs.update({f"refs/heads/{branch.name}": branch.commit.sha for branch in branches})
        url = response.links.get("next", {}).get("url")
    return refs


async def resolve_repository_bundle(
    required_sha: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> RepositoryBundleState:
    owned_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            follow_redirects=True,
            timeout=10.0,
            trust_env=False,
        )

    try:
        release_response = await client.get(
            f"/repos/{REPOSITORY}/releases/tags/{RELEASE_TAG}"
        )
        if release_response.status_code == 404:
            return RepositoryBundleState(
                status="refresh_pending",
                required_sha=required_sha,
                reason="cache_release_missing",
            )
        release_response.raise_for_status()
        release = _Release.model_validate(release_response.json())
        assets = {asset.name: asset for asset in release.assets}
        bundle_asset = assets.get(BUNDLE_NAME)
        manifest_asset = assets.get(MANIFEST_NAME)
        if bundle_asset is None or manifest_asset is None:
            return RepositoryBundleState(
                status="refresh_pending",
                required_sha=required_sha,
                reason="cache_assets_in_transition",
            )

        manifest_response = await client.get(manifest_asset.browser_download_url)
        if manifest_response.status_code == 404:
            return RepositoryBundleState(
                status="refresh_pending",
                required_sha=required_sha,
                reason="manifest_in_transition",
            )
        manifest_response.raise_for_status()
        manifest = _Manifest.model_validate_json(manifest_response.text)

        if (
            manifest.repository != REPOSITORY
            or not _valid_ref_map(manifest.refs)
            or not _is_hex(manifest.snapshot_sha256, 64)
            or not _is_hex(manifest.bundle_sha256, 64)
            or ref_snapshot_digest(manifest.refs) != manifest.snapshot_sha256
        ):
            return RepositoryBundleState(
                status="refresh_pending",
                required_sha=required_sha,
                reason="manifest_invalid",
            )

        if bundle_asset.digest != f"sha256:{manifest.bundle_sha256}":
            return RepositoryBundleState(
                status="refresh_pending",
                required_sha=required_sha,
                reason="bundle_manifest_digest_mismatch",
            )

        refs = await _current_refs(client)
        if not _valid_ref_map(refs):
            return RepositoryBundleState(
                status="unavailable",
                required_sha=required_sha,
                reason="authoritative_refs_unavailable",
            )
        if refs != manifest.refs or ref_snapshot_digest(refs) != manifest.snapshot_sha256:
            return RepositoryBundleState(
                status="refresh_pending",
                required_sha=required_sha,
                reason="authoritative_refs_advanced",
            )

        return RepositoryBundleState(
            status="current",
            required_sha=required_sha,
            snapshot_sha256=manifest.snapshot_sha256,
            bundle_sha256=manifest.bundle_sha256,
            bundle_url=bundle_asset.browser_download_url,
            bundle_size=bundle_asset.size,
            ref_count=len(refs),
        )
    except (httpx.HTTPError, ValidationError, ValueError):
        return RepositoryBundleState(
            status="unavailable",
            required_sha=required_sha,
            reason="provider_unavailable",
        )
    finally:
        if owned_client:
            await client.aclose()
