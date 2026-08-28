"""Closed, dependency-free types for the existing native facade boundary."""
from __future__ import annotations

from typing import Literal, NotRequired, Protocol, TypedDict


NativeStatus = Literal["active", "idle", "systemError", "notLoaded"]
NativeTurnStatus = Literal[
    "unknown", "none", "inProgress", "completed", "failed", "interrupted"
]
NativeSourceKind = Literal[
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
]


class NativeObservation(TypedDict):
    connected: bool
    available: bool
    historical: bool
    errorCode: str | None
    completedAt: float | None
    passAgeSeconds: float | None
    kind: Literal["completed_multi_request_pass"]


class NativeAgent(TypedDict):
    agentRef: str
    label: str
    parentRef: str | None
    depth: int
    sourceKind: NativeSourceKind
    sourceDetail: str | None
    createdAt: float
    updatedAt: float
    status: NativeStatus
    turnStatus: NativeTurnStatus
    activeFlags: list[str]
    activeObservedSeconds: float
    updatedAgeSeconds: float


NativeTrailValue = str | int | float | list[str] | None


NativeTrailChange = TypedDict(
    "NativeTrailChange",
    {"from": NativeTrailValue, "to": NativeTrailValue},
)
NativePresenceChange = TypedDict(
    "NativePresenceChange",
    {
        "from": Literal["absent", "present"],
        "to": Literal["absent", "present"],
    },
)


class NativeTrailChanges(TypedDict, total=False):
    presence: NativePresenceChange
    agentRef: NativeTrailChange
    parentRef: NativeTrailChange
    depth: NativeTrailChange
    sourceKind: NativeTrailChange
    sourceDetail: NativeTrailChange
    createdAt: NativeTrailChange
    updatedAt: NativeTrailChange
    status: NativeTrailChange
    activeFlags: NativeTrailChange


class NativeTrailEntry(TypedDict):
    observedAt: float
    agentRef: str
    changes: NativeTrailChanges


class NativeBoardSnapshot(TypedDict):
    mode: Literal["native"]
    observation: NativeObservation
    agents: list[NativeAgent]
    trail: list[NativeTrailEntry]
    trailLimit: int
    disclosure: str


NativeEvidenceEventKind = Literal[
    "observation",
    "selection",
    "input",
    "stop_prepare",
    "stop_commit",
    "stop_status",
    "focus_invariant",
    "refresh",
    "stop_cancel",
]
NativeEvidenceOutcome = Literal[
    "connected",
    "disconnected",
    "selected",
    "invalid_agent_ref",
    "app_server_disconnected",
    "observation_stale",
    "agent_not_present",
    "unavailable",
    "sent_start",
    "sent_steer",
    "not_sent",
    "prepared",
    "not_sent_target_unavailable",
    "not_sent_capacity",
    "requested",
    "rejected",
    "unknown",
    "not_sent_confirmation_unavailable",
    "confirmed",
    "not_confirmed",
    "not_sent_operation_unavailable",
    "failed",
    "coalesced",
]


class NativeEvidenceEvent(TypedDict):
    observedAt: float
    kind: NativeEvidenceEventKind
    outcome: NativeEvidenceOutcome
    durationMs: int | None
    passAgeSeconds: float | None


class NativeStatusCounts(TypedDict):
    active: int
    idle: int
    systemError: int
    notLoaded: int


class NativeTurnStatusCounts(TypedDict):
    unknown: int
    none: int
    inProgress: int
    completed: int
    failed: int
    interrupted: int


NativeEvidenceSummary = TypedDict(
    "NativeEvidenceSummary",
    {
        "available": bool, "storage": Literal["bounded_process_memory"], "capacity": int,
        "retainedCount": int, "droppedCount": int, "duplicateCount": int,
        "refreshCount": int, "coalescedRefreshCount": int,
        "observationConnected": bool | None, "passAgeSeconds": float | None,
        "agentCount": int, "statusCounts": NativeStatusCounts,
        "turnStatusCounts": NativeTurnStatusCounts,
        "lastObservedActivityAgeSeconds": float | None,
        "recentEvents": list[NativeEvidenceEvent], "disclosure": str,
    },
)


class NativeWorkbenchSnapshot(NativeBoardSnapshot):
    evidence: NativeEvidenceSummary


