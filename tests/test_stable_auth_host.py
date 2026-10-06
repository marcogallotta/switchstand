import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import httpx
import pytest
from chatgpt_fixture import service
from fastmcp.server.auth.auth import AccessToken
from key_value.aio.stores.memory import MemoryStore

from switchstand.chatgpt_edge import create_delegated_app
from switchstand.stable_auth import (
    IntrospectionTokenVerifier,
    StableAuthConfig,
    create_auth_service,
)
from switchstand.stable_auth_host import (
    PUBLIC_CERTIFICATION_PATHS,
    PUBLIC_ISSUER_METADATA_PATHS,
    PUBLIC_MCP_PATHS,
    PUBLIC_OAUTH_PATHS,
    PUBLIC_RESOURCE_METADATA_PATHS,
    HostAssets,
    caddy_routes,
    internal_secret_receipt,
    provision_internal_secret,
    read_internal_secret,
    render_assets,
    rotate_internal_secret,
    systemd_units,
)
from switchstand.stable_auth_runtime import AuthRuntimeConfig, EdgeRuntimeConfig

RESOURCE = "https://switchstand.example/switchstand/mcp"
ISSUER = "https://switchstand.example/switchstand"
USER_ID = "192548"


def _credential(tmp_path: Path) -> Path:
    path = tmp_path / "internal.secret"
    provision_internal_secret(path)
    return path


def _assets(tmp_path: Path, credential: Path) -> HostAssets:
    auth_env = tmp_path / "auth.env"
    edge_env = tmp_path / "edge.env"
    auth_env.touch(mode=0o600)
    edge_env.touch(mode=0o600)
    runtime = tmp_path / "runtime"
    (runtime / "src/switchstand").mkdir(parents=True)
    return HostAssets(Path("/opt/switchstand/bin/python"), runtime, auth_env, edge_env, credential)


def test_internal_credential_provision_rotation_and_readback_are_bounded(tmp_path: Path):
    path = tmp_path / "internal.secret"
    first_digest = provision_internal_secret(path)
    first = read_internal_secret(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert internal_secret_receipt(path) == {"path": str(path), "sha256": first_digest}
    with pytest.raises(ValueError, match="already exists"):
        provision_internal_secret(path)
    with pytest.raises(ValueError, match="stale"):
        rotate_internal_secret(path, "0" * 64)
    assert read_internal_secret(path) == first
    second_digest = rotate_internal_secret(path, first_digest)
    assert read_internal_secret(path) != first
    assert internal_secret_receipt(path)["sha256"] == second_digest


def test_internal_credential_rejects_symlinks_modes_and_oversize(tmp_path: Path):
    target = _credential(tmp_path)
    link = tmp_path / "credential-link"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="unavailable"):
        read_internal_secret(link)
    target.chmod(0o640)
    with pytest.raises(ValueError, match="mode-0600"):
        read_internal_secret(target)
    target.chmod(0o600)
    target.write_text("x" * 257)
    with pytest.raises(ValueError, match="invalid size"):
        read_internal_secret(target)


@pytest.mark.parametrize("value", ("x", "x" * 63, "x" * 64, "x" * 64 + "\n"))
def test_internal_credential_rejects_weak_or_truncated_values(tmp_path: Path, value: str):
    target = tmp_path / "internal.secret"
    target.write_text(value)
    target.chmod(0o600)
    with pytest.raises(ValueError, match="generated-token format"):
        read_internal_secret(target)


def test_concurrent_credential_init_has_one_winner(tmp_path: Path):
    target = tmp_path / "internal.secret"
    barrier = Barrier(2)

    def initialize():
        barrier.wait()
        try:
            return ("ok", provision_internal_secret(target))
        except ValueError as exc:
            return ("error", str(exc))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: initialize(), range(2)))
    assert [status for status, _value in results].count("ok") == 1
    assert [status for status, _value in results].count("error") == 1
    assert any("already exists" in value for status, value in results if status == "error")
    assert internal_secret_receipt(target)["sha256"] == next(
        value for status, value in results if status == "ok"
    )


def test_concurrent_same_digest_rotation_has_one_winner(tmp_path: Path):
    target = tmp_path / "internal.secret"
    current_digest = provision_internal_secret(target)
    barrier = Barrier(2)

    def rotate():
        barrier.wait()
        try:
            return ("ok", rotate_internal_secret(target, current_digest))
        except ValueError as exc:
            return ("error", str(exc))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: rotate(), range(2)))
    assert [status for status, _value in results].count("ok") == 1
    assert [status for status, _value in results].count("error") == 1
    assert any("stale" in value for status, value in results if status == "error")
    assert internal_secret_receipt(target)["sha256"] == next(
        value for status, value in results if status == "ok"
    )


def test_rendered_units_separate_secret_bearing_auth_and_resource_edge(tmp_path: Path):
    credential = _credential(tmp_path)
    assets = _assets(tmp_path, credential)
    units = systemd_units(assets)
    auth = units["switchstand-stable-auth.service"]
    edge = units["switchstand-delegated-edge.service"]
    assert str(assets.auth_environment_file) in auth
    assert str(assets.edge_environment_file) not in auth
    assert str(assets.edge_environment_file) in edge
    assert str(assets.auth_environment_file) not in edge
    assert "stable_auth_runtime auth" in auth
    assert "stable_auth_runtime edge" in edge
    assert f"Environment=PYTHONPATH={assets.runtime_root}/src" in auth
    assert f"Environment=PYTHONPATH={assets.runtime_root}/src" in edge
    assert "Requires=switchstand-stable-auth.service" in edge

    output = tmp_path / "rendered"
    output.mkdir()
    receipt = render_assets(assets, output)
    assert set(receipt) == {*units, "switchstand-split-caddy-routes.json"}
    assert json.loads((output / "switchstand-split-caddy-routes.json").read_text()) == (
        caddy_routes(assets)
    )
    with pytest.raises(ValueError, match="refusing to replace"):
        render_assets(assets, output)


