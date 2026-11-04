"""Real delivery observation stays read-only; local reports cannot send mail."""
from uuid import uuid4

import httpx
from fastmcp import FastMCP
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.agent_mailboxes import AgentMailboxState
from switchstand.chatgpt_alerts import HostReporter, plan
from switchstand.chatgpt_wake import PATH, install_browser_wake
from switchstand.grant_state import GrantState
from switchstand.messages import MessageState, message_deliveries, messages


async def test_local_observation_and_reports(state_database_schema, caplog):
    engine = create_async_engine(state_database_schema)
    state = AgentMailboxState(engine)
    receiver = (await state.register_agent(f"Browser {uuid4()}", "owner", str(uuid4()))).mailbox
    other = (await state.register_agent(f"Other {uuid4()}", "owner", str(uuid4()))).mailbox
    assert receiver and other
    deliveries = [uuid4() for _ in range(51)]
    async with engine.begin() as connection:
        for identity in deliveries:
            await connection.execute(messages.insert().values(
                sender_work_id=other.endpoint_id, message_id=identity, route_ref="ordinary",
                kind="request", payload={"text": "private body"}, digest=str(identity)))
            await connection.execute(message_deliveries.insert().values(
                delivery_id=identity, sender_work_id=other.endpoint_id, message_id=identity,
                recipient_work_id=receiver.endpoint_id, recipient_grant_version=1,
                state="AVAILABLE"))
        before = (await connection.execute(select(message_deliveries))).all()
    reader = create_async_engine(state_database_schema,
        connect_args={"options": "-c default_transaction_read_only=on"})
    async with reader.connect() as connection:
        assert (await connection.execute(text("SHOW transaction_read_only"))).scalar_one() == "on"
    server = FastMCP("browser-test")
    install_browser_wake(server, MessageState(reader, GrantState(reader)))
    app = server.http_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            observed = await client.get(PATH + "/poll", params={"name": receiver.name})
            assert observed.status_code == 200
            value = observed.json()
            assert set(value["delivery_ids"]) == set(map(str, deliveries))
            assert value["endpoint"] == str(receiver.endpoint_id)
            assert value["generation"] == receiver.generation
            assert "private body" not in observed.text
            assert "access-control-allow-origin" not in observed.headers
            empty = await client.get(PATH + "/poll", params={"name": other.name})
            assert empty.json()["delivery_ids"] == []
            assert (await client.get(PATH + "/poll", params={"name": "missing"})).status_code == 403
            assert (await client.get(PATH + "/poll", params={"name": receiver.name, "ack": "1"})).status_code == 400
            token = (await client.get(PATH + "/config")).json()["report_token"]
            payload = {"name": receiver.name, "delivery_ids": [str(deliveries[0])],
                       "outcome": "bound-tab-missing"}
            assert (await client.post(PATH + "/report", json=payload)).status_code == 403
            headers = {"Authorization": "Bearer " + token}
            assert (await client.post(PATH + "/report", content="plain", headers=headers)).status_code == 415
            receipt = await client.post(PATH + "/report", json=payload, headers=headers)
            assert receipt.json() == {"status": "logged", "escalation": "unconfigured"}
            assert "bound-tab-missing" in caplog.text
            assert (await client.post(PATH + "/report", json={**payload, "delivery_ids": [12]}, headers=headers)).status_code == 400
            assert (await client.get(PATH + "/config", headers={"Host": "public.example"})).status_code == 403
        remote = httpx.ASGITransport(app=app, client=("192.0.2.1", 1234))
        async with httpx.AsyncClient(transport=remote, base_url="http://127.0.0.1") as client:
            assert (await client.get(PATH + "/config")).status_code == 403
        async with engine.connect() as connection:
            assert (await connection.execute(select(message_deliveries))).all() == before
    finally:
        await reader.dispose()
        await engine.dispose()