class NativeEvidenceRequest(TypedDict):
    version: Literal["native-evidence-v1"]
    event: Literal["focus_preservation_failed", "refresh_coalesced", "stop_cancelled"]


class NativeEvidenceRecorded(TypedDict):
    code: Literal["evidence_recorded"]
    outcome: Literal["recorded"]


class NativeEvidenceUnavailable(TypedDict):
    code: Literal["evidence_unavailable"]
    outcome: Literal["not_recorded"]


NativeEvidenceResult = NativeEvidenceRecorded | NativeEvidenceUnavailable


class NativeSelectionPair(TypedDict):
    observationRunRef: str
    agentRef: str


class NativeSelectionSuccess(TypedDict):
    version: Literal["native-selection-v1"]
    observationRunRef: str
    agentRef: str
    connected: Literal[True]
    present: Literal[True]
    name: NotRequired[str]
    agentNickname: NotRequired[str]


NativeSelectionErrorCode = Literal[
    "INVALID_AGENT_REF",
    "APP_SERVER_DISCONNECTED",
    "OBSERVATION_STALE",
    "AGENT_NOT_PRESENT",
]


class NativeSelectionError(TypedDict):
    code: NativeSelectionErrorCode
    message: str


NativeSelectionSnapshot = NativeSelectionSuccess | NativeSelectionError


class NativeBrowserSelectionResult(TypedDict):
    selection: NativeSelectionPair | None
    snapshot: NativeSelectionSnapshot


class NativeInputRequest(TypedDict):
    version: Literal["native-input-v1"]
    observationRunRef: str
    agentRef: str
    text: str


class NativeInputNotSent(TypedDict):
    code: Literal["input_unavailable"]
    outcome: Literal["not_sent"]


class NativeInputSent(TypedDict):
    code: Literal["input_sent"]
    outcome: Literal["sent"]
    mode: Literal["start", "steer"]


NativeInputResult = NativeInputNotSent | NativeInputSent


class NativeStopPrepareFailure(TypedDict):
    code: Literal["target_unavailable", "stop_capacity"]
    outcome: Literal["not_sent"]


class NativeStopPrepared(TypedDict):
    code: Literal["prepared"]
    agentRef: str
    confirmationRef: str


NativeStopPrepareResult = NativeStopPrepareFailure | NativeStopPrepared
NativeStopCommitOutcome = Literal[
    "not_sent",
    "rejected",
    "requested",
    "unknown",
]
NativeStopStatusOutcome = Literal[
    "not_sent",
    "rejected",
    "requested",
    "confirmed",
    "not_confirmed",
    "unknown",
]


class NativeStopCommitUnavailable(TypedDict):
    code: Literal["confirmation_unavailable"]
    outcome: Literal["not_sent"]


class NativeStopCommitted(TypedDict):
    code: Literal["stop_result"]
    operationRef: str
    outcome: NativeStopCommitOutcome


NativeStopCommitResult = NativeStopCommitUnavailable | NativeStopCommitted


class NativeStopStatusUnavailable(TypedDict):
    code: Literal["operation_unavailable"]
    outcome: Literal["unknown"]


class NativeStopPending(TypedDict):
    code: Literal["stop_pending"]
    operationRef: str
    outcome: Literal["unknown"]


class NativeStopObserved(TypedDict):
    code: Literal["stop_result"]
    operationRef: str
    outcome: NativeStopStatusOutcome


NativeStopStatusResult = NativeStopStatusUnavailable | NativeStopPending | NativeStopObserved


class NativeBoardSnapshotPort(Protocol):
    def snapshot(self) -> NativeBoardSnapshot: ...


class NativeBrowserSelectionPort(Protocol):
    def browser_selection(
        self,
        agent_ref: object,
        *,
        now: object,
        maximum_observation_age_seconds: object,
    ) -> NativeBrowserSelectionResult: ...


class NativeInputPort(Protocol):
    def send(self, request: object) -> NativeInputResult: ...


class NativeStopPort(Protocol):
    def prepare_stop(self, agent_ref: object) -> NativeStopPrepareResult: ...
    def commit_stop(self, confirmation_ref: object) -> NativeStopCommitResult: ...
    def stop_status(self, operation_ref: object) -> NativeStopStatusResult: ...
