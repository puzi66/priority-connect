"""Read-only checks against Priority's public sandbox.

The sandbox is shared and publicly documented. These tests never create,
update, or delete. The client is constructed read-only, and its request
method refuses every verb except GET before a byte is sent.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from mcp import Client

from priority_mcp.client import PriorityClient
from priority_mcp.errors import PriorityError
from priority_mcp.server import create_server
from tests.support import SANDBOX_URL, payload, tool_text

pytestmark = pytest.mark.integration

if os.environ.get("PRIORITY_SKIP_INTEGRATION", "").strip().lower() in {"1", "true", "yes", "on"}:
    pytest.skip("PRIORITY_SKIP_INTEGRATION is set", allow_module_level=True)


@asynccontextmanager
async def sandbox_session() -> AsyncIterator[Client]:
    """One MCP session. Opened and closed in the test task so anyio stays happy."""

    client = PriorityClient(
        base_url=SANDBOX_URL,
        username="apidemo",
        password="123",
        read_only=True,
        timeout=60,
        max_retries=3,
        retry_base=1,
        jitter_ratio=0,
    )
    original = client.request

    async def guarded(method: str, path: str, **kwargs: object):
        if method.upper() != "GET":
            raise PriorityError(f"Refusing {method} {path}: sandbox integration tests are read-only.")
        if "://" in str(path) and not str(path).startswith(SANDBOX_URL):
            raise PriorityError(f"Refusing to leave the public sandbox: {path}")
        return await original(method, path, **kwargs)

    client.request = guarded  # type: ignore[method-assign]
    assert client.base_url == SANDBOX_URL
    assert client.read_only is True
    try:
        async with Client(create_server(client)) as mcp_client:
            yield mcp_client
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_sandbox_refuses_writes_without_calling_the_api():
    async with sandbox_session() as sandbox:
        result = await sandbox.call_tool(
            "create_record",
            {"entity": "CUSTOMERS", "data": {"CUSTNAME": "SHOULD_NOT_EXIST"}},
        )
        assert result.is_error
        text = (await tool_text(result)).lower()
        assert "read-only" in text


@pytest.mark.asyncio
async def test_list_and_describe_orders():
    async with sandbox_session() as sandbox:
        listed = payload(await sandbox.call_tool("list_entities", {"grep": "CUSTOMERS"}))
        assert "CUSTOMERS" in listed["entities"]
        assert listed["count"] >= 1

        described = payload(await sandbox.call_tool("describe_entity", {"entity": "orders"}))
        assert described["entity_set"] == "ORDERS"
        assert "ORDNAME" in described["keys"]
        assert "name_note" in described
        fields = {field["name"] for field in described["fields"]}
        assert {"ORDNAME", "CUSTNAME", "CURDATE"} <= fields
        subforms = {item["name"]: item for item in described["subforms"]}
        assert subforms["ORDERITEMS_SUBFORM"]["collection"] is True
        order_name = next(field for field in described["fields"] if field["name"] == "ORDNAME")
        assert order_name.get("description")


@pytest.mark.asyncio
async def test_query_and_get_customer():
    async with sandbox_session() as sandbox:
        listed = payload(
            await sandbox.call_tool(
                "query_entity",
                {"entity": "CUSTOMERS", "select": "CUSTNAME,CUSTDES", "top": 2},
            )
        )
        assert 1 <= listed["count"] <= 2
        first = listed["value"][0]
        assert first["CUSTNAME"]
        assert "@odata.context" not in first

        record = payload(
            await sandbox.call_tool(
                "get_record",
                {"entity": "CUSTOMERS", "key": first["CUSTNAME"], "select": "CUSTNAME,CUSTDES"},
            )
        )
        assert record["record"]["CUSTNAME"] == first["CUSTNAME"]
        assert "@odata.context" not in record["record"]

        if "'" not in first["CUSTNAME"]:
            filtered = payload(
                await sandbox.call_tool(
                    "query_entity",
                    {
                        "entity": "CUSTOMERS",
                        "filter": f"CUSTNAME eq '{first['CUSTNAME']}'",
                        "select": "CUSTNAME",
                        "top": 1,
                    },
                )
            )
            assert filtered["value"][0]["CUSTNAME"] == first["CUSTNAME"]


@pytest.mark.asyncio
async def test_order_subform_and_composite_key():
    async with sandbox_session() as sandbox:
        orders = payload(
            await sandbox.call_tool(
                "query_entity",
                {"entity": "ORDERS", "select": "ORDNAME,CUSTNAME", "top": 1, "orderby": "CURDATE desc"},
            )
        )
        assert orders["count"] == 1
        order_name = orders["value"][0]["ORDNAME"]
        lines = payload(
            await sandbox.call_tool(
                "get_subform",
                {
                    "entity": "ORDERS",
                    "key": order_name,
                    "subform": "ORDERITEMS_SUBFORM",
                    "select": "PARTNAME,TQUANT,PRICE",
                    "top": 2,
                },
            )
        )
        assert lines["subform"] == "ORDERITEMS_SUBFORM"
        assert lines["entity"] == "ORDERS"
        assert isinstance(lines["value"], list)

        composite = payload(
            await sandbox.call_tool(
                "query_entity",
                {"entity": "CINVOICESCONT", "select": "DEBIT,IVNUM", "top": 1},
            )
        )
        assert composite["count"] == 1
        debit = composite["value"][0]["DEBIT"]
        invoice = composite["value"][0]["IVNUM"]
        by_object = payload(
            await sandbox.call_tool(
                "get_record",
                {
                    "entity": "CINVOICESCONT",
                    "keys": {"DEBIT": debit, "IVNUM": invoice},
                    "select": "DEBIT,IVNUM",
                },
            )
        )
        assert by_object["record"]["DEBIT"] == debit
        assert by_object["record"]["IVNUM"] == invoice
        by_string = payload(
            await sandbox.call_tool(
                "get_record",
                {
                    "entity": "CINVOICESCONT",
                    "key": f"DEBIT={debit},IVNUM={invoice}",
                    "select": "DEBIT,IVNUM",
                },
            )
        )
        assert by_string["record"]["IVNUM"] == invoice


@pytest.mark.asyncio
async def test_missing_record_is_a_clear_error():
    async with sandbox_session() as sandbox:
        result = await sandbox.call_tool("get_record", {"entity": "NO_SUCH_ENTITY", "key": "1"})
        assert result.is_error
        text = await tool_text(result)
        assert "404" in text or "not found" in text.lower()
        assert "case-sensitive" in text.lower() or "not found" in text.lower()