async def test_configured_reporter_without_incident_reports_actual_configuration(state_database_schema):
    engine = create_async_engine(state_database_schema)
    try:
        async with engine.connect() as connection:
            before = (await connection.execute(select(message_deliveries))).all()
        state = MessageState(engine, GrantState(engine))
        server = FastMCP("configured-browser-test")
        install_browser_wake(server, state, HostReporter(plan("a" * 40), state))
        transport = httpx.ASGITransport(app=server.http_app(), client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            config = await client.get(PATH + "/config")
            value = config.json()
            assert value["escalation"] == "configured"
            assert value["recipient"] == "browser extension"
            assert config.headers["cache-control"] == "no-store"
            assert "access-control-allow-origin" not in config.headers
            payload = {"name": "fixture", "delivery_ids": [], "outcome": "bound-tab-missing"}
            assert (await client.post(PATH + "/report", json=payload)).status_code == 403
            response = await client.post(PATH + "/report", json=payload,
                headers={"Authorization": "Bearer " + value["report_token"]})
            assert response.json() == {"status": "logged", "escalation": "configured"}
            async with engine.connect() as connection:
                assert (await connection.execute(select(message_deliveries))).all() == before
    finally:
        await engine.dispose()


async def test_diagnostics_are_typed_bounded_and_never_escalate(caplog):
    from copy import deepcopy
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    reporter = SimpleNamespace(recipient="browser extension", send=AsyncMock())
    server = FastMCP("diagnostic-test")
    install_browser_wake(server, None, reporter)
    transport = httpx.ASGITransport(app=server.http_app(), client=("127.0.0.1", 1234))
    identity, now = str(uuid4()), "2026-11-04T20:00:00Z"
    diagnostic = {"release": identity, "boot": identity, "legacy_halt": False,
        "attempt": {"id": identity, "boot": identity, "release": identity,
            "endpoint": identity, "generation": 1, "delivery_ids": [identity],
            "started": now, "finished": now, "outcome": "attempt-outcome-unknown",
            "reason": "dom-exception", "facts": {"editorFound": True}, "exception": "TypeError"},
        "pair": {"id": identity, "boot": identity, "release": identity, "at": now,
            "result": "refused", "reason": "unresolved-attempt", "attempt": identity}}
    payload = {"name": "fixture", "delivery_ids": [], "outcome": "attempt-outcome-unknown",
               "diagnostics": diagnostic}
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        token = (await client.get(PATH + "/config")).json()["report_token"]
        headers = {"Authorization": "Bearer " + token}
        receipt = await client.post(PATH + "/report", json=payload, headers=headers)
        assert receipt.json() == {"status": "logged"}
        assert "dom-exception" in caplog.text and "TypeError" in caplog.text
        fenced = deepcopy(payload)
        fenced["diagnostics"]["block"] = "retry-evidence-not-current"
        fenced["diagnostics"]["pair"]["reason"] = "stop-requested"
        assert (await client.post(PATH + "/report", json=fenced, headers=headers)).json() == {"status": "logged"}
        for selection in ["missing-form", "no-explicit", "all-hidden", "ambiguous", "unique"]:
            selected = deepcopy(payload)
            selected["diagnostics"]["attempt"]["facts"]["controlSelection"] = selection
            assert (await client.post(PATH + "/report", json=selected, headers=headers)).json() == {"status": "logged"}
        invalid_selection = deepcopy(payload)
        invalid_selection["diagnostics"]["attempt"]["facts"]["controlSelection"] = "private draft sentinel"
        assert (await client.post(PATH + "/report", json=invalid_selection, headers=headers)).status_code == 400
        current = deepcopy(payload)
        current["diagnostics"]["current_control"] = {"release": identity, "boot": identity, "reason": "unique"}
        assert (await client.post(PATH + "/report", json=current, headers=headers)).json() == {"status": "logged"}
        current["diagnostics"]["current_control"]["reason"] = "private draft sentinel"
        assert (await client.post(PATH + "/report", json=current, headers=headers)).status_code == 400
        for source in ["explicit", "generic-submit", "private draft sentinel"]:
            sourced = deepcopy(payload)
            sourced["diagnostics"]["attempt"]["facts"]["controlSource"] = source
            sourced["diagnostics"]["current_control"] = {"release": identity, "boot": identity, "reason": "unique", "controlSource": source}
            assert (await client.post(PATH + "/report", json=sourced, headers=headers)).status_code == (400 if source == "private draft sentinel" else 200)
        legacy = {**diagnostic, "legacy_halt": True, "attempt": None, "pair": None}
        assert (await client.post(PATH + "/report", json={**payload, "diagnostics": legacy},
                                  headers=headers)).json() == {"status": "logged"}
        assert (await client.post(PATH + "/report", json={**payload, "delivery_ids": [identity]},
                                  headers=headers)).status_code == 400
        incident = {"incident_id": identity, "phase": "blocked", "name": "fixture",
            "endpoint": identity, "generation": 1, "delivery_ids": [identity],
            "reason": "bound-tab-missing", "started_at": now, "observed_at": now}
        assert (await client.post(PATH + "/report", json={**payload, "incident": incident},
                                  headers=headers)).status_code == 400
        for field, value in [("editorFound", "private draft sentinel"), ("draftText", "private draft sentinel")]:
            invalid = deepcopy(payload)
            invalid["diagnostics"]["attempt"]["facts"][field] = value
            assert (await client.post(PATH + "/report", json=invalid, headers=headers)).status_code == 400
        for field, value in [("outcome", "private draft sentinel"), ("reason", "private draft sentinel"),
                             ("exception", "private draft sentinel"), ("delivery_ids", [identity] * 51)]:
            invalid = deepcopy(payload)
            invalid["diagnostics"]["attempt"][field] = value
            assert (await client.post(PATH + "/report", json=invalid, headers=headers)).status_code == 400
        maximum = deepcopy(payload)
        maximum["name"] = "x" * 80
        maximum["diagnostics"]["report_failure"] = "report-unavailable"
        maximum["diagnostics"]["attempt"]["delivery_ids"] = [str(uuid4()) for _ in range(50)]
        maximum["diagnostics"]["attempt"]["generation"] = 9007199254740991
        maximum["diagnostics"]["attempt"]["exception"] = "ReferenceError"
        maximum["diagnostics"]["attempt"]["facts"] = dict.fromkeys([
            "urlMatched", "editorFound", "busy", "draftPresent", "inserted",
            "controlPresent", "controlEnabled", "clickDispatched"], False)
        maximum["diagnostics"]["attempt"]["facts"]["controlSelection"] = "missing-form"
        maximum["diagnostics"]["current_control"] = {"release": identity, "boot": identity, "reason": "editor-unavailable"}
        maximum["diagnostics"]["pair"]["reason"] = "conversation-already-bound"
        request = client.build_request("POST", PATH + "/report", json=maximum, headers=headers)
        assert len(request.content) < 4096, "all 50 IDs occur only inside the attempt, not twice"
        assert (await client.send(request)).json() == {"status": "logged"}
        assert "private draft sentinel" not in caplog.text and token not in caplog.text
        reporter.send.assert_not_awaited()
        # The preexisting HTTP payload still works, without diagnostics/schema migration.
        ordinary = {"name": "fixture", "delivery_ids": [], "outcome": "draft-present"}
        assert (await client.post(PATH + "/report", json=ordinary, headers=headers)).json() == {
            "status": "logged", "escalation": "configured"}
