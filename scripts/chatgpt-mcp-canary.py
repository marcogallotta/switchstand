"""Explicit local qualification server, never an authenticated ChatGPT connection.

Run only on a disposable switchstand_test database and an authorized disposable
Asana task. The command line is a trusted host provisioning surface, not MCP.
"""

import argparse
import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand.chatgpt import ChatGPTService
from switchstand.chatgpt_mcp import build_chatgpt_server
from switchstand.core import provision_launch
from switchstand.grant_state import GrantState
from switchstand.grants import PrincipalContext, WorkGrant
from switchstand.provider import AsanaProvider
from switchstand.state import PostgresState
from switchstand.task_ref import asana_task_id


async def serve(task: str, references: tuple[str, ...]) -> None:
    url = os.environ["TEST_DATABASE_URL"]
    if make_url(url).database != "switchstand_test":
        raise ValueError("canary requires disposable switchstand_test database")
    engine = create_async_engine(url)
    async with httpx.AsyncClient(base_url="https://app.asana.com/api/1.0", trust_env=False,
        headers={"Authorization": f"Bearer {os.environ['ASANA_TOKEN']}"}) as client:
        try:
            provider, state, grants = AsanaProvider(client), PostgresState(engine), GrantState(engine)
            authority = await provision_launch(state, "asana", provider, task, references)
            principal = PrincipalContext(issuer="switchstand-local-canary", subject=task,
                                         client_id="disposable-stdio", assurance="test")
            current = await grants.current(principal.key)
            if current is None:
                current = WorkGrant(id=uuid4(), version=1, principal=principal, authority=authority,
                    operations=frozenset({"work_get", "work_append"}), issuer="local-canary-command",
                    provenance=f"explicit disposable target {task}",
                    expires_at=datetime.now(UTC) + timedelta(minutes=30),
                    append_qualification=f"test:disposable-task:{task}")
                await grants.issue(current, None)
            elif current.authority != authority or not current.current():
                raise ValueError("existing canary grant is different or expired; trusted review required")
            # Re-entry reuses the durable grant and intents; never clears UNKNOWN.
            async def resolve() -> PrincipalContext:
                return principal
            server = build_chatgpt_server(ChatGPTService(resolve, state, grants, {"asana": provider}))
            await server.run_stdio_async()
        finally:
            await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, type=asana_task_id)
    parser.add_argument("--reference", action="append", default=[], type=asana_task_id)
    args = parser.parse_args()
    asyncio.run(serve(args.task, tuple(args.reference)))
