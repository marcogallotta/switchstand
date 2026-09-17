"""Read-only checks for an activated ChatGPT MCP edge."""
# pyright: reportPrivateUsage=false
import argparse
import subprocess
from pathlib import Path
from typing import cast

import httpx

from .chatgpt_edge import _bind_port, _https_resource_url, _loopback_host

REQUIRED_KEYS = frozenset(("SWITCHSTAND_MCP_GITHUB_CLIENT_ID",
    "SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET", "SWITCHSTAND_MCP_GITHUB_USER_ID",
    "SWITCHSTAND_MCP_RESOURCE_URL", "SWITCHSTAND_MCP_BIND_HOST",
    "SWITCHSTAND_MCP_BIND_PORT", "DATABASE_URL", "ASANA_TOKEN"))
def _result(name: str, status: str, detail: str) -> bool:
    print(f"{status} {name}: {detail}")
    return status != "FAIL"

def _read_env(path: Path) -> tuple[dict[str, str] | None, bool]:
    try:
        mode = path.stat().st_mode & 0o7777
        values = {key.strip(): value.strip().strip("'\"") for line in path.read_text().splitlines()
                  if line.strip() and not line.lstrip().startswith("#") and "=" in line
                  for key, value in [line.split("=", 1)]}
    except (OSError, UnicodeError) as exc:
        ok = _result("env_file", "FAIL", type(exc).__name__)
        _result("env_keys", "NOT_RUN", "environment unavailable")
        return None, ok
    ok = _result("env_file", "PASS" if mode == 0o600 else "FAIL", f"mode {mode:04o}")
    missing = sorted(key for key in REQUIRED_KEYS if not values.get(key))
    ok &= _result("env_keys", "FAIL" if missing else "PASS",
                  "missing " + ",".join(missing) if missing else "all required keys present")
    return values, ok

def _probe(name: str, target: str, resource: str) -> bool:
    metadata = resource.removesuffix("/mcp") + "/.well-known/oauth-protected-resource/mcp"
    endpoint = target.removesuffix("/mcp") + "/.well-known/oauth-protected-resource/mcp"
    try:
        url = httpx.URL(target)
        if url.username or url.password:
            return _result(name, "FAIL", "probe URL must not contain credentials")
        with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
            challenge = client.post(target)
            document = client.get(endpoint)
            actual = document.json()
        valid = (challenge.status_code == 401
                 and f'resource_metadata="{metadata}"' in challenge.headers.get("www-authenticate", "")
                 and document.status_code == 200 and isinstance(actual, dict)
                 and cast(dict[str, object], actual).get("resource") == resource)
        return _result(name, "PASS" if valid else "FAIL", "challenge and resource metadata exact"
                       if valid else "unexpected challenge or resource metadata")
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        return _result(name, "FAIL", type(exc).__name__)

def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--public-url")
    parser.add_argument("--expected-sha", help="expected checkout HEAD; does not verify running code")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    values, ok = _read_env(args.env_file)
    if values is None:
        _result("local_http", "NOT_RUN", "environment unavailable")
        _result("public_http", "NOT_RUN", "environment unavailable")
    else:
        try:
            resource = _https_resource_url(values.get("SWITCHSTAND_MCP_RESOURCE_URL", ""))
            host = _loopback_host(values.get("SWITCHSTAND_MCP_BIND_HOST", ""))
            port = _bind_port(values.get("SWITCHSTAND_MCP_BIND_PORT", ""))
            local_host = f"[{host}]" if ":" in host else host
            ok &= _probe("local_http", f"http://{local_host}:{port}/mcp", resource)
            if args.public_url:
                ok &= _probe("public_http", args.public_url, resource)
            else:
                _result("public_http", "NOT_RUN", "no public URL supplied")
        except ValueError as exc:
            ok &= _result("local_http", "FAIL", f"invalid edge configuration ({type(exc).__name__})")
            _result("public_http", "NOT_RUN", "invalid edge configuration")
    if args.expected_sha:
        try:
            actual = subprocess.run(["git", "-C", str(args.repo), "rev-parse", "HEAD"],
                                    check=True, capture_output=True, text=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            ok &= _result("checkout_sha", "FAIL", type(exc).__name__)
        else:
            exact = actual == args.expected_sha
            ok &= _result("checkout_sha", "PASS" if exact else "FAIL", actual)
    else:
        _result("checkout_sha", "NOT_RUN", "no expected SHA supplied")
    return 0 if ok else 1
