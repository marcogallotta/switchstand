import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from switchstand import edge_monitor_host as host
from switchstand.edge_monitor import CanaryTarget, FunctionalStatus

REAL_CLIENT = httpx.Client


def test_demo_proves_dedup_and_two_healthy_cycle_recovery(tmp_path, capsys):
    assert host.main(["--state-dir", str(tmp_path), "demo"]) == 0

    output = json.loads(capsys.readouterr().out)

    assert [cycle["condition"] for cycle in output["cycles"]] == [
        "healthy", "oauth_failure", "oauth_failure", "healthy", "healthy",
    ]
    assert [cycle["emitted"] for cycle in output["cycles"]] == [
        False, True, False, False, True,
    ]
    assert [event["kind"] for event in output["events"]] == [
        "edge.oauth_failure", "edge.recovered",
    ]
    assert tmp_path.stat().st_mode & 0o7777 == 0o700
    assert "bad_refresh_token" not in capsys.readouterr().out


def test_events_prints_only_sanitized_outbox(tmp_path, capsys):
    assert host.main([
        "--state-dir", str(tmp_path), "check", "--fixture", "bad_refresh_token",
    ]) == 0
    capsys.readouterr()

    assert host.main(["--state-dir", str(tmp_path), "events"]) == 0

    events = json.loads(capsys.readouterr().out)
    assert events[0]["summary"] == "OAuth token exchange or refresh is failing"
    assert "bad_refresh_token" not in json.dumps(events)


def test_canary_requires_mode_0600_token_and_uuid(tmp_path):
    token = tmp_path / "token"
    token.write_text("secret-value\n")
    os.chmod(token, 0o600)
    work_id = "a3b2421c-7a04-41f7-b868-9880e460a7a0"

    canary, value = host._canary(token, work_id)

    assert canary == CanaryTarget("fixed-read-only-bearer", work_id)
    assert value == "secret-value"
    assert host._functional_target("http://127.0.0.1:8790/mcp", None,
                                   "https://real.example/mcp") == "http://127.0.0.1:8790/mcp"
    assert host._functional_target("http://127.0.0.1:8790/mcp", "https://real.example/mcp",
                                   "https://real.example/mcp") == "https://real.example/mcp"
    os.chmod(token, 0o640)
    assert host._canary(token, work_id) == (None, "")
    assert host._canary(token, "not-a-work-id") == (None, "")


def test_systemd_and_journal_use_fixed_argv_and_cursor(monkeypatch):
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[0] == "systemctl":
            return "MainPID=2345\nActiveState=active\n"
        return (
            '{"__CURSOR":"cursor-2","MESSAGE":"Upstream token refresh failed"}\n'
            "-- cursor: cursor-2\n"
        )

    monkeypatch.setattr(host, "_run", run)

    assert host.HostSystemd("edge.service").observe().main_pid == 2345
    batch = host.HostJournal("edge.service").read_after("cursor-1")

    assert batch.next_cursor == "cursor-2"
    assert batch.messages == ("Upstream token refresh failed",)
    assert calls == [
        ["systemctl", "--user", "show", "edge.service", "--property=ActiveState",
         "--property=MainPID"],
        ["journalctl", "--user", "--unit", "edge.service", "--show-cursor",
         "--after-cursor", "cursor-1", "--output=json"],
    ]


def client_with(handler):
    return lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)


def test_http_probe_checks_local_and_public_challenge_and_metadata(monkeypatch):
    resource = "https://edge.example/mcp"
    metadata = "https://edge.example/.well-known/oauth-protected-resource/mcp"
    seen = []

    def respond(request):
        seen.append(str(request.url))
        if request.method == "POST":
            return httpx.Response(401, headers={
                "www-authenticate": f'Bearer resource_metadata="{metadata}"'
            })
        return httpx.Response(200, json={"resource": resource})

    monkeypatch.setattr(host.httpx, "Client", client_with(respond))

    result = host.HostHttp(("http://127.0.0.1:8765/mcp", resource), resource).observe()

    assert result.transport_ok and result.status_code == 401 and result.valid_auth_challenge
    assert len(seen) == 4


