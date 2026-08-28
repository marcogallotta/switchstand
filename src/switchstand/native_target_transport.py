"""Private App Server transport for one opaque current native target."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from .app_server import CodexAppServer
from .current_target import ExactCurrentTarget
from .native_board import NativeBoard
from .native_turns import project_exact_turn_list


TransportResult = tuple[str, Mapping[str, Any] | None]
_UNAVAILABLE: TransportResult = ("unavailable", None)


class NativeTargetTransport:
    """Bind opaque targets inside the board, then make one bounded request."""

    def __init__(self, board: NativeBoard, socket_path: Path | str) -> None:
        self._board = board
        self._socket_path = Path(socket_path)

    def _perform(
        self,
        target: object,
        *,
        max_response_bytes: int,
        timeout_seconds: float,
        operation: Callable[[CodexAppServer, str], TransportResult],
    ) -> TransportResult:
        if type(target) is not ExactCurrentTarget:
            return _UNAVAILABLE

        def bound(native_thread_id: str) -> TransportResult:
            client = CodexAppServer(
                self._socket_path,
                client_name="switchstand-native-input",
                timeout_seconds=timeout_seconds,
                bounded_stop=True,
                bounded_response_bytes=max_response_bytes,
            )
            return operation(client, native_thread_id)

        try:
            result = self._board._with_current_native_target(target, bound)
        except Exception:
            return ("ambiguous", None)
        return _UNAVAILABLE if result is None else result

    def turns_list(
        self,
        target: object,
        *,
        max_response_bytes: int,
        timeout_seconds: float,
    ) -> TransportResult:
        """Read content-free newest-turn metadata for one current target."""

        def read(client: CodexAppServer, native_thread_id: str) -> TransportResult:
            classification, response = client.stop_request(
                "thread/turns/list", {"threadId": native_thread_id, "limit": 1,
                    "sortDirection": "desc", "itemsView": "notLoaded"},
                max_response_bytes=max_response_bytes,
                timeout_seconds=timeout_seconds,
            )
            if classification != "ok" or response is None:
                return classification, None
            if "target" in response:
                return ("malformed", None)
            rebound_response = dict(response)
            rebound_response["target"] = target
            if project_exact_turn_list(rebound_response, target) is None:
                return ("malformed", None)
            return ("ok", rebound_response)

        return self._perform(
            target,
            max_response_bytes=max_response_bytes,
            timeout_seconds=timeout_seconds,
            operation=read,
        )

    def turn_start(
        self,
        target: object,
        text: str,
        *,
        max_response_bytes: int,
        timeout_seconds: float,
    ) -> TransportResult:
        """Start exactly one bounded turn on the currently bound target."""
        return self._perform(
            target,
            max_response_bytes=max_response_bytes,
            timeout_seconds=timeout_seconds,
            operation=lambda client, native_thread_id: (
                client.bounded_turn_start_text_native(
                    native_thread_id,
                    text,
                    max_response_bytes=max_response_bytes,
                    timeout_seconds=timeout_seconds,
                )
            ),
        )

    def turn_steer(
        self,
        target: object,
        expected_turn_id: str,
        text: str,
        *,
        max_response_bytes: int,
        timeout_seconds: float,
    ) -> TransportResult:
        """Steer exactly one bounded active turn on the currently bound target."""
        return self._perform(
            target,
            max_response_bytes=max_response_bytes,
            timeout_seconds=timeout_seconds,
            operation=lambda client, native_thread_id: (
                client.bounded_turn_steer_text(
                    native_thread_id,
                    expected_turn_id,
                    text,
                    max_response_bytes=max_response_bytes,
                    timeout_seconds=timeout_seconds,
                )
            ),
        )
