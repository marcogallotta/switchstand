import sys

from mcp import Client, StdioServerParameters


async def test_real_stdio_handshake_exposes_exact_surface():
    server = StdioServerParameters(command=sys.executable, args=["-m", "switchstand.mcp"])
    async with Client(server) as client:
        result = await client.list_tools()
        assert {tool.name for tool in result.tools} == {"work_get", "work_update", "work_append"}
        assert all(tool.output_schema for tool in result.tools)
