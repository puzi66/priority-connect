"""HTTP client for the Priority OData service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from priority_mcp import __version__
from priority_mcp.errors import HINTS, PriorityError, format_http_error
from priority_mcp.odata import (
    Metadata,
    entity_sets_from_service_document,
    format_key,
    query_options,
    slim,
    unknown_entity_message,
    validate_ident,
)

logger = logging.getLogger("priority_mcp")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise PriorityError(f"Expected a boolean value, got {value!r}. Use true or false.")


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return max(0.0, when.timestamp() - time.time())


class PriorityClient:
    """Async Priority OData client.

    Authentication is HTTP Basic. A personal access token is sent as
    username=token and password ``PAT``. Optional ``X-App-Id`` and
    ``X-App-Key`` headers cover per-application licensing.

    HTTP 429 is retried with exponential backoff, honoring ``Retry-After``
    when Priority sends it (capped at 120 seconds).
    """

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        app_id: str | None = None,
        app_key: str | None = None,
        timeout: float = 180.0,
        max_retries: int = 4,
        retry_base: float = 1.0,
        retry_max: float = 30.0,
        jitter_ratio: float = 0.25,
        read_only: bool = False,
        metadata_ttl: float = 600.0,
        max_response_bytes: int = 5_000_000,
        metadata_max_bytes: int = 32_000_000,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        rng: Callable[[], float] | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise PriorityError(
                "PRIORITY_URL must be an http(s) URL: "
                "https://<server>/odata/Priority/<tabula.ini>/<company>."
            )
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.app_id = app_id or None
        self.app_key = app_key or None
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_base = retry_base
        self.retry_max = retry_max
        self.jitter_ratio = jitter_ratio
        self.read_only = read_only
        self.metadata_ttl = metadata_ttl
        self.max_response_bytes = max_response_bytes
        self.metadata_max_bytes = metadata_max_bytes
        self._transport = transport
        self._sleep = sleep or asyncio.sleep
        self._rng = rng or random.random
        self._http_client: httpx.AsyncClient | None = None
        self._names: tuple[float, list[str]] | None = None
        self._descriptions: dict[str, tuple[float, dict[str, Any]]] = {}
        self._full_metadata: tuple[float, str] | None = None

    @classmethod
    def from_env(cls) -> PriorityClient:
        """Build a client from PRIORITY_* environment variables."""

        url = os.environ.get("PRIORITY_URL", "").strip()
        if not url:
            raise PriorityError(
                "PRIORITY_URL is not set. Expected "
                "https://<server>/odata/Priority/<tabula.ini>/<company>."
            )
        pat = os.environ.get("PRIORITY_PAT", "").strip()
        if pat:
            username, password = pat, "PAT"
        else:
            username = os.environ.get("PRIORITY_USER", "")
            if not username.strip() or "PRIORITY_PASSWORD" not in os.environ:
                raise PriorityError(
                    "Set PRIORITY_USER and PRIORITY_PASSWORD, or set PRIORITY_PAT "
                    "(sent as username=token, password=PAT)."
                )
            username = username.strip()
            password = os.environ["PRIORITY_PASSWORD"]
        app_id = os.environ.get("PRIORITY_APP_ID", "").strip() or None
        app_key = os.environ.get("PRIORITY_APP_KEY", "").strip() or None
        timeout = _env_float("PRIORITY_TIMEOUT", 180.0)
        max_retries = _env_int("PRIORITY_MAX_RETRIES", 4)
        read_only = parse_bool(os.environ.get("PRIORITY_READ_ONLY"))
        max_response = _env_int("PRIORITY_MAX_RESPONSE_BYTES", 5_000_000)
        return cls(
            base_url=url,
            username=username,
            password=password,
            app_id=app_id,
            app_key=app_key,
            timeout=timeout,
            max_retries=max_retries,
            read_only=read_only,
            max_response_bytes=max_response,
        )

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> PriorityClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _http(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            headers = {"User-Agent": f"priority-mcp/{__version__}"}
            if self.app_id:
                headers["X-App-Id"] = self.app_id
            if self.app_key:
                headers["X-App-Key"] = self.app_key
            self._http_client = httpx.AsyncClient(
                auth=httpx.BasicAuth(self.username, self.password),
                headers=headers,
                timeout=httpx.Timeout(self.timeout, connect=min(30.0, self.timeout)),
                transport=self._transport,
                follow_redirects=False,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._http_client

    def _url(self, path: str) -> str:
        relative = path.lstrip("/")
        if not relative:
            return self.base_url
        return f"{self.base_url}/{relative}"

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        parsed = _parse_retry_after(retry_after)
        if parsed is not None:
            delay = min(parsed, 120.0)
        else:
            delay = min(self.retry_max, self.retry_base * (2**attempt))
        if self.jitter_ratio:
            delay *= 1 + (self.jitter_ratio * self._rng())
        return delay

    async def _read_bounded(self, response: httpx.Response, limit: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > limit:
                raise PriorityError(
                    f"Priority response exceeded {limit} bytes before it finished.",
                    status=response.status_code,
                    hint="Use a smaller $top and a narrower $select or $expand.",
                )
            chunks.append(chunk)
        return b"".join(chunks)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        accept: str = "application/json",
        raw: bool = False,
        max_bytes: int | None = None,
    ) -> Any:
        """Send one OData request. Returns parsed JSON, or text when ``raw``."""

        verb = method.upper()
        if self.read_only and verb not in {"GET", "HEAD"}:
            raise PriorityError(
                f"Refusing {verb} because this client is read-only (PRIORITY_READ_ONLY).",
                hint="Unset PRIORITY_READ_ONLY to create, update, or delete. Writes consume billed API transactions.",
            )
        url = self._url(path)
        headers = {"Accept": accept}
        request_kwargs: dict[str, Any] = {"headers": headers}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            if verb in {"POST", "PATCH"}:
                headers["Prefer"] = "return=representation"
            request_kwargs["json"] = json_body
        limit = max_bytes if max_bytes is not None else (self.metadata_max_bytes if raw else self.max_response_bytes)
        attempt = 0
        while True:
            try:
                async with self._http().stream(verb, url, **request_kwargs) as response:
                    body = await self._read_bounded(response, limit)
                    status = response.status_code
                    retry_after = response.headers.get("Retry-After")
            except PriorityError:
                raise
            except httpx.TimeoutException as exc:
                raise PriorityError(
                    f"Timed out calling {verb} {url}. Priority drops requests that run longer than 3 minutes.",
                    hint="Narrow $select, $expand, and $top, then retry.",
                ) from exc
            except httpx.HTTPError as exc:
                raise PriorityError(f"Cannot reach {url}: {exc}") from exc

            if status == 429 and attempt < self.max_retries:
                delay = self._backoff(attempt, retry_after)
                logger.warning(
                    "Priority API returned 429 for %s %s; retry %d/%d in %.1fs",
                    verb,
                    path.split("?", 1)[0],
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
                await self._sleep(delay)
                attempt += 1
                continue
            return self._interpret(status, body, verb, url, raw=raw, attempts=attempt)

    def _interpret(self, status: int, body: bytes, method: str, url: str, *, raw: bool, attempts: int) -> Any:
        text = body.decode("utf-8-sig", "replace")
        if 300 <= status < 400:
            raise PriorityError(
                f"HTTP {status} redirect from {url} was not followed.",
                status=status,
                hint="PRIORITY_URL should be the final OData service root, with no further redirect.",
            )
        if status >= 400:
            raise PriorityError(
                format_http_error(status=status, method=method, url=url, body=text, attempts=attempts),
                status=status,
                hint=HINTS.get(status),
            )
        if raw:
            return text
        if not text.strip():
            return {"status": status}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise PriorityError(
                f"Expected JSON from {method} {url} but got: {text[:500]}",
                status=status,
            ) from exc

    def _fresh(self, cached: tuple[float, Any] | None) -> Any | None:
        if cached is None:
            return None
        stored_at, value = cached
        if self.metadata_ttl >= 0 and (time.monotonic() - stored_at) > self.metadata_ttl:
            return None
        return value

    async def entity_names(self) -> list[str]:
        cached = self._fresh(self._names)
        if cached is not None:
            return cached
        try:
            document = await self.request("GET", "")
        except PriorityError as exc:
            if exc.status != 404:
                raise
            names = await self._names_from_metadata()
        else:
            if not isinstance(document, dict):
                raise PriorityError("Priority service document was not a JSON object.")
            names = entity_sets_from_service_document(document)
            if not names:
                names = await self._names_from_metadata()
        self._names = (time.monotonic(), names)
        return names

    async def _names_from_metadata(self) -> list[str]:
        xml = await self._metadata_document()
        return sorted(Metadata(xml).entity_sets)

    async def _metadata_document(self) -> str:
        cached = self._fresh(self._full_metadata)
        if isinstance(cached, str):
            return cached
        xml = await self.request(
            "GET",
            "$metadata",
            accept="application/xml",
            raw=True,
            max_bytes=self.metadata_max_bytes,
        )
        if not str(xml).lstrip().startswith("<"):
            raise PriorityError(f"Expected OData metadata XML, got: {str(xml)[:300]}")
        self._full_metadata = (time.monotonic(), xml)
        return xml

    async def _metadata_for(self, entity: str) -> str:
        path = f"GetMetadataFor(entity='{entity}')"
        try:
            xml = await self.request(
                "GET",
                path,
                accept="application/xml, application/json",
                raw=True,
                max_bytes=self.metadata_max_bytes,
            )
        except PriorityError as exc:
            # Older Priority versions have no GetMetadataFor function.
            if exc.status in {404, 501}:
                return await self._metadata_document()
            raise
        if not str(xml).lstrip().startswith("<"):
            raise PriorityError(f"Expected OData metadata XML for {entity}, got: {str(xml)[:300]}")
        return xml

    async def list_entities(self, grep: str | None = None) -> dict[str, Any]:
        names = await self.entity_names()
        if grep:
            needle = grep.strip().lower()
            names = [name for name in names if needle in name.lower()]
        return {"count": len(names), "entities": names}

    async def describe_entity(self, entity: str) -> dict[str, Any]:
        requested = validate_ident(entity, "entity")
        names = await self.entity_names()
        canonical = requested
        if requested not in names:
            matches = [name for name in names if name.lower() == requested.lower()]
            if len(matches) == 1:
                canonical = matches[0]
            elif len(matches) > 1:
                raise PriorityError(
                    f"Entity name {requested!r} is ambiguous: {', '.join(matches)}.",
                    hint="Names are case-sensitive. Pass the canonical entity set name.",
                )
        cached = self._fresh(self._descriptions.get(canonical))
        if isinstance(cached, dict):
            return self._with_name_note(cached, requested, canonical)
        try:
            xml = await self._metadata_for(canonical)
            described = Metadata(xml).describe(canonical)
        except PriorityError as exc:
            if canonical not in names and (exc.status in {404, 500} or "not found" in str(exc).lower()):
                raise PriorityError(unknown_entity_message(requested, names), status=404) from exc
            raise
        self._descriptions[canonical] = (time.monotonic(), described)
        return self._with_name_note(described, requested, canonical)

    def _with_name_note(self, described: dict[str, Any], requested: str, canonical: str) -> dict[str, Any]:
        if requested == canonical or "name_note" in described:
            return described
        return {
            **described,
            "name_note": f"Requested {requested!r}; using canonical entity set {canonical}.",
        }

    async def query_entity(
        self,
        entity: str,
        *,
        filter: str | None = None,
        select: str | None = None,
        expand: str | None = None,
        orderby: str | None = None,
        top: int | None = None,
        skip: int | None = None,
    ) -> dict[str, Any]:
        name = validate_ident(entity, "entity")
        path = name + query_options(
            filter=filter, select=select, expand=expand, orderby=orderby, top=top, skip=skip
        )
        data = await self.request("GET", path)
        return _collection(name, data, top=top, skip=skip)

    async def get_record(
        self,
        entity: str,
        *,
        key: str | None = None,
        keys: dict[str, Any] | None = None,
        select: str | None = None,
        expand: str | None = None,
    ) -> dict[str, Any]:
        name = validate_ident(entity, "entity")
        segment = format_key(key=key, keys=keys)
        path = name + segment + query_options(select=select, expand=expand)
        data = await self.request("GET", path)
        return {"entity": name, "key": keys if keys is not None else key, "record": slim(data)}

    async def get_subform(
        self,
        entity: str,
        subform: str,
        *,
        key: str | None = None,
        keys: dict[str, Any] | None = None,
        filter: str | None = None,
        select: str | None = None,
        expand: str | None = None,
        orderby: str | None = None,
        top: int | None = None,
        skip: int | None = None,
    ) -> dict[str, Any]:
        name = validate_ident(entity, "entity")
        child = validate_ident(subform, "subform")
        segment = format_key(key=key, keys=keys)
        path = (
            f"{name}{segment}/{child}"
            + query_options(filter=filter, select=select, expand=expand, orderby=orderby, top=top, skip=skip)
        )
        data = await self.request("GET", path)
        payload = _collection(name, data, top=top, skip=skip)
        payload["subform"] = child
        payload["key"] = keys if keys is not None else key
        return payload

    async def create_record(self, entity: str, data: dict[str, Any]) -> dict[str, Any]:
        name = validate_ident(entity, "entity")
        _require_object(data, "data")
        result = await self.request("POST", name, json_body=data)
        return {"entity": name, "record": slim(result)}

    async def update_record(
        self,
        entity: str,
        data: dict[str, Any],
        *,
        key: str | None = None,
        keys: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = validate_ident(entity, "entity")
        _require_object(data, "data")
        segment = format_key(key=key, keys=keys)
        result = await self.request("PATCH", name + segment, json_body=data)
        return {"entity": name, "key": keys if keys is not None else key, "record": slim(result)}

    async def delete_record(
        self,
        entity: str,
        *,
        key: str | None = None,
        keys: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = validate_ident(entity, "entity")
        segment = format_key(key=key, keys=keys)
        result = await self.request("DELETE", name + segment)
        payload: dict[str, Any] = {
            "deleted": True,
            "entity": name,
            "key": keys if keys is not None else key,
        }
        if isinstance(result, dict) and set(result) == {"status"}:
            payload["status"] = result["status"]
        else:
            payload["record"] = slim(result)
        return payload


def _require_object(data: Any, what: str) -> None:
    if not isinstance(data, dict):
        raise PriorityError(f"{what} must be a JSON object of field names to values.")


def _collection(entity: str, data: Any, *, top: int | None, skip: int | None) -> dict[str, Any]:
    next_link = data.get("@odata.nextLink") if isinstance(data, dict) else None
    total = data.get("@odata.count") if isinstance(data, dict) else None
    cleaned = slim(data)
    if isinstance(cleaned, dict) and isinstance(cleaned.get("value"), list):
        payload: dict[str, Any] = {"entity": entity, "count": len(cleaned["value"]), "value": cleaned["value"]}
        if top is not None:
            payload["top"] = top
        if skip:
            payload["skip"] = skip
        if total is not None:
            payload["total_count"] = total
        if next_link:
            payload["next_link"] = next_link
        return payload
    return {"entity": entity, "record": cleaned}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise PriorityError(f"{name} must be an integer, got {raw!r}.") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise PriorityError(f"{name} must be a number, got {raw!r}.") from exc
