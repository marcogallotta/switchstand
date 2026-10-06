from typing import Any

import pytest

from switchstand import human_review_runtime as runtime


def _environment() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql+psycopg://switchstand@localhost/switchstand",
        "SWITCHSTAND_HUMAN_REVIEW_ORIGIN": "https://review.example.test",
        "SWITCHSTAND_HUMAN_REVIEW_USERNAME": "marco",
        "SWITCHSTAND_HUMAN_REVIEW_PASSWORD_HASH": "$2b$12$valid-at-shell-boundary",
        "SWITCHSTAND_HUMAN_REVIEW_BIND_HOST": "127.0.0.1",
        "SWITCHSTAND_HUMAN_REVIEW_BIND_PORT": "8792",
    }


@pytest.mark.parametrize("name", _environment())
def test_runtime_requires_every_explicit_configuration_value(name: str) -> None:
    environment = _environment()
    environment[name] = "  "

    with pytest.raises(ValueError, match=name):
        runtime.HumanReviewRuntimeConfig.from_environment(environment)


@pytest.mark.parametrize("host", ["localhost", "0.0.0.0", "192.0.2.1"])
def test_runtime_rejects_non_loopback_or_implicit_hosts(host: str) -> None:
    environment = _environment()
    environment["SWITCHSTAND_HUMAN_REVIEW_BIND_HOST"] = host

    with pytest.raises(ValueError, match="loopback IP"):
        runtime.HumanReviewRuntimeConfig.from_environment(environment)


@pytest.mark.parametrize("port", ["zero", "0", "65536"])
def test_runtime_rejects_invalid_ports(port: str) -> None:
    environment = _environment()
    environment["SWITCHSTAND_HUMAN_REVIEW_BIND_PORT"] = port

    with pytest.raises(ValueError, match="integer|between 1 and 65535"):
        runtime.HumanReviewRuntimeConfig.from_environment(environment)


async def test_runtime_serves_exact_loopback_config_and_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Any] = []

    class Engine:
        async def dispose(self) -> None:
            events.append("disposed")

    class Server:
        def __init__(self, config: Any) -> None:
            events.append(("configured", config.app, config.host, config.port))

        async def serve(self) -> None:
            events.append("served")

    engine = Engine()
    app = object()
    monkeypatch.setattr(runtime, "create_async_engine", lambda url: events.append(url) or engine)
    monkeypatch.setattr(
        runtime,
        "create_persistent_human_review_shell",
        lambda actual, **values: events.append((actual, values)) or app,
    )
    monkeypatch.setattr(runtime.uvicorn, "Server", Server)

    config = runtime.HumanReviewRuntimeConfig.from_environment(_environment())
    await runtime.serve(config)

    assert events[-3:] == [
        ("configured", app, "127.0.0.1", 8792),
        "served",
        "disposed",
    ]


async def test_shell_configuration_is_validated_before_listen_and_engine_is_disposed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Engine:
        async def dispose(self) -> None:
            events.append("disposed")

    monkeypatch.setattr(runtime, "create_async_engine", lambda _url: Engine())
    monkeypatch.setattr(
        runtime.uvicorn,
        "Server",
        lambda _config: pytest.fail("invalid shell configuration reached listener creation"),
    )
    environment = _environment()
    environment["SWITCHSTAND_HUMAN_REVIEW_ORIGIN"] = "http://review.example.test"

    with pytest.raises(ValueError, match="exact HTTPS origin"):
        await runtime.serve(runtime.HumanReviewRuntimeConfig.from_environment(environment))

    assert events == ["disposed"]
