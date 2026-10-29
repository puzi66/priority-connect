"""Command-line entry point for stdio and streamable HTTP transports."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from mcp.server.transport_security import TransportSecuritySettings

from priority_mcp import __version__
from priority_mcp.client import PriorityClient, parse_bool
from priority_mcp.errors import PriorityError
from priority_mcp.server import create_server

logger = logging.getLogger("priority_mcp")


def _env_port() -> int:
    raw = os.environ.get("PRIORITY_MCP_PORT", "8741")
    try:
        return int(raw)
    except ValueError as exc:
        raise PriorityError(f"PRIORITY_MCP_PORT must be an integer, got {raw!r}.") from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="priority-mcp",
        description="MCP server for the Priority ERP REST/OData API (stdio or streamable HTTP).",
    )
    parser.add_argument("--version", action="version", version=f"priority-mcp {__version__}")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=os.environ.get("PRIORITY_MCP_TRANSPORT", "stdio"),
        help="stdio (default, for Claude/Cursor local configs) or http (streamable HTTP, for remote hosting). "
        "Env: PRIORITY_MCP_TRANSPORT.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("PRIORITY_MCP_HOST", "127.0.0.1"),
        help="HTTP bind address. Use 0.0.0.0 to accept remote connections. Env: PRIORITY_MCP_HOST.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_env_port(),
        help="HTTP port. Env: PRIORITY_MCP_PORT. Default: 8741.",
    )
    parser.add_argument(
        "--path",
        default=os.environ.get("PRIORITY_MCP_PATH", "/mcp"),
        help="Streamable HTTP endpoint path. Env: PRIORITY_MCP_PATH. Default: /mcp.",
    )
    parser.add_argument(
        "--stateless",
        action="store_true",
        default=parse_bool(os.environ.get("PRIORITY_MCP_STATELESS")),
        help="Disable MCP session affinity so the HTTP server can sit behind a load balancer. "
        "Env: PRIORITY_MCP_STATELESS=1.",
    )
    parser.add_argument(
        "--json-response",
        action="store_true",
        default=parse_bool(os.environ.get("PRIORITY_MCP_JSON_RESPONSE")),
        help="Answer each HTTP request with a single JSON body instead of an SSE stream. "
        "Env: PRIORITY_MCP_JSON_RESPONSE=1.",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        default=parse_bool(os.environ.get("PRIORITY_READ_ONLY")),
        help="Refuse create, update, and delete. Env: PRIORITY_READ_ONLY=1. Use this for the public sandbox.",
    )
    parser.add_argument(
        "--allowed-hosts",
        default=os.environ.get("PRIORITY_MCP_ALLOWED_HOSTS", ""),
        help="Comma-separated Host headers allowed when DNS-rebinding protection is on "
        "(for example priority.example.com:*,localhost:*). Env: PRIORITY_MCP_ALLOWED_HOSTS. "
        "Binding 127.0.0.1 already allows localhost. Binding a public address leaves protection off "
        "until this is set.",
    )
    return parser.parse_args(argv)


def http_run_kwargs(args: argparse.Namespace) -> dict[str, object]:
    """Keyword arguments for MCPServer.run(transport='streamable-http')."""

    kwargs: dict[str, object] = {
        "transport": "streamable-http",
        "host": args.host,
        "port": args.port,
        "streamable_http_path": args.path,
        "stateless_http": args.stateless,
        "json_response": args.json_response,
    }
    allowed = [item.strip() for item in str(args.allowed_hosts).split(",") if item.strip()]
    if allowed:
        kwargs["transport_security"] = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed,
            allowed_origins=[],
        )
    return kwargs


def main(argv: list[str] | None = None) -> None:
    try:
        args = parse_args(argv)
        if args.read_only:
            os.environ["PRIORITY_READ_ONLY"] = "1"
        client = PriorityClient.from_env()
    except PriorityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    server = create_server(client)
    if args.transport == "http":
        logger.info(
            "Priority MCP streamable HTTP on http://%s:%s%s",
            args.host,
            args.port,
            args.path,
        )
        server.run(**http_run_kwargs(args))
        return
    server.run(transport="stdio")
