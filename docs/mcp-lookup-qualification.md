# Lookup usability qualification

The candidate changes existing lookup descriptions and unavailable-WorkId recovery.
It adds no procedure reader. Current work notes still own procedure routing.

Pre-activation boundary check: `sh scripts/check tests/test_chatgpt_edge_process.py
-k test_process_replays_durable_database_append_after_restart`. This exercises real
HTTP MCP, process restarts and disposable PostgreSQL with fixture authentication.
It proves transport/persistence behavior, not fresh-agent or ChatGPT acceptance.

For real ChatGPT, reuse the existing TEST owner and setup in
[ChatGPT MCP edge](chatgpt-mcp-edge.md#reuse-and-lifecycle). The operator must first
record the exact candidate running there, an admitted TEST WorkId whose current
notes route to a TEST procedure, an absent UUID, and a verified TEST legacy reference.
Server schema, real GitHub login and the fresh ChatGPT run are separate results.

Use a fresh chat with the TEST connector and duplicated explanatory Project
Settings/Agentic text withheld. Give only this assignment, filling the three IDs:

> Read my assigned TEST work `<WorkId>`. Find and read its current Human Review
> procedure through Switchstand. First check `<absent UUID>`, which was supplied
> as my work reference. If it cannot be read, continue with my assigned TEST work.
> Resolve legacy reference `<verified TEST legacy reference>` too.
> Report the current work and procedure, what happened on the wrong target, and
> whether any work was changed. Do not write or start Human Review.

Pass requires the right current work/procedure, useful wrong-target recovery,
successful legacy resolution, no write and no inferred assignment/authority.
Retain tool calls/results, candidate SHA, connector identity and withheld text.
Any missing login, candidate deployment or client capability leaves ChatGPT
qualification `NOT_RUN`; server-only and native-fork evidence cannot replace it.
Do not delete duplicate instructions until this actual run supports that cut.
