from __future__ import annotations

import json
import unittest

from switchstand.agent_tree import (
    MAX_DESCENDANT_RECORDS,
    MAX_PAGINATION_CURSOR_CHARACTERS,
    MAX_PROTOCOL_IDENTITY_CHARACTERS,
)
from switchstand.native_board import NativeBoard
from tests.test_native_board import (
    ClientFactory,
    FakeClient,
    client_with_status,
    native_thread,
)


class OverflowClient(FakeClient):
    def __init__(self, page_factory) -> None:
        super().__init__(native_thread("raw-root"), [])
        self.page_factory = page_factory
        self.stop_calls = 0

    def thread_list(self, params):
        self.list_requests.append(dict(params))
        return self.page_factory(len(self.list_requests), params)

    def stop_request(self, method, params, **limits):
        self.stop_calls += 1
        return super().stop_request(method, params, **limits)


class NativePaginationBoardTests(unittest.TestCase):
    def test_overflows_preserve_last_good_indices_and_send_no_action(self):
        secret_cursor = "c" * (MAX_PAGINATION_CURSOR_CHARACTERS + 1)
        secret_identity = "i" * (MAX_PROTOCOL_IDENTITY_CHARACTERS + 1)
        repeated = native_thread("private-overflow-id", parent="raw-root")
        cases = (
            (
                "page_count",
                lambda page, _params: {"data": [], "nextCursor": f"cursor-{page}"},
            ),
            (
                "record_count",
                lambda _page, _params: {
                    "data": [repeated] * (MAX_DESCENDANT_RECORDS + 1),
                    "nextCursor": None,
                },
            ),
            (
                "response_page",
                lambda _page, _params: {
                    "data": [repeated] * 101,
                    "nextCursor": None,
                },
            ),
            (
                "cursor_length",
                lambda _page, _params: {"data": [], "nextCursor": secret_cursor},
            ),
            (
                "identity_length",
                lambda _page, _params: {
                    "data": [native_thread(secret_identity, parent="raw-root")],
                    "nextCursor": None,
                },
            ),
        )
        for label, page_factory in cases:
            with self.subTest(label=label):
                overflow = OverflowClient(page_factory)
                board = NativeBoard(
                    ClientFactory(client_with_status(), overflow), "raw-root"
                )
                board.poll_once()
                before = board.snapshot()
                records_before = tuple(board._target_records)
                identities_before = dict(board._target_identities)
                reverse_before = dict(board._native_ids_by_target)

                board.poll_once()
                failed = board.snapshot()

                self.assertFalse(failed["observation"]["connected"])
                self.assertTrue(failed["observation"]["historical"])
                self.assertEqual(
                    failed["observation"]["errorCode"], "native_observation_unavailable"
                )
                self.assertEqual(
                    failed["observation"]["completedAt"],
                    before["observation"]["completedAt"],
                )
                self.assertEqual(failed["trail"], before["trail"])
                self.assertEqual(tuple(board._target_records), records_before)
                self.assertEqual(board._target_identities, identities_before)
                self.assertEqual(board._native_ids_by_target, reverse_before)
                self.assertEqual(overflow.stop_calls, 0)
                emitted = json.dumps(failed)
                self.assertNotIn(secret_cursor, emitted)
                self.assertNotIn(secret_identity, emitted)
                self.assertNotIn("private-overflow-id", emitted)


if __name__ == "__main__":
    unittest.main()
