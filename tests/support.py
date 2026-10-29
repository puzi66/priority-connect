"""Shared fixtures for Priority client tests."""

from __future__ import annotations

import httpx

from priority_mcp.client import PriorityClient

SANDBOX_URL = "https://t.eu.priority-connect.online/odata/Priority/tabbtd38.ini/usdemo"


class ScriptedApi:
    """httpx mock that returns queued responses and records requests."""

    def __init__(self, responses: list[tuple[int, object]], **client_kwargs: object) -> None:
        self.responses = list(responses)
        self.calls: list[httpx.Request] = []
        self.delays: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            if not self.responses:
                raise AssertionError(f"unexpected {request.method} {request.url}")
            status, body = self.responses.pop(0)
            headers: dict[str, str] = {}
            if isinstance(body, tuple):
                body, headers = body
            if isinstance(body, (dict, list)):
                return httpx.Response(status, json=body, headers=headers)
            if isinstance(body, str):
                body = body.encode()
            return httpx.Response(status, content=body, headers=headers)

        defaults: dict[str, object] = {
            "base_url": "https://erp.example/odata/Priority/tabula.ini/demo",
            "username": "api",
            "password": "secret",
            "jitter_ratio": 0,
            "sleep": self.sleep,
            "transport": httpx.MockTransport(handler),
            "timeout": 5.0,
        }
        defaults.update(client_kwargs)
        self.client = PriorityClient(**defaults)  # type: ignore[arg-type]

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)


def payload(result: object) -> dict:
    """Structured tool output, unwrapping the SDK's single-value envelope."""

    content = getattr(result, "structured_content", None)
    if isinstance(content, dict):
        if set(content) == {"result"} and isinstance(content["result"], dict):
            return content["result"]
        return content
    text = result.content[0].text  # type: ignore[attr-defined]
    import json

    parsed = json.loads(text)
    if isinstance(parsed, dict):
        return parsed
    raise AssertionError(f"expected a JSON object, got {text[:500]}")


async def tool_text(result: object) -> str:
    chunks = []
    for block in result.content:  # type: ignore[attr-defined]
        text = getattr(block, "text", "")
        if text:
            chunks.append(text)
    return "\n".join(chunks)
