import pytest

from switchstand.human_review_caddy import caddy_route


def test_route_maps_explicit_public_prefix_to_fixed_internal_path_and_loopback() -> None:
    route = caddy_route("/switchstand/human-review", 8792)

    assert route["match"] == [
        {"path": ["/switchstand/human-review", "/switchstand/human-review/*"]}
    ]
    assert route["handle"] == [
        {"handler": "rewrite", "strip_path_prefix": "/switchstand"},
        {
            "@id": "switchstand_human_review_upstream",
            "handler": "reverse_proxy",
            "upstreams": [{"dial": "127.0.0.1:8792"}],
        },
    ]
    assert route["terminal"] is True


def test_route_at_internal_path_needs_no_rewrite() -> None:
    route = caddy_route("/human-review", 9000)

    assert [handler["handler"] for handler in route["handle"]] == ["reverse_proxy"]
    assert route["handle"][0]["upstreams"] == [{"dial": "127.0.0.1:9000"}]


@pytest.mark.parametrize(
    "path",
    [
        "human-review",
        "/switchstand/review",
        "/switchstand/human-review/",
        "/switchstand/*/human-review",
        "/switchstand/%2f/human-review",
        "/switch stand/human-review",
        "/./human-review",
        "/switchstand/../human-review",
    ],
)
def test_route_rejects_nonliteral_or_wrong_internal_paths(path: str) -> None:
    with pytest.raises(ValueError, match="literal path"):
        caddy_route(path, 8792)


@pytest.mark.parametrize("port", [0, 65536])
def test_route_rejects_invalid_ports(port: int) -> None:
    with pytest.raises(ValueError, match="between"):
        caddy_route("/human-review", port)