def test_external_ingress_probe_forces_public_dns_addresses(monkeypatch):
    resource = "https://edge.example/mcp"
    metadata = "https://edge.example/.well-known/oauth-protected-resource/mcp"
    requests = []

    def resolve(request):
        assert request.url.host == "resolver.example"
        return httpx.Response(200, json={"Answer": [
            {"type": 1, "data": "203.0.113.1"},
            {"type": 1, "data": "8.8.8.8"},
            {"type": 5, "data": "ignored.example"},
        ]})

    def request(hostname, address, method, path):
        requests.append((hostname, address, method, path))
        if method == "POST":
            return 401, f'Bearer resource_metadata="{metadata}"', b""
        return 200, "", json.dumps({"resource": resource}).encode()

    monkeypatch.setattr(host.httpx, "Client", client_with(resolve))
    monkeypatch.setattr(host.ExternalIngressHttp, "_request", staticmethod(request))

    result = host.ExternalIngressHttp(
        resource, resource, "https://resolver.example/dns-query"
    ).observe()

    assert result == host.HttpObservation(True, 401, valid_auth_challenge=True)
    assert requests == [
        ("edge.example", "8.8.8.8", "POST", "/mcp"),
        ("edge.example", "8.8.8.8", "GET", "/.well-known/oauth-protected-resource/mcp"),
    ]


def test_external_ingress_probe_rejects_split_dns_only_result(monkeypatch):
    def resolve(_request):
        return httpx.Response(200, json={"Answer": [
            {"type": 1, "data": "100.100.100.100"},
        ]})

    monkeypatch.setattr(host.httpx, "Client", client_with(resolve))

    result = host.ExternalIngressHttp(
        "https://edge.example/mcp",
        "https://edge.example/mcp",
        "https://resolver.example/dns-query",
    ).observe()

    assert result == host.HttpObservation(False, None)


@pytest.mark.parametrize(
    ("structured", "expected"),
    [
        ({"status": "ok", "item": {"id": "a3b2421c-7a04-41f7-b868-9880e460a7a0"}},
         FunctionalStatus.OK),
        ({"status": "ok"}, FunctionalStatus.FAILED),
        ({"status": "ok", "item": {"id": "00000000-0000-0000-0000-000000000002"}},
         FunctionalStatus.FAILED),
        ({"status": "provider_error"}, FunctionalStatus.PROVIDER_ERROR),
    ],
)
def test_functional_probe_requires_exact_work_result(monkeypatch, structured, expected):
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["method"] == "initialize":
            return httpx.Response(200, headers={"mcp-session-id": "fixed"}, json={"result": {}})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(200, json={
            "result": {"structuredContent": structured}
        })

    monkeypatch.setattr(host.httpx, "Client", client_with(respond))
    target = CanaryTarget("fixed", "a3b2421c-7a04-41f7-b868-9880e460a7a0")

    result = host.HostFunctional("https://edge.example/mcp", "secret").observe(target)

    assert result.status is expected
    assert requests[-1]["params"] == {
        "name": "work_get",
        "arguments": {"api_version": "1", "work_id": target.work_id},
    }


@pytest.mark.parametrize(
    ("local_url", "public_url"),
    [
        ("http://attacker.example/mcp", None),
        ("http://127.0.0.1:8790/mcp", "https://attacker.example/mcp"),
    ],
)
def test_spoofed_endpoint_cannot_read_or_send_bearer(
    monkeypatch, tmp_path, local_url, public_url,
):
    token = tmp_path / "token"
    token.write_text("must-not-be-read")
    os.chmod(token, 0o600)
    resource = "https://real.example/switchstand/mcp"
    metadata = "https://real.example/.well-known/oauth-protected-resource/switchstand/mcp"
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(401, headers={
                "www-authenticate": f'Bearer resource_metadata="{metadata}"'
            })
        return httpx.Response(200, json={"resource": resource})

    monkeypatch.setattr(host.httpx, "Client", client_with(respond))
    monkeypatch.setattr(host, "HostSystemd", lambda _service: host.FixedProbe(
        host.SystemdObservation(True, 1234)
    ))
    monkeypatch.setattr(host, "HostJournal", lambda _service: host.FixedJournal(""))

    def forbidden_read(_self):
        raise AssertionError("untrusted endpoint must be rejected before token read")

    monkeypatch.setattr(Path, "read_text", forbidden_read)
    args = SimpleNamespace(
        fixture=None, bearer_token_file=token,
        work_id="a3b2421c-7a04-41f7-b868-9880e460a7a0",
        local_url=local_url, public_url=public_url, resource_url=resource,
        service="edge.service", public_dns_url=host.PUBLIC_DNS_URL,
    )

    result = host._check(args, tmp_path / "state")

    assert result["condition"] == "missing_capability"
    assert requests
    assert all("authorization" not in request.headers for request in requests)