def test_caddy_contract_splits_only_upstream_and_never_exposes_introspection(tmp_path: Path):
    assets = _assets(tmp_path, _credential(tmp_path))
    routes = caddy_routes(assets)
    assert [route["match"][0]["path"] for route in routes] == [
        list(PUBLIC_MCP_PATHS),
        list(PUBLIC_RESOURCE_METADATA_PATHS + PUBLIC_CERTIFICATION_PATHS),
        list(PUBLIC_OAUTH_PATHS),
        list(PUBLIC_ISSUER_METADATA_PATHS),
    ]
    encoded = json.dumps(routes)
    assert "/internal/oauth/verify" not in encoded
    assert "/.well-known/switchstand-certification-runtime" in encoded
    assert encoded.count("127.0.0.1:8790") == 2
    assert encoded.count("127.0.0.1:8791") == 2
    assert routes[0]["handle"][0] == {
        "handler": "rewrite",
        "strip_path_prefix": "/switchstand",
    }
    assert routes[3]["handle"][0] == {
        "handler": "rewrite",
        "uri": "/.well-known/oauth-authorization-server",
    }


def test_split_runtime_configuration_is_loopback_only_and_edge_needs_no_oauth_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    credential = _credential(tmp_path)
    common = {
        "SWITCHSTAND_MCP_RESOURCE_URL": RESOURCE,
        "SWITCHSTAND_MCP_GITHUB_USER_ID": USER_ID,
        "SWITCHSTAND_INTERNAL_CREDENTIAL_FILE": str(credential),
    }
    for name, value in common.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SWITCHSTAND_AUTH_INTERNAL_URL", "http://127.0.0.1:8791")
    monkeypatch.delenv("SWITCHSTAND_MCP_GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("FASTMCP_HOME", raising=False)
    edge = EdgeRuntimeConfig.from_environment()
    assert edge.auth_url == "http://127.0.0.1:8791"
    assert edge.contract.issuer_url == ISSUER
    assert read_internal_secret(credential) not in repr(edge)

    for invalid in (
        "https://127.0.0.1:8791",
        "http://localhost:8791",
        "http://127.0.0.1:8791/internal/oauth/verify",
        "http://user@127.0.0.1:8791",
    ):
        monkeypatch.setenv("SWITCHSTAND_AUTH_INTERNAL_URL", invalid)
        with pytest.raises(ValueError, match="exact loopback"):
            EdgeRuntimeConfig.from_environment()

    monkeypatch.setenv("SWITCHSTAND_MCP_GITHUB_CLIENT_ID", "client")
    monkeypatch.setenv("SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET", "secret")
    with pytest.raises(ValueError, match="must not receive stable-auth state"):
        EdgeRuntimeConfig.from_environment()
    auth = AuthRuntimeConfig.from_environment()
    assert auth.auth.github_client_id == "client"
    assert "secret" not in repr(auth)
    assert read_internal_secret(credential) not in repr(auth)


async def test_disposable_split_apps_preserve_metadata_and_per_request_revocation():
    from switchstand.stable_auth import IntrospectionContract

    contract = IntrospectionContract.for_resource(RESOURCE, USER_ID)
    config = StableAuthConfig("client", "github-secret", "internal-secret", contract)
    auth_app = create_auth_service(config, client_storage=MemoryStore())
    provider = auth_app.state.auth_provider
    active = True

    async def verify(token: str):
        if not active or token != "legacy-token":
            return None
        return AccessToken(
            token=token,
            client_id="existing-client",
            scopes=["read:user"],
            subject=USER_ID,
            expires_at=int(time.time()) + 60,
            resource=RESOURCE,
            claims={"iss": ISSUER},
        )

    provider.verify_token = verify
    auth_transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(
        transport=auth_transport, base_url="http://127.0.0.1:8791"
    ) as auth_client:
        verifier = IntrospectionTokenVerifier(
            auth_client, internal_secret="internal-secret", contract=contract
        )
        edge_app = create_delegated_app(service(), verifier)
        edge_transport = httpx.ASGITransport(app=edge_app)
        async with httpx.AsyncClient(
            transport=edge_transport, base_url="http://127.0.0.1:8790"
        ) as edge_client:
            oauth = await auth_client.get("/.well-known/oauth-authorization-server")
            resource = await edge_client.get(
                "/.well-known/oauth-protected-resource/switchstand/mcp"
            )
            assert oauth.json()["issuer"] == ISSUER
            assert resource.json()["authorization_servers"] == [ISSUER]
            assert await verifier.verify_token("legacy-token") is not None
            active = False
            assert await verifier.verify_token("legacy-token") is None


def test_host_assets_reject_shared_environment_file(tmp_path: Path):
    shared = tmp_path / "shared.env"
    shared.touch()
    with pytest.raises(ValueError, match="separate environment"):
        HostAssets(Path("/bin/python"), tmp_path / "runtime", shared, shared, tmp_path / "secret")
