"""Errors surfaced to MCP clients."""

from __future__ import annotations


class PriorityError(Exception):
    """A failed Priority API call, or a rejected request before it was sent.

    ``status`` is the HTTP status when the failure came from Priority.
    The exception text is what the model should read: status, URL, API
    message, and a short hint.
    """

    def __init__(self, message: str, *, status: int | None = None, hint: str | None = None) -> None:
        self.status = status
        self.hint = hint
        text = message if not hint else f"{message}\nhint: {hint}"
        super().__init__(text)


HINTS: dict[int, str] = {
    400: "Check $filter syntax, field names, and the JSON body. Entity and field names are case-sensitive.",
    401: "Check PRIORITY_USER and PRIORITY_PASSWORD, or PRIORITY_PAT (username is the token, password is the literal PAT).",
    403: (
        "The form may not be enabled in API Forms (Limited Access), or this user has no permission. "
        "For a per-application license, set PRIORITY_APP_ID and PRIORITY_APP_KEY."
    ),
    404: "That form or record was not found. Names are case-sensitive and are usually UPPERCASE.",
    409: "A record with this key already exists.",
    429: "Priority Cloud allows about 100 calls per minute per tenant and 10 requests at a time. Wait, then retry.",
    500: "Priority reported an internal error. The message above is from the API.",
}


def extract_api_message(body: str) -> str:
    """Pull the human-readable message out of a Priority error body.

    Since Priority 19.1, errors are JSON: ``{"error": {"code", "message"}}``.
    Older servers sent XML or plain text; that text is returned trimmed.
    """

    import json

    text = body.strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text[:2000]
    if not isinstance(data, dict):
        return text[:2000]
    err = data.get("error")
    if err is None:
        err = data.get("odata.error")
    if not isinstance(err, dict):
        return text[:2000]
    message = err.get("message")
    if isinstance(message, dict):
        message = message.get("value") or message.get("message") or ""
    message = str(message or "").strip()
    code = err.get("code")
    if code and message:
        return f"{code}: {message}"
    return message or text[:2000]


def format_http_error(
    *,
    status: int,
    method: str,
    url: str,
    body: str,
    attempts: int = 0,
) -> str:
    """Build the tool-facing error for a non-success HTTP response."""

    lines = [f"HTTP {status} {method} {url}"]
    api_message = extract_api_message(body)
    if api_message:
        lines.append(api_message)
    elif body.strip():
        lines.append(body.strip()[:2000])
    if status == 429 and attempts:
        lines.append(f"Retried {attempts} time(s) and Priority was still returning HTTP 429.")
    return "\n".join(lines)
