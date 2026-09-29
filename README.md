# Switchstand

Switchstand is a provider-neutral controller for bounded engineering work. It keeps durable work, message, grant, and protected-effect identity behind stable application contracts while provider credentials and provider-specific behavior remain in trusted adapters. Readability and tool availability do not create authority.

The implementation targets Python 3.14, PostgreSQL, SQLAlchemy 2, Alembic, HTTPX, Pydantic 2, and the MCP Python SDK v2. `marcogallotta/switchstandold` is retired read-only evidence, not an implementation base.

## Supported modes

| Mode | Entry | Boundary |
| --- | --- | --- |
| Ordinary Codex Coordinator | `scripts/switchstand` or `scripts/switchstand --coordinator` in the canonical repository | Runs through `scripts/codex-dispatch` with normal host development capability and no launch-bound WorkId. Coordinator duty and tool access grant no work or provider effect. |
| Ordinary Claude Code Coordinator | `scripts/switchstand --claude` | Runs through `scripts/claude-dispatch` with a clean `CLAUDE_CONFIG_DIR`, only repository-owned settings and MCP config, and read-only `switchstand` tools. Same authority boundary as the Codex Coordinator. |
| Managed Codex worker | `scripts/switchstand --active <Asana task ID or URL> -- <assignment>` | Trusted launch binds one active WorkId, bounded references, runtime currentness, and the available managed tools. It is a one-task worker, not workspace discovery. |
| Ordinary ChatGPT | Authenticated repository-configured `switchstand` HTTP/OAuth MCP | Uses admitted WorkIds and durable messaging. One chat should have one stable named agent identity distinct from OAuth authentication; the current implementation does not yet fully provide that separation. |

Exact-candidate managed qualification uses `scripts/switchstand --isolated --active <task> --commit <SHA>`. It remains fail-closed until the separately managed external selector has an ACTIVE CONTROL manifest; repository landing alone does not deploy or activate that path.

See [How Marco uses Switchstand](docs/how-marco-uses-switchstand.md) for the current usage and identity model. The executable factories, schemas, repository MCP allowlist, and tests own exact tool inventory; raw `source_*` reads are bounded managed compatibility, not the ordinary mental model.

## Quick start

```bash
scripts/bootstrap
docker compose up --build
sh scripts/check

# Ordinary Coordinator
scripts/switchstand

# One managed task-bound worker
scripts/switchstand --active <Asana task ID or URL> -- <exact initial assignment>

# Create an owned writer at an accepted exact base
scripts/switchstand-worktree <writer-name> <exact-40-character-green-SHA>
```

Ordinary ChatGPT without a checkout uses `repository_bundle_get` as specified in [AGENTS.md](AGENTS.md): accept only `current`, verify the advertised SHA-256, and materialize a normal repository. Existing Codex/Claude checkouts stay on normal Git.

## Documentation map

- [Agent bootstrap](AGENTS.md): load-bearing authority, safety, routing, and repository bootstrap.
- [How Marco uses Switchstand](docs/how-marco-uses-switchstand.md): current modes, identities, routing, and messaging model.
- [Architecture](docs/architecture.md): runtime surfaces, semantic owners, durable state, and edit map.
- [Development](docs/development.md): writers, checks, evidence subjects, launch, and recovery.
- [Code quality and Code Review](docs/code-quality.md): implementation, review, qualification, and candidate/current-target rules.
- [Work, messaging, and source compatibility](docs/source-history-feedback.md): WorkId reads, durable messages, feedback, and legacy source boundaries.
- [Agent-project bootstrap MCP](docs/agent-project-bootstrap-mcp.md): the thin ordinary MCP adapter, its dry-run default, and its retained operator/UNKNOWN boundaries.
- [North Star](docs/north-star.md): durable strategic direction; a guiding reference only, not an execution owner.
- [Roadmap](docs/roadmap.md): short, volatile snapshot of current near-term priorities; not strategic direction.
