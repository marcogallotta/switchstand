"""Route native observation reads through an existing connection safety budget."""
from __future__ import annotations

from typing import Any, Mapping, Protocol


class ObservationClient(Protocol):
    _bounded_response_bytes_remaining: int | None

    def _request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def bounded_request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        _close_after: bool = True,
    ) -> tuple[str, Mapping[str, Any] | None]: ...


def observation_request(
    client: ObservationClient, method: str, params: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Use the shared deadline and byte budget when the client has one."""
    if getattr(client, "_bounded_response_bytes_remaining", None) is None:
        return client._request(method, params)
    classification, result = client.bounded_request(method, params, _close_after=False)
    if classification != "ok" or result is None:
        raise RuntimeError("bounded App Server observation is unavailable")
    return result
