"""Verify guidance reaches a real client and its example preserves every constraint."""
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import db
from config import settings
from mcp_server.guidance import SERVER_INSTRUCTIONS, STRUCTURED_SEARCH_EXAMPLE


def test_connected_client_receives_guidance_and_can_execute_structured_example(tmp_path, monkeypatch):
    catalog_path = tmp_path / "guidance.db"
    monkeypatch.setattr(settings, "db_path", catalog_path)
    db.init_db()
    conn = db.get_db_connection()
    try:
        # Two exact matches, plus a cheaper counterexample for each requirement.
        listings = [(1, 24, 80, 100, 1, 200), (2, 24, 100, 90, 1, 100),
                    (3, 12, 80, 100, 1, 1), (4, 24, 150, 100, 1, 2),
                    (5, 24, 80, 40, 1, 3), (6, 24, 80, 100, 0, 4)]
        for pid, voltage, rpm, torque, available, price in listings:
            title = f"{voltage}V DC Geared Motor"
            conn.execute(
                "INSERT INTO products(id,vendor,vendor_name,external_id,title,normalized_title,"
                "price,available,product_url,category_canonical) "
                "VALUES(?,'robu','Robu',?,?,?,?,?,'https://example.invalid','motor')",
                (pid, str(pid), title, title.lower(), price, available),
            )
            conn.executemany(
                "INSERT INTO product_specs(product_id,key,value,value_num,value_type) VALUES(?,?,?,?,?)",
                [(pid, 'voltage', str(voltage), voltage, 'enum'),
                 (pid, 'rpm', str(rpm), rpm, 'integer'),
                 (pid, 'torque_kgcm', str(torque), torque, 'float')],
            )
        conn.executemany(
            "INSERT INTO spec_stats_global(key,value_type,applicable_count,value_counts) VALUES(?,?,6,'{}')",
            [('voltage', 'enum'), ('rpm', 'integer'), ('torque_kgcm', 'float')],
        )
        conn.commit()
    finally:
        conn.close()

    async def run():
        # Configure the child before importing the server: never open the real catalog.
        bootstrap = (
            "import sys; from pathlib import Path; from config import settings; "
            "settings.db_path=Path(sys.argv[1]); settings.price_stock_refresh_enabled=False; "
            "settings.matcher_audit_logging=False; "
            "from mcp_server.server import mcp; mcp.run(transport='stdio')"
        )
        params = StdioServerParameters(command=sys.executable,
                                      args=['-c', bootstrap, str(catalog_path)],
                                      cwd=str(Path(__file__).resolve().parents[1]))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.instructions == SERVER_INSTRUCTIONS
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                for name in ('count_components', 'search_components'):
                    properties = tools[name].inputSchema['properties']
                    assert properties['filters']['examples'][0] == STRUCTURED_SEARCH_EXAMPLE['filters']
                    assert 'identity' in properties['query']['description']

                def payload(result):
                    return json.loads(next(item.text for item in result.content if item.type == 'text'))

                requirements = {key: value for key, value in STRUCTURED_SEARCH_EXAMPLE.items()
                                if key not in {'sort', 'limit'}}
                count = payload(await session.call_tool('count_components', requirements))
                found = payload(await session.call_tool('search_components', STRUCTURED_SEARCH_EXAMPLE))
                assert count['ok'] and found['ok']
                assert count['total_matches'] == found['total_matches'] == 2
                assert [row['product_id'] for row in found['data']['results']] == [2, 1]

    asyncio.run(run())
