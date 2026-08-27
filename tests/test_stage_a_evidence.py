from __future__ import annotations

from collections import deque
from copy import deepcopy
import json
import unittest

from stage_a_probe_support import ProbeClient, fixture
from switchstand.agent_tree import THREAD_SOURCE_KINDS
from switchstand.stage_a_probe import collect_evidence


class StageAEvidenceProjectionTests(unittest.TestCase):
    def test_every_untrusted_surface_retains_only_the_exact_allowed_projection(self):
        sentinels = {
            "prompt": "SENTINEL-PROMPT-4f371f",
            "output": "SENTINEL-OUTPUT-b6b0fb",
            "token": "SENTINEL-TOKEN-c6f753",
            "credential": "SENTINEL-CREDENTIAL-85eb02",
            "path": "/private/SENTINEL-PATH-31a914",
            "cursor": "SENTINEL-CURSOR-f17d21",
            "unrelated": "SENTINEL-UNRELATED-0a85aa",
        }
        root_id = sentinels["token"]
        child_id = sentinels["output"]
        grandchild_id = "SENTINEL-GRANDCHILD-533e48"

        client = ProbeClient()
        client.root["thread"].update(
            {
                "id": root_id,
                "sessionId": sentinels["credential"],
                "preview": sentinels["output"],
                "prompt": sentinels["prompt"],
                "token": sentinels["token"],
                "credential": sentinels["credential"],
                "path": sentinels["path"],
            }
        )
        page_one = fixture("thread_list_descendants_page_1.json")
        child = page_one["data"][0]
        child.update(
            {
                "id": child_id,
                "sessionId": sentinels["path"],
                "parentThreadId": root_id,
                "preview": sentinels["prompt"],
                "output": sentinels["output"],
                "token": sentinels["token"],
                "credential": sentinels["credential"],
            }
        )
        spawn = child["source"]["subAgent"]["thread_spawn"]
        spawn.update(
            {
                "parent_thread_id": root_id,
                "agent_path": sentinels["path"],
                "agent_nickname": sentinels["prompt"],
                "agent_role": sentinels["credential"],
                "prompt": sentinels["prompt"],
                "output": sentinels["output"],
                "token": sentinels["token"],
                "credential": sentinels["credential"],
                "unknown": {"path": sentinels["path"]},
            }
        )
        child["status"]["detail"] = sentinels["credential"]
        page_one["nextCursor"] = sentinels["cursor"]
        page_one["backwardsCursor"] = sentinels["path"]

        page_two = fixture("thread_list_descendants_page_2.json")
        grandchild = page_two["data"][0]
        grandchild.update(
            {
                "id": grandchild_id,
                "sessionId": "SENTINEL-SESSION-c53d6c",
                "parentThreadId": child_id,
                "preview": sentinels["prompt"],
                "credential": sentinels["credential"],
            }
        )
        page_two["backwardsCursor"] = sentinels["cursor"]
        client.pages = deque(
            [
                deepcopy(page_one),
                deepcopy(page_two),
                deepcopy(page_one),
                deepcopy(page_two),
            ]
        )
        client.resume_threads = {
            root_id: deepcopy(client.root["thread"]),
            child_id: deepcopy(child),
            grandchild_id: deepcopy(grandchild),
        }
        client.queued.append(
            {
                "method": "thread/status/changed",
                "params": {
                    "threadId": sentinels["unrelated"],
                    "status": {
                        "type": "idle",
                        "detail": sentinels["credential"],
                        "output": sentinels["output"],
                    },
                    "prompt": sentinels["prompt"],
                },
            }
        )
        client.resume_events[child_id] = {
            "method": "thread/status/changed",
            "params": {
                "threadId": child_id,
                "status": {
                    "type": "active",
                    "activeFlags": ["waitingOnUserInput"],
                    "detail": sentinels["credential"],
                    "output": sentinels["output"],
                },
                "prompt": sentinels["prompt"],
            },
        }
        times = iter(["initial-start", "initial-end", "again-start", "again-end", "event-time"])
        monotonic_values = iter([0.0, 0.0])

        evidence = collect_evidence(
            client,
            root_id,
            notification_wait_seconds=1.0,
            require_status_notification=True,
            subscribe_status_notifications=True,
            now=lambda: next(times),
            monotonic=lambda: next(monotonic_values),
        )

        source_kinds = list(THREAD_SOURCE_KINDS)

        def expected_snapshot(started_at, completed_at):
            return {
                "observationWindow": {
                    "startedAt": started_at,
                    "completedAt": completed_at,
                },
                "rootThreadRef": "thread-1",
                "sourceKindsRequested": source_kinds,
                "pagination": {
                    "complete": True,
                    "pagesRead": 2,
                    "pages": [
                        {
                            "page": 1,
                            "requestCursorPresent": False,
                            "resultCount": 1,
                            "nextCursorPresent": True,
                            "sourceKinds": source_kinds,
                        },
                        {
                            "page": 2,
                            "requestCursorPresent": True,
                            "resultCount": 1,
                            "nextCursorPresent": False,
                            "sourceKinds": source_kinds,
                        },
                    ],
                },
                "threads": [
                    {
                        "threadRef": "thread-1",
                        "parentThreadRef": None,
                        "sessionRef": "session-1",
                        "source": "cli",
                        "status": {"type": "idle"},
                        "createdAt": 1777221000,
                        "updatedAt": 1777221060,
                    },
                    {
                        "threadRef": "thread-2",
                        "parentThreadRef": "thread-1",
                        "sessionRef": "session-2",
                        "source": {
                            "subAgent": {
                                "thread_spawn": {
                                    "depth": 1,
                                    "agent_path": "[redacted]",
                                }
                            }
                        },
                        "status": {
                            "type": "active",
                            "activeFlags": ["waitingOnApproval"],
                        },
                        "createdAt": 1777221010,
                        "updatedAt": 1777221070,
                    },
                    {
                        "threadRef": "thread-3",
                        "parentThreadRef": "thread-2",
                        "sessionRef": "session-3",
                        "source": {"subAgent": "review"},
                        "status": {"type": "notLoaded"},
                        "createdAt": 1777221020,
                        "updatedAt": 1777221080,
                    },
                ],
            }

        initial = expected_snapshot("initial-start", "initial-end")
        revalidation = expected_snapshot("again-start", "again-end")
        self.assertEqual(evidence["snapshots"], [initial])
        self.assertEqual(
            evidence["subscriptionEvidence"],
            {
                "requested": True,
                "method": "thread/resume",
                "subscribedThreadRefs": ["thread-1", "thread-2", "thread-3"],
                "attemptedCount": 3,
                "acknowledgedCount": 3,
                "unacknowledgedAttemptCount": 0,
                "mayHaveChanged": False,
                "exactTreeRevalidatedAfterResume": True,
                "revalidationSnapshot": revalidation,
            },
        )
        self.assertEqual(
            evidence["notificationEvidence"],
            {
                "delivery": "threadResumeSubscription",
                "explicitSubscriptionRpcUsed": False,
                "threadResumeUsedToSubscribe": True,
                "waitSeconds": 1.0,
                "statusChanged": [
                    {
                        "receivedAt": "event-time",
                        "threadRef": "thread-2",
                        "status": {
                            "type": "active",
                            "activeFlags": ["waitingOnUserInput"],
                        },
                        "belongsToObservedTree": True,
                    }
                ],
                "ignoredServerMessageCount": 0,
                "unrelatedThreadStatusChangedCount": 1,
            },
        )
        self.assertEqual(
            evidence["redaction"],
            {
                "threadPreviewTurnsAndOutputFieldsEmitted": False,
                "sensitiveSourcePaths": "redacted",
                "nativeStatusFieldsEmitted": ["type", "activeFlags"],
                "protocolDerivedErrorTextEmitted": False,
                "socketPathEmitted": False,
                "nativeIdentifiers": "runLocalPseudonyms",
                "paginationCursors": "presenceOnly",
                "unrelatedThreadStatusDetailsEmitted": False,
            },
        )
        self.assertEqual(
            {
                key: evidence[key]
                for key in (
                    "schemaVersion",
                    "probe",
                    "captureMode",
                    "readOnly",
                    "conversationHistoryMutated",
                    "runtimeLoadedOrSubscriptionStateChanged",
                    "requirementsObserved",
                    "semanticInferences",
                )
            },
            {
                "schemaVersion": 2,
                "probe": "switchstand-stage-a",
                "captureMode": "oneShotSnapshot",
                "readOnly": False,
                "conversationHistoryMutated": False,
                "runtimeLoadedOrSubscriptionStateChanged": True,
                "requirementsObserved": {
                    "exactRootThread": True,
                    "spawnedDescendant": True,
                    "completeParentThreadIdLineage": True,
                    "allSourceKindsRequestedOnEveryPage": True,
                    "paginationExhausted": True,
                    "nativeSourceStatusAndTimestampsForEveryThread": True,
                    "localObservationWindow": True,
                    "threadStatusChangedNotificationForObservedTree": True,
                },
                "semanticInferences": {
                    "idleMeansDone": False,
                    "silenceMeansStale": False,
                },
            },
        )
        self.assertEqual(
            set(evidence),
            {
                "schemaVersion",
                "probe",
                "captureMode",
                "readOnly",
                "conversationHistoryMutated",
                "runtimeLoadedOrSubscriptionStateChanged",
                "snapshots",
                "subscriptionEvidence",
                "notificationEvidence",
                "requirementsObserved",
                "semanticInferences",
                "redaction",
            },
        )
        self.assertEqual(client.resumes, [root_id, child_id, grandchild_id])
        serialized = json.dumps(evidence, sort_keys=True)
        forbidden = [*sentinels.values(), grandchild_id, "SENTINEL-SESSION-c53d6c"]
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, serialized)


if __name__ == "__main__":
    unittest.main()
