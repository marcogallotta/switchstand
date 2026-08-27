from __future__ import annotations

from collections import deque
from copy import deepcopy
import json
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures" / "app_server"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ProbeClient:
    def __init__(self) -> None:
        self.root = fixture("thread_read_root.json")
        self.pages = deque(
            [
                fixture("thread_list_descendants_page_1.json"),
                fixture("thread_list_descendants_page_2.json"),
            ]
        )
        self.queued = []
        self.waiting = deque()
        self.resumes = []
        self.resume_events = {}
        self.resume_return_ids = {}
        self.resume_exceptions = {}
        self.resume_threads = {
            "root-1": deepcopy(self.root["thread"]),
            "child-1": deepcopy(self.pages[0]["data"][0]),
            "grandchild-1": deepcopy(self.pages[1]["data"][0]),
        }

    def thread_read(self, thread_id, *, include_turns=True):
        return deepcopy(self.root)

    def thread_list(self, params):
        return deepcopy(self.pages.popleft())

    def drain_server_messages(self):
        result = deepcopy(self.queued)
        self.queued.clear()
        return result

    def next_server_message(self, *, timeout_seconds=None):
        if not self.waiting:
            raise TimeoutError
        return deepcopy(self.waiting.popleft())

    def thread_resume(self, thread_id):
        self.resumes.append(thread_id)
        failure = self.resume_exceptions.get(thread_id)
        if failure is not None:
            raise failure
        event = self.resume_events.get(thread_id)
        if event is not None:
            self.queued.append(deepcopy(event))
        return {
            "thread": {
                "id": self.resume_return_ids.get(
                    thread_id, self.resume_threads[thread_id]["id"]
                )
            }
        }

    def close(self):
        pass
