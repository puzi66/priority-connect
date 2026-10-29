"""MCP tool contracts, annotations, and both transports."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import uvicorn
from mcp import Client, StdioServerParameters

from priority_mcp.server import create_server
from tests.support import ScriptedApi, payload, tool_text

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")


def _server(responses: list[tuple[int, object]], **kwargs: object):
    script = ScriptedApi(responses, **kwargs)
    return script, create_server(script.client)


@pytest.mark.asyncio
async def test_tool_annotations_and_names():
    script, server = _server([])
    async with Client(server) as client:
        listed = await client.list_tools()
    tools = {tool.name: tool for tool in listed.tools}
    assert set(tools) == {
        "list_entities",
        "describe_entity",
        "query_entity",
        "get_record",
        "get_subform",
        "create_record",
        "update_record",
        "delete_record",
    }
    for name in ("list_entities", "describe_entity", "query_entity", "get_record", "get_subform"):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
        assert annotations.destructive_hint is False
        assert annotations.idempotent_hint is True
    for name in ("create_record", "update_record", "delete_record"):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is False
        assert annotations.destructive_hint is True
    assert tools["create_record"].annotations.idempotent_hint is False
    assert tools["update_record"].annotations.idempotent_hint is True
    assert tools["delete_record"].annotations.idempotent_hint is True
    assert script.calls == []


@pytest.mark.asyncio
async def test_query_and_errors_through_mcp():
    script, server = _server(
        [
            (200, {"value": [{"CUSTNAME": "1001", "CUSTDES": "Northwind"}]}),
            (404, b""),
        ]
    )
    async with Client(server) as client:
        result = await client.call_tool("query_entity", {"entity": "CUSTOMERS", "select": "CUSTNAME,CUSTDES"})
        body = payload(result)
        assert body["value"][0]["CUSTNAME"] == "1001"
        assert "$top=50" in str(script.calls[0].url)
        assert "$select=CUSTNAME,CUSTDES" in str(script.calls[0].url)

        missing = await client.call_tool("get_record", {"entity": "ORDERS", "key": "NOPE"})
        assert missing.is_error
        text = await tool_text(missing)
        assert "404" in text
        assert "case-sensitive" in text

        rejected = await client.call_tool("query_entity", {"entity": "../CUSTOMERS"})
        assert rejected.is_error
        assert "Invalid" in await tool_text(rejected)
        assert len(script.calls) == 2


@pytest.mark.asyncio
async def test_composite_key_and_writes_through_mcp():
    script, server = _server(
        [
            (200, {"DEBIT": "D", "IVNUM": "T4862"}),
            (201, {"ORDNAME": "SO9"}),
            (200, {"ORDNAME": "SO9"}),
            (201, b""),
        ]
    )
    async with Client(server) as client:
        record = await client.call_tool(
            "get_record",
            {"entity": "CINVOICESCONT", "keys": {"DEBIT": "D", "IVNUM": "T4862"}, "select": "DEBIT,IVNUM"},
        )
        assert payload(record)["record"]["IVNUM"] == "T4862"
        assert "CINVOICESCONT(DEBIT='D',IVNUM='T4862')" in str(script.calls[0].url)

        created = await client.call_tool(
            "create_record",
            {"entity": "ORDERS", "data": {"CUSTNAME": "C1"}},
        )
        assert payload(created)["record"]["ORDNAME"] == "SO9"
        assert script.calls[1].method == "POST"

        updated = await client.call_tool(
            "update_record",
            {"entity": "ORDERS", "key": "SO9", "data": {"DETAILS": "x"}},
        )
        assert script.calls[2].method == "PATCH"
        assert payload(updated)["record"]["ORDNAME"] == "SO9"

        deleted = await client.call_tool("delete_record", {"entity": "ORDERS", "key": "SO9"})
        assert payload(deleted)["deleted"] is True
        assert script.calls[3].method == "DELETE"


@pytest.mark.asyncio
async def test_read_only_and_missing_configuration(monkeypatch):
    script, server = _server([], read_only=True)
    async with Client(server) as client:
        blocked = await client.call_tool("delete_record", {"entity": "ORDERS", "key": "SO1"})
        assert blocked.is_error
        assert "read-only" in (await tool_text(blocked)).lower()
    assert script.calls == []

    monkeypatch.delenv("PRIORITY_URL", raising=False)
    monkeypatch.delenv("PRIORITY_PAT", raising=False)
    monkeypatch.delenv("PRIORITY_USER", raising=False)
    monkeypatch.delenv("PRIORITY_PASSWORD", raising=False)
    monkeypatch.setattr("priority_mcp.server._CLIENT", None)
    bare = create_server()
    async with Client(bare) as client:
        failed = await client.call_tool("list_entities", {})
    assert failed.is_error
    assert "PRIORITY_URL" in await tool_text(failed)


def test_cli_parses_http_options(monkeypatch):
    from priority_mcp.cli import http_run_kwargs, parse_args

    monkeypatch.delenv("PRIORITY_MCP_TRANSPORT", raising=False)
    monkeypatch.delenv("PRIORITY_MCP_HOST", raising=False)
    monkeypatch.delenv("PRIORITY_MCP_PORT", raising=False)
    monkeypatch.delenv("PRIORITY_MCP_PATH", raising=False)
    monkeypatch.delenv("PRIORITY_MCP_STATELESS", raising=False)
    monkeypatch.delenv("PRIORITY_MCP_JSON_RESPONSE", raising=False)
    monkeypatch.delenv("PRIORITY_MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("PRIORITY_READ_ONLY", raising=False)

    stdio = parse_args([])
    assert stdio.transport == "stdio"
    assert stdio.port == 8741

    args = parse_args(
        ["--transport", "http", "--host", "0.0.0.0", "--port", "9001", "--stateless", "--json-response", "--path", "/api"]
    )
    kwargs = http_run_kwargs(args)
    assert kwargs["transport"] == "streamable-http"
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9001
    assert kwargs["stateless_http"] is True
    assert kwargs["json_response"] is True
    assert kwargs["streamable_http_path"] == "/api"
    assert "transport_security" not in kwargs

    locked = parse_args(["--transport", "http", "--allowed-hosts", "priority.example.com:*,localhost:*"])
    security = http_run_kwargs(locked)["transport_security"]
    assert security.enable_dns_rebinding_protection is True
    assert "localhost:*" in security.allowed_hosts


def test_cli_exits_when_configuration_is_missing(monkeypatch):
    env = os.environ.copy()
    for name in ("PRIORITY_URL", "PRIORITY_PAT", "PRIORITY_USER", "PRIORITY_PASSWORD", "PRIORITY_READ_ONLY"):
        env.pop(name, None)
    env["PYTHONPATH"] = SRC
    completed = subprocess.run(
        [sys.executable, "-m", "priority_mcp"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 2
    assert "PRIORITY_URL" in completed.stderr
    assert completed.stdout == ""


@pytest.mark.asyncio
async def test_stdio_transport_lists_tools():
    env = {
        "PYTHONPATH": SRC,
        "PRIORITY_URL": "https://erp.example/odata/Priority/tabula.ini/demo",
        "PRIORITY_USER": "api",
        "PRIORITY_PASSWORD": "secret",
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "priority_mcp"], env=env, cwd=ROOT)
    async with Client(params) as client:
        listed = await asyncio.wait_for(client.list_tools(), timeout=20)
    names = {tool.name for tool in listed.tools}
    assert "query_entity" in names
    assert "delete_record" in names


@pytest.mark.asyncio
async def test_streamable_http_transport_calls_a_tool():
    script, server = _server([(200, {"value": [{"name": "CUSTOMERS", "kind": "EntitySet"}]})])
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    app = server.streamable_http_app(host="127.0.0.1", stateless_http=True, json_response=True)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    http_server = uvicorn.Server(config)
    task = asyncio.create_task(http_server.serve())
    try:
        for _ in range(50):
            if http_server.started:
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("streamable HTTP server did not start")
        async with Client(f"http://127.0.0.1:{port}/mcp") as client:
            listed = await asyncio.wait_for(client.list_tools(), timeout=20)
            assert "list_entities" in {tool.name for tool in listed.tools}
            result = await asyncio.wait_for(client.call_tool("list_entities", {"grep": "cust"}), timeout=20)
        assert payload(result)["entities"] == ["CUSTOMERS"]
    finally:
        http_server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
