"""Legacy Codex adapter with exact phase receipts."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from .app_server import CodexAppServer
from .legacy_deadline import LegacyDeadline, PhaseResult
from .legacy_transport import close_legacy, open_legacy, request_legacy


MESSAGE_MARKER_PREFIX = "switchstand-message:"
MESSAGE_MARKER_PATTERN = re.compile(r"\[\[switchstand-message:[A-Za-z0-9._-]+\]\]")


def message_marker(message_id: str) -> str:
    return f"[[{MESSAGE_MARKER_PREFIX}{message_id}]]"


def submitted_message_text(message: Mapping[str, Any]) -> str:
    return f"{message['text']}\n\n{message_marker(str(message['id']))}"


def official_user_texts(item: Mapping[str, Any]) -> list[str] | None:
    if str(item.get("type") or "") != "userMessage":
        return []
    content = item.get("content")
    if not isinstance(content, list):
        return None
    texts: list[str] = []
    for part in content:
        if not isinstance(part, Mapping):
            return None
        part_type = str(part.get("type") or "")
        if part_type not in {"text", "image", "localImage"}:
            return None
        if part_type == "text":
            text = part.get("text")
            if not isinstance(text, str):
                return None
            texts.append(text)
    return texts


def message_text(item: Mapping[str, Any]) -> str:
    direct = item.get("text")
    if isinstance(direct, str):
        return direct
    return "\n".join(
        str(part["text"])
        for part in item.get("content") or []
        if isinstance(part, Mapping) and isinstance(part.get("text"), str)
    )


@dataclass(frozen=True)
class AdapterReceipt:
    phase: PhaseResult
    close_disposition: str
    name_disposition: str | None = None


class CodexAdapter:
    """Explicit adapter from Switchstand operations to Codex app-server calls."""

    def __init__(self, socket_path: Path | str, *, cwd: Path | str) -> None:
        self.socket_path = Path(socket_path)
        self.cwd = Path(cwd).resolve()

    def _client(self) -> CodexAppServer:
        return CodexAppServer(self.socket_path)

    def _thread_start_params(self, role: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "cwd": str(self.cwd),
            "ephemeral": False,
            "serviceName": "Switchstand",
            "developerInstructions": (
                "You are one directly addressable Switchstand logical role. Work only from "
                "the durable context below and the operator's direct messages. Do not "
                "delegate, create tasks, mutate remote systems, release, deploy, or infer "
                "authority outside a direct message. State uncertainty plainly.\n\n"
                "DURABLE ROLE CONTEXT\n" + json.dumps(context, sort_keys=True)
            ),
        }

    def create_attempt_bounded(
        self,
        *,
        role: Mapping[str, Any],
        context: Mapping[str, Any],
        deadline: LegacyDeadline,
        on_thread_id: Callable[[str], None],
    ) -> AdapterReceipt:
        setup = open_legacy(self.socket_path, deadline)
        if setup.client is None:
            return AdapterReceipt(setup.phase, "not_sent")
        client = setup.client
        name_disposition: str | None = None
        try:
            phase = request_legacy(client, "thread/start", self._thread_start_params(role, context), deadline)
            if phase.disposition == "acknowledged":
                thread = phase.result.get("thread") if phase.result else None
                raw_thread_id = thread.get("id") if isinstance(thread, Mapping) else None
                thread_id = raw_thread_id if type(raw_thread_id) is str else ""
                if not thread_id:
                    phase = PhaseResult("ambiguous", "thread/start", code="missing_exact_acknowledgement")
                else:
                    on_thread_id(thread_id)
                    if deadline.expired():
                        name_disposition = "not_sent"
                    else:
                        named = request_legacy(
                            client,
                            "thread/name/set",
                            {"threadId": thread_id, "name": f"Switchstand — {role['name']}"},
                            deadline,
                        )
                        name_disposition = named.disposition
            return AdapterReceipt(phase, close_legacy(client, deadline), name_disposition)
        except BaseException:
            close_legacy(client, deadline)
            raise

    def _target_bounded(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        thread_id: str,
        deadline: LegacyDeadline,
    ) -> AdapterReceipt:
        setup = open_legacy(self.socket_path, deadline)
        if setup.client is None:
            return AdapterReceipt(setup.phase, "not_sent")
        client = setup.client
        try:
            resumed = request_legacy(client, "thread/resume", {"threadId": thread_id}, deadline)
            resumed_thread = resumed.result.get("thread") if resumed.result else None
            resumed_id = resumed_thread.get("id") if isinstance(resumed_thread, Mapping) else None
            if resumed.disposition == "acknowledged" and (
                type(resumed_id) is not str or resumed_id != thread_id
            ):
                phase = PhaseResult(
                    "ambiguous", "thread/resume", code="missing_exact_acknowledgement"
                )
            elif resumed.disposition != "acknowledged":
                code = "setup_rejected" if resumed.disposition == "rejected" else resumed.code
                phase = PhaseResult(resumed.disposition, "thread/resume", code=code)
            else:
                phase = request_legacy(client, method, params, deadline)
            return AdapterReceipt(phase, close_legacy(client, deadline))
        except BaseException:
            close_legacy(client, deadline)
            raise

    def start_message_bounded(
        self,
        *,
        thread_id: str,
        message: Mapping[str, Any],
        deadline: LegacyDeadline,
    ) -> AdapterReceipt:
        return self._target_bounded(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": submitted_message_text(message)}],
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [str(self.cwd)]},
            },
            thread_id=thread_id,
            deadline=deadline,
        )

    def interrupt_bounded(self, *, thread_id: str, turn_id: str, deadline: LegacyDeadline) -> AdapterReceipt:
        return self._target_bounded(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
            thread_id=thread_id,
            deadline=deadline,
        )

    def inspect_turn_bounded(
        self, *, thread_id: str, turn_id: str, deadline: LegacyDeadline
    ) -> AdapterReceipt:
        receipt = self._read_bounded(thread_id, deadline)
        if receipt.phase.disposition != "acknowledged":
            return receipt
        response = receipt.phase.result or {}
        raw_thread = response.get("thread")
        thread = raw_thread if isinstance(raw_thread, Mapping) else {}
        for turn in thread.get("turns") or []:
            if isinstance(turn, Mapping) and type(turn.get("id")) is str and turn.get("id") == turn_id:
                return AdapterReceipt(
                    PhaseResult("acknowledged", "thread/read", self._turn_result(turn)),
                    receipt.close_disposition,
                )
        return AdapterReceipt(
            PhaseResult("acknowledged", "thread/read", {"status": "unknown", "output": None}),
            receipt.close_disposition,
        )

    def inspect_message_bounded(
        self, *, thread_id: str, message_id: str, deadline: LegacyDeadline
    ) -> AdapterReceipt:
        receipt = self._read_bounded(thread_id, deadline)
        if receipt.phase.disposition != "acknowledged":
            return receipt
        return AdapterReceipt(
            PhaseResult(
                "acknowledged",
                "thread/read",
                self._inspect_message_response(receipt.phase.result or {}, message_id),
            ),
            receipt.close_disposition,
        )

    def _read_bounded(self, thread_id: str, deadline: LegacyDeadline) -> AdapterReceipt:
        setup = open_legacy(self.socket_path, deadline)
        if setup.client is None:
            return AdapterReceipt(setup.phase, "not_sent")
        client = setup.client
        try:
            phase = request_legacy(
                client,
                "thread/read",
                {"threadId": thread_id, "includeTurns": True},
                deadline,
            )
            if phase.disposition == "acknowledged":
                raw_thread = phase.result.get("thread") if phase.result else None
                observed_id = raw_thread.get("id") if isinstance(raw_thread, Mapping) else None
                if type(observed_id) is not str or observed_id != thread_id:
                    phase = PhaseResult(
                        "ambiguous", "thread/read", code="missing_exact_acknowledgement"
                    )
            return AdapterReceipt(phase, close_legacy(client, deadline))
        except BaseException:
            close_legacy(client, deadline)
            raise

    def create_attempt(self, *, role: Mapping[str, Any], context: Mapping[str, Any]) -> str:
        client = self._client()
        try:
            response = client.thread_start(self._thread_start_params(role, context))
            raw_thread = response.get("thread")
            thread = raw_thread if isinstance(raw_thread, Mapping) else {}
            raw_thread_id = thread.get("id")
            thread_id = raw_thread_id if type(raw_thread_id) is str else ""
            if not thread_id:
                raise RuntimeError("Codex thread/start returned no thread id")
            client.thread_name_set(thread_id, f"Switchstand — {role['name']}")
            return thread_id
        finally:
            client.close()

    def start_message(self, *, thread_id: str, message: Mapping[str, Any]) -> str:
        client = self._client()
        try:
            client.thread_resume(thread_id)
            response = client.turn_start_text(
                thread_id,
                submitted_message_text(message),
                approval_policy="never",
                sandbox_policy={"type": "workspaceWrite", "writableRoots": [str(self.cwd)]},
            )
            raw_turn = response.get("turn")
            turn = raw_turn if isinstance(raw_turn, Mapping) else {}
            raw_turn_id = turn.get("id")
            turn_id = raw_turn_id if type(raw_turn_id) is str else ""
            if not turn_id:
                raise RuntimeError("Codex turn/start returned no turn id")
            return turn_id
        finally:
            client.close()

    def interrupt(self, *, thread_id: str, turn_id: str) -> None:
        client = self._client()
        try:
            client.thread_resume(thread_id)
            client.turn_interrupt(thread_id, turn_id)
        finally:
            client.close()

    def _read(self, thread_id: str) -> Mapping[str, Any]:
        client = self._client()
        try:
            return client.thread_read(thread_id, include_turns=True)
        finally:
            client.close()

    @staticmethod
    def _turn_result(turn: Mapping[str, Any]) -> Mapping[str, Any]:
        markers: set[str] = set()
        for item in turn.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            texts = official_user_texts(item)
            if texts is not None:
                for text in texts:
                    markers.update(MESSAGE_MARKER_PATTERN.findall(text))
        messages = [
            message_text(item)
            for item in turn.get("items") or []
            if isinstance(item, Mapping)
            and str(item.get("type") or "") in {"agentMessage", "agent_message"}
            and str(item.get("phase") or "final_answer") == "final_answer"
        ]
        output = messages[-1] if messages else None
        if output is not None:
            for marker in markers:
                output = output.replace(marker, "")
            output = output.strip()
        return {"status": str(turn.get("status") or "unknown"), "output": output}

    @classmethod
    def _inspect_message_response(cls, response: Mapping[str, Any], message_id: str) -> Mapping[str, Any]:
        raw_thread = response.get("thread")
        thread = raw_thread if isinstance(raw_thread, Mapping) else {}
        turns = thread.get("turns")
        complete_history = isinstance(turns, list)
        for turn in turns if isinstance(turns, list) else []:
            if not isinstance(turn, Mapping):
                complete_history = False
                continue
            turn_id, items = turn.get("id"), turn.get("items")
            if type(turn_id) is not str or not turn_id or type(turn.get("status")) is not str:
                complete_history = False
            if not isinstance(items, list):
                complete_history = False
                continue
            for item in items:
                if not isinstance(item, Mapping):
                    complete_history = False
                    continue
                if type(item.get("id")) is not str or not item.get("id") or type(item.get("type")) is not str:
                    complete_history = False
                texts = official_user_texts(item)
                if texts is None:
                    complete_history = False
                    continue
                if any(message_marker(message_id) in text for text in texts) and type(turn_id) is str and turn_id:
                    return {"found": True, "turn_id": turn_id, **cls._turn_result(turn)}
        raw_status = thread.get("status")
        status = raw_status if isinstance(raw_status, Mapping) else {}
        status_type = str(status.get("type") or "unknown")
        return {
            "found": False,
            "absence_proven": status_type == "idle" and complete_history,
            "thread_status": status_type,
            "history_complete": complete_history,
        }

    def inspect_turn(self, *, thread_id: str, turn_id: str) -> Mapping[str, Any]:
        response = self._read(thread_id)
        raw_thread = response.get("thread")
        thread = raw_thread if isinstance(raw_thread, Mapping) else {}
        for turn in thread.get("turns") or []:
            if isinstance(turn, Mapping) and type(turn.get("id")) is str and turn.get("id") == turn_id:
                return self._turn_result(turn)
        return {"status": "unknown", "output": None}

    def inspect_message(self, *, thread_id: str, message_id: str) -> Mapping[str, Any]:
        return self._inspect_message_response(self._read(thread_id), message_id)
