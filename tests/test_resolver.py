import json

from switchstand.contracts import SourceTask, SourceTaskResult
from switchstand.resolver import REGISTRY_TASK_GID, normalize_alias, resolve_alias


def source(gid: str, notes: str = "") -> SourceTaskResult:
    return SourceTaskResult(
        status="ok",
        item=SourceTask(
            task_gid=gid, title=f"Task {gid}", notes=notes,
            completed=False, revision=f"r-{gid}",
        ),
    )


async def test_resolver_reads_registry_and_every_exact_reference():
    notes = """CURRENT
MCP_RESOLVER_V1
{
  "chatgpt_mcp": {
    "owner": "1218572464592132",
    "roles": {
      "execution": ["1218891495699859"],
      "spec": ["1218891186737600"]
    }
  }
}
"""
    seen = []
    values = {
        REGISTRY_TASK_GID: source(REGISTRY_TASK_GID, notes),
        "1218572464592132": source("1218572464592132"),
        "1218891495699859": source("1218891495699859"),
        "1218891186737600": source("1218891186737600"),
    }

    async def reader(gid):
        seen.append(gid)
        return values[gid]

    result = await resolve_alias("ChatGPT MCP", reader)
    assert result.status == "ok"
    assert result.alias == "chatgpt_mcp"
    assert result.hit is not None
    assert result.hit.owner.task_gid == "1218572464592132"
    assert [task.task_gid for task in result.hit.roles["execution"]] == ["1218891495699859"]
    assert seen == [
        REGISTRY_TASK_GID, "1218572464592132", "1218891495699859", "1218891186737600"
    ]


async def test_resolver_fails_closed_on_missing_block_alias_reference_or_duplicate():
    cases = [
        "no resolver block",
        "MCP_RESOLVER_V1\n{}",
        'MCP_RESOLVER_V1\n{"stateful":{"owner":"1","roles":{"map":["2","2"]}}}',
    ]
    for notes in cases:
        async def reader(gid, notes=notes):
            if gid == REGISTRY_TASK_GID:
                return source(gid, notes)
            return SourceTaskResult(status="unknown")
        assert (await resolve_alias("stateful", reader)).status == "unknown"

    notes = 'MCP_RESOLVER_V1\n{"stateful":{"owner":"1","roles":{"map":["2"]}}}'
    async def missing_reference(gid):
        if gid == REGISTRY_TASK_GID:
            return source(gid, notes)
        if gid == "1":
            return source(gid)
        return SourceTaskResult(status="unknown")
    assert (await resolve_alias("stateful", missing_reference)).status == "unknown"


def test_alias_normalization_is_deterministic():
    assert normalize_alias("  ChatGPT / MCP ") == "chatgpt_mcp"
