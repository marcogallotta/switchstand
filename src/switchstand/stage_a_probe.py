"""Transport orchestration and CLI for the native Codex agent-tree probe."""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from typing import Any, Callable, Mapping, Sequence

from .agent_tree import AgentTreeAdapter, AgentTreeEvidenceError
from .app_server import CodexAppServer
from .stage_a_evidence import (
    SCHEMA_VERSION,
    EvidenceIdentifiers,
    ProbeEvidenceError,
    ProbeExecutionError,
    collect_notification,
    snapshot,
    utc_now,
)


_SAFE_ERROR_PHASES = frozenset(
    {
        "root_read",
        "descendant_list",
        "lineage_validation",
        "status_validation",
        "timestamp_validation",
        "source_validation",
        "notification_validation",
        "notification_wait",
        "subscription_setup",
        "subscription_revalidation",
        "transport",
        "protocol_validation",
        "probe_runtime",
    }
)


def _subscription_disclosure(
    *, requested: bool, attempted_count: int, acknowledged_count: int
) -> dict[str, Any]:
    unacknowledged = attempted_count - acknowledged_count
    if acknowledged_count:
        runtime_changed: bool | str = True
    elif attempted_count:
        runtime_changed = "unknown"
    else:
        runtime_changed = False
    return {
        "readOnly": attempted_count == 0,
        "conversationHistoryMutated": False,
        "runtimeLoadedOrSubscriptionStateChanged": runtime_changed,
        "subscriptionEvidence": {
            "requested": requested,
            "method": "thread/resume" if requested else None,
            "attemptedCount": attempted_count,
            "acknowledgedCount": acknowledged_count,
            "unacknowledgedAttemptCount": unacknowledged,
            "mayHaveChanged": unacknowledged > 0,
        },
    }


def _subscription_failure(
    exc: Exception, *, attempted_count: int, acknowledged_count: int
) -> ProbeExecutionError:
    disclosure = _subscription_disclosure(
        requested=True,
        attempted_count=attempted_count,
        acknowledged_count=acknowledged_count,
    )
    if isinstance(exc, ProbeEvidenceError):
        return ProbeExecutionError(
            exc.code, str(exc), 4, disclosure, phase=exc.phase
        )
    if isinstance(exc, AgentTreeEvidenceError):
        return ProbeExecutionError(
            exc.code,
            str(exc),
            4,
            disclosure,
            phase=exc.phase,
        )
    if isinstance(exc, (OSError, socket.timeout)):
        return ProbeExecutionError(
            "transport_failure",
            "App Server transport failed",
            3,
            disclosure,
            phase="transport",
        )
    if isinstance(exc, RuntimeError):
        if "WebSocket" in str(exc) or "app-server closed" in str(exc):
            return ProbeExecutionError(
                "transport_failure",
                "App Server connection failed or closed (RuntimeError)",
                3,
                disclosure,
                phase="transport",
            )
        return ProbeExecutionError(
            "evidence_unavailable",
            "App Server request failed or returned unusable protocol evidence (RuntimeError)",
            4,
            disclosure,
            phase="protocol_validation",
        )
    return ProbeExecutionError(
        "subscription_failed",
        "notification subscription could not be established or validated",
        4,
        disclosure,
        phase="subscription_setup",
    )


