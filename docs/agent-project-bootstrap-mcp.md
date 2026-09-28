# Agent-project bootstrap and ordinary MCP

The ordinary authenticated HTTP/OAuth MCP exposes `agent_project_bootstrap` so
ChatGPT can use the existing Asana project bootstrap even though its shell has
no Asana network access. The tool mirrors the retained
`switchstand-bootstrap-agent-project` inputs and result. It previews by default;
the caller must set `apply=true` to permit the existing bootstrap to write.

This is a thin adapter, not a general Asana administration surface. It uses the
service's `ASANA_TOKEN` and fixed Asana API endpoint, and delegates project,
section, custom-field, and master-task behavior to `durable_agent_project.py`.
The caller cannot select a provider, endpoint, or credential.

The adapter deliberately preserves the CLI's operational contract. Preflight
and final readback still enforce the existing shape, and a failure after a
possible provider write still reports `UNKNOWN`; the MCP adds no journal,
automatic replay, or cancellation recovery. An operator must inspect Asana
before retrying an ambiguous apply.

OAuth admission to the ordinary edge is the caller boundary. Possession of the
tool does not authorize a bootstrap: effect authority must still come from the
active assignment. Landing this code is inert and does not deploy the edge,
enable a client, or authorize an Asana call.