def collect_evidence(
    client: Any,
    root_thread_id: str,
    *,
    poll_count: int = 1,
    poll_interval_seconds: float = 1.0,
    notification_wait_seconds: float = 0.0,
    require_status_notification: bool = False,
    subscribe_status_notifications: bool = False,
    now: Callable[[], str] = utc_now,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Collect bounded snapshots, optionally loading exact threads to subscribe."""
    if notification_wait_seconds and not subscribe_status_notifications:
        raise ValueError("notification waiting requires explicit subscription opt-in")
    if require_status_notification and not subscribe_status_notifications:
        raise ValueError("required notifications require explicit subscription opt-in")
    adapter = AgentTreeAdapter(client)
    identifiers = EvidenceIdentifiers()
    snapshots: list[dict[str, Any]] = []
    snapshot_exact_threads: list[tuple[dict[str, Any], ...]] = []
    notifications: list[dict[str, Any]] = []
    ignored_notification_count = 0
    unrelated_status_count = 0
    subscribed_thread_ids: list[str] = []
    subscription_revalidation: dict[str, Any] | None = None
    subscription_attempted_count = 0
    subscription_acknowledged_count = 0

    def drain() -> None:
        nonlocal ignored_notification_count, unrelated_status_count
        observed_ids = {
            thread["id"]
            for exact_threads in snapshot_exact_threads
            for thread in exact_threads
        }
        for message in client.drain_server_messages():
            event, unrelated = collect_notification(
                adapter,
                message,
                observed_ids,
                now,
                identifiers,
            )
            if event is None:
                if unrelated:
                    unrelated_status_count += 1
                else:
                    ignored_notification_count += 1
            else:
                notifications.append(event)

    for index in range(poll_count):
        if index:
            sleep(poll_interval_seconds)
        capture = snapshot(adapter, root_thread_id, now, identifiers)
        snapshots.append(capture.evidence)
        snapshot_exact_threads.append(capture.exact_threads)
        drain()

    if subscribe_status_notifications:
        try:
            exact_threads = snapshot_exact_threads[-1]
            subscribed_thread_ids = [thread["id"] for thread in exact_threads]
            expected_by_id = {thread["id"]: thread for thread in exact_threads}
            for thread_id in subscribed_thread_ids:
                subscription_attempted_count += 1
                adapter.resume_exact(thread_id)
                subscription_acknowledged_count += 1
            revalidation_capture = snapshot(adapter, root_thread_id, now, identifiers)
            subscription_revalidation = revalidation_capture.evidence
            revalidated_ids = [
                thread["id"] for thread in revalidation_capture.exact_threads
            ]
            if set(revalidated_ids) != set(subscribed_thread_ids):
                raise ProbeEvidenceError(
                    "tree_changed_during_subscription",
                    "the native tree changed while notification subscription was established",
                    phase="subscription_revalidation",
                )
            revalidated_by_id = {
                thread["id"]: thread for thread in revalidation_capture.exact_threads
            }
            for thread_id, expected in expected_by_id.items():
                revalidated = revalidated_by_id[thread_id]
                for field in ("parentThreadId", "sessionId", "source"):
                    if revalidated[field] != expected[field]:
                        raise ProbeEvidenceError(
                            "thread_changed_during_subscription",
                            "an exact thread changed while notification subscription was established",
                            phase="subscription_revalidation",
                        )
            drain()

            deadline = monotonic() + notification_wait_seconds
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                try:
                    message = client.next_server_message(timeout_seconds=remaining)
                except TimeoutError:
                    break
                observed_ids = {
                    thread["id"]
                    for exact_threads in snapshot_exact_threads
                    for thread in exact_threads
                }
                event, unrelated = collect_notification(
                    adapter,
                    message,
                    observed_ids,
                    now,
                    identifiers,
                )
                if event is None:
                    if unrelated:
                        unrelated_status_count += 1
                    else:
                        ignored_notification_count += 1
                else:
                    notifications.append(event)

            relevant_notifications = [
                event for event in notifications if event["belongsToObservedTree"]
            ]
            if require_status_notification and not relevant_notifications:
                raise ProbeEvidenceError(
                    "missing_status_notification",
                    "no status-change notification was received for the observed tree",
                    phase="notification_wait",
                )
        except ProbeExecutionError:
            raise
        except Exception as exc:
            raise _subscription_failure(
                exc,
                attempted_count=subscription_attempted_count,
                acknowledged_count=subscription_acknowledged_count,
            ) from exc
    else:
        relevant_notifications = [
                event for event in notifications if event["belongsToObservedTree"]
        ]

    return {
        "schemaVersion": SCHEMA_VERSION,
        "probe": "switchstand-stage-a",
        "captureMode": "oneShotSnapshot" if poll_count == 1 else "pollingSnapshots",
        "readOnly": not subscribe_status_notifications,
        "conversationHistoryMutated": False,
        "runtimeLoadedOrSubscriptionStateChanged": subscribe_status_notifications,
        "snapshots": snapshots,
        "subscriptionEvidence": {
            "requested": subscribe_status_notifications,
            "method": "thread/resume" if subscribe_status_notifications else None,
            "subscribedThreadRefs": [
                identifiers.thread_ref(thread_id) for thread_id in subscribed_thread_ids
            ],
            "attemptedCount": subscription_attempted_count,
            "acknowledgedCount": subscription_acknowledged_count,
            "unacknowledgedAttemptCount": (
                subscription_attempted_count - subscription_acknowledged_count
            ),
            "mayHaveChanged": False,
            "exactTreeRevalidatedAfterResume": subscription_revalidation is not None,
            "revalidationSnapshot": subscription_revalidation,
        },
        "notificationEvidence": {
            "delivery": (
                "threadResumeSubscription"
                if subscribe_status_notifications
                else "initializedAppServerConnection"
            ),
            "explicitSubscriptionRpcUsed": False,
            "threadResumeUsedToSubscribe": subscribe_status_notifications,
            "waitSeconds": notification_wait_seconds,
            "statusChanged": notifications,
            "ignoredServerMessageCount": ignored_notification_count,
            "unrelatedThreadStatusChangedCount": unrelated_status_count,
        },
        "requirementsObserved": {
            "exactRootThread": all(
                exact_threads[0]["id"] == root_thread_id
                for exact_threads in snapshot_exact_threads
            ),
            "spawnedDescendant": all(len(snapshot["threads"]) > 1 for snapshot in snapshots),
            "completeParentThreadIdLineage": True,
            "allSourceKindsRequestedOnEveryPage": True,
            "paginationExhausted": True,
            "nativeSourceStatusAndTimestampsForEveryThread": True,
            "localObservationWindow": True,
            "threadStatusChangedNotificationForObservedTree": bool(relevant_notifications),
        },
        "semanticInferences": {
            "idleMeansDone": False,
            "silenceMeansStale": False,
        },
        "redaction": {
            "threadPreviewTurnsAndOutputFieldsEmitted": False,
            "sensitiveSourcePaths": "redacted",
            "nativeStatusFieldsEmitted": ["type", "activeFlags"],
            "protocolDerivedErrorTextEmitted": False,
            "socketPathEmitted": False,
            "nativeIdentifiers": "runLocalPseudonyms",
            "paginationCursors": "presenceOnly",
            "unrelatedThreadStatusDetailsEmitted": False,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchstand-stage-a",
        description=(
            "Read one exact native Codex root tree and emit fail-closed Stage A JSON evidence. "
            "Default snapshot/polling mode never loads or mutates a thread."
        ),
    )
    parser.add_argument("--app-server-socket", required=True, help="Unix App Server socket path")
    parser.add_argument("--root-thread-id", required=True, help="exact native root thread id")
    parser.add_argument(
        "--poll-count", type=int, default=1, help="bounded complete snapshots to collect (default: 1)"
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=1.0,
        help="delay between complete snapshots (default: 1.0)",
    )
    parser.add_argument(
        "--notification-wait-seconds",
        type=float,
        default=0.0,
        help="bounded wait after snapshots for real App Server notifications (default: 0)",
    )
    parser.add_argument(
        "--subscribe-status-notifications",
        action="store_true",
        help=(
            "OPT IN: thread/resume the exact observed root and descendants, changing runtime "
            "loaded/subscribed state but not conversation history"
        ),
    )
    parser.add_argument(
        "--require-status-notification",
        action="store_true",
        help="exit nonzero unless thread/status/changed is received for the observed tree",
    )
    return parser


def _failure(
    code: str,
    message: str,
    *,
    phase: str,
    side_effects: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    safe_phase = phase if phase in _SAFE_ERROR_PHASES else "probe_runtime"
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "probe": "switchstand-stage-a",
        "ok": False,
        "error": {"code": code, "message": message, "phase": safe_phase},
    }
    result.update(
        dict(side_effects)
        if side_effects is not None
        else _subscription_disclosure(
            requested=False, attempted_count=0, acknowledged_count=0
        )
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.poll_count < 1:
        parser.error("--poll-count must be at least 1")
    if args.poll_interval_seconds < 0:
        parser.error("--poll-interval-seconds cannot be negative")
    if args.notification_wait_seconds < 0:
        parser.error("--notification-wait-seconds cannot be negative")
    if args.notification_wait_seconds > 0 and not args.subscribe_status_notifications:
        parser.error(
            "--notification-wait-seconds requires --subscribe-status-notifications"
        )
    if args.subscribe_status_notifications and args.notification_wait_seconds <= 0:
        parser.error(
            "--subscribe-status-notifications requires --notification-wait-seconds > 0"
        )
    if args.require_status_notification and not args.subscribe_status_notifications:
        parser.error(
            "--require-status-notification requires --subscribe-status-notifications"
        )
    if args.require_status_notification and args.notification_wait_seconds <= 0:
        parser.error("--require-status-notification requires --notification-wait-seconds > 0")

    client = None
    no_effects = _subscription_disclosure(
        requested=args.subscribe_status_notifications,
        attempted_count=0,
        acknowledged_count=0,
    )
    try:
        client = CodexAppServer(args.app_server_socket, client_name="switchstand-stage-a")
        result = collect_evidence(
            client,
            args.root_thread_id,
            poll_count=args.poll_count,
            poll_interval_seconds=args.poll_interval_seconds,
            notification_wait_seconds=args.notification_wait_seconds,
            require_status_notification=args.require_status_notification,
            subscribe_status_notifications=args.subscribe_status_notifications,
        )
    except ProbeExecutionError as exc:
        result = _failure(
            exc.code, str(exc), phase=exc.phase, side_effects=exc.side_effects
        )
        print(json.dumps(result, sort_keys=True))
        return exc.exit_code
    except (OSError, socket.timeout):
        result = _failure(
            "transport_failure",
            "App Server transport failed",
            phase="transport",
            side_effects=no_effects,
        )
        print(json.dumps(result, sort_keys=True))
        return 3
    except ProbeEvidenceError as exc:
        result = _failure(
            exc.code, str(exc), phase=exc.phase, side_effects=no_effects
        )
        print(json.dumps(result, sort_keys=True))
        return 4
    except AgentTreeEvidenceError as exc:
        result = _failure(
            exc.code,
            str(exc),
            phase=exc.phase,
            side_effects=no_effects,
        )
        print(json.dumps(result, sort_keys=True))
        return 4
    except RuntimeError as exc:
        if "WebSocket" in str(exc) or "app-server closed" in str(exc):
            result = _failure(
                "transport_failure",
                "App Server connection failed or closed (RuntimeError)",
                phase="transport",
                side_effects=no_effects,
            )
            exit_code = 3
        else:
            result = _failure(
                "evidence_unavailable",
                "App Server request failed or returned unusable protocol evidence (RuntimeError)",
                phase="protocol_validation",
                side_effects=no_effects,
            )
            exit_code = 4
        print(json.dumps(result, sort_keys=True))
        return exit_code
    except ValueError:
        result = _failure(
            "evidence_unavailable",
            "protocol evidence was not valid",
            phase="protocol_validation",
            side_effects=no_effects,
        )
        print(json.dumps(result, sort_keys=True))
        return 4
    finally:
        if client is not None:
            try:
                client.close()
            except (OSError, ValueError):
                pass

    result["ok"] = True
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
