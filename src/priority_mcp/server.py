"""MCP tools for the Priority ERP OData API."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypeVar

from pydantic import Field

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from priority_mcp import __version__
from priority_mcp.client import PriorityClient
from priority_mcp.errors import PriorityError

_F = TypeVar("_F", bound=Callable[..., Awaitable[Any]])

READ = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
CREATE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)
MODIFY = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=True,
)

INSTRUCTIONS = (
    "Tools for Priority Software ERP through its OData REST API. "
    "Entity and field names are case-sensitive and usually UPPERCASE; call describe_entity when unsure. "
    "Common forms: CUSTOMERS (CUSTNAME), SUPPLIERS (SUPNAME), ORDERS (ORDNAME; subform ORDERITEMS_SUBFORM), "
    "LOGPART (PARTNAME), AINVOICES, CINVOICES, PORDERS, DOCUMENTS_D, CPROF. "
    "list_entities, describe_entity, query_entity, get_record, and get_subform only read. "
    "create_record, update_record, and delete_record modify data and consume billed API transactions. "
    "Leave the public sandbox read-only. Page large results with top and skip. "
    "Dates look like 2025-01-01T00:00:00Z; the company timezone (TZSERVER) may differ from the server."
)


def _safe(fn: _F) -> _F:
    """Turn PriorityError into a tool error the model can read and correct."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except PriorityError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper  # type: ignore[return-value]


def create_server(client: PriorityClient | None = None) -> MCPServer:
    """Build the Priority MCP server. A client is created from the environment if omitted."""

    def current() -> PriorityClient:
        if client is not None:
            return client
        return _shared_client()

    mcp = MCPServer(
        "priority-erp",
        title="Priority ERP",
        description="Read and update Priority Software ERP through the official REST/OData API.",
        instructions=INSTRUCTIONS,
        version=__version__,
    )

    @mcp.tool(title="List entities", annotations=READ)
    @_safe
    async def list_entities(
        grep: Annotated[
            str | None,
            Field(description="Case-insensitive substring, for example ORDER or CUST. Omit to list every entity set."),
        ] = None,
    ) -> dict[str, Any]:
        """List Priority entity sets (forms) exposed by the REST API.

        Names are case-sensitive and usually UPPERCASE. The administrator enables
        each upper-level form in API Forms; subforms are not separate entity sets.
        Pass grep to keep a long catalog short.
        """

        return await current().list_entities(grep)

    @mcp.tool(title="Describe entity", annotations=READ)
    @_safe
    async def describe_entity(
        entity: Annotated[str, Field(description="Entity set name, for example ORDERS or CUSTOMERS.")],
    ) -> dict[str, Any]:
        """Describe one entity: key fields, properties, and subforms from OData metadata.

        Call this before querying an unfamiliar form. Subforms are navigation
        properties (often named *_SUBFORM). Pass a subform name to get_subform,
        or expand it from query_entity or get_record. A unique case-insensitive
        match is accepted and reported in name_note; the canonical name is UPPERCASE.
        """

        return await current().describe_entity(entity)

    @mcp.tool(title="Query entity", annotations=READ)
    @_safe
    async def query_entity(
        entity: Annotated[str, Field(description="Entity set name, for example CUSTOMERS or ORDERS.")],
        filter: Annotated[
            str | None,
            Field(
                description=(
                    "OData $filter. Example: CUSTNAME eq '1001' and CURDATE ge 2025-01-01T00:00:00Z. "
                    "Strings are single-quoted. startswith(PARTNAME,'10') is supported."
                )
            ),
        ] = None,
        select: Annotated[
            str | None,
            Field(description="Comma-separated field names for $select, for example ORDNAME,CUSTNAME,TOTPRICE."),
        ] = None,
        expand: Annotated[
            str | None,
            Field(description="OData $expand. Example: ORDERITEMS_SUBFORM($select=PARTNAME,TQUANT,PRICE)."),
        ] = None,
        orderby: Annotated[
            str | None,
            Field(description="OData $orderby. Example: CURDATE desc."),
        ] = None,
        top: Annotated[
            int,
            Field(
                ge=1,
                le=5000,
                description="OData $top. Defaults to 50. Priority caps rows at MAXAPILINES (usually 2000).",
            ),
        ] = 50,
        skip: Annotated[int | None, Field(ge=0, description="OData $skip, for paging past earlier rows.")] = None,
    ) -> dict[str, Any]:
        """Query an entity set with OData $filter, $select, $expand, $orderby, $top, and $skip.

        This tool only reads. Prefer $select so responses stay small. Page with
        top and skip when next_link is present or count equals top.
        """

        return await current().query_entity(
            entity,
            filter=filter,
            select=select,
            expand=expand,
            orderby=orderby,
            top=top,
            skip=skip,
        )

    @mcp.tool(title="Get record", annotations=READ)
    @_safe
    async def get_record(
        entity: Annotated[str, Field(description="Entity set name, for example ORDERS.")],
        key: Annotated[
            str | None,
            Field(
                description=(
                    "Single key value, without quotes, for example SO26000001. "
                    "Composite keys can also be written as KEY=value pairs separated by commas. "
                    "Do not pass this together with keys."
                )
            ),
        ] = None,
        keys: Annotated[
            dict[str, str | int | float | bool] | None,
            Field(
                description=(
                    "Composite key object. Strings are quoted in the URL; numbers are not. "
                    "Example: {\"DEBIT\": \"D\", \"IVNUM\": \"T4862\"}."
                )
            ),
        ] = None,
        select: Annotated[str | None, Field(description="Comma-separated $select list.")] = None,
        expand: Annotated[str | None, Field(description="OData $expand of subforms on this record.")] = None,
    ) -> dict[str, Any]:
        """Get one record by its key, including composite keys.

        Use key for a single-field key. Use keys when describe_entity reports
        more than one key field. This tool only reads.
        """

        return await current().get_record(entity, key=key, keys=keys, select=select, expand=expand)

    @mcp.tool(title="Get subform", annotations=READ)
    @_safe
    async def get_subform(
        entity: Annotated[str, Field(description="Parent entity set, for example ORDERS.")],
        subform: Annotated[
            str,
            Field(description="Subform (navigation property) name, for example ORDERITEMS_SUBFORM."),
        ],
        key: Annotated[str | None, Field(description="Single parent key value, without quotes.")] = None,
        keys: Annotated[
            dict[str, str | int | float | bool] | None,
            Field(description="Composite parent key. See get_record."),
        ] = None,
        filter: Annotated[str | None, Field(description="OData $filter applied to the subform rows.")] = None,
        select: Annotated[str | None, Field(description="Comma-separated $select list for the subform.")] = None,
        expand: Annotated[str | None, Field(description="OData $expand of a nested subform.")] = None,
        orderby: Annotated[str | None, Field(description="OData $orderby for the subform rows.")] = None,
        top: Annotated[
            int,
            Field(ge=1, le=5000, description="OData $top. Defaults to 50."),
        ] = 50,
        skip: Annotated[int | None, Field(ge=0, description="OData $skip for the subform rows.")] = None,
    ) -> dict[str, Any]:
        """Read a subform of one parent record.

        Example: entity ORDERS, key SO26000001, subform ORDERITEMS_SUBFORM.
        A subform that is not a collection returns a single record instead of value.
        This tool only reads.
        """

        return await current().get_subform(
            entity,
            subform,
            key=key,
            keys=keys,
            filter=filter,
            select=select,
            expand=expand,
            orderby=orderby,
            top=top,
            skip=skip,
        )

    @mcp.tool(title="Create record", annotations=CREATE)
    @_safe
    async def create_record(
        entity: Annotated[str, Field(description="Entity set to insert into, for example ORDERS or CUSTOMERS.")],
        data: Annotated[
            dict[str, Any],
            Field(
                description=(
                    "JSON body of field values. Subform lines belong in the same object, "
                    "for example ORDERITEMS_SUBFORM: [{PARTNAME, TQUANT, PRICE}]."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Create a record with HTTP POST.

        This writes to Priority. The new record, and each subform line included
        in data, consumes a billed API transaction. Leave the public sandbox
        read-only. Field names are case-sensitive.
        """

        return await current().create_record(entity, data)

    @mcp.tool(title="Update record", annotations=MODIFY)
    @_safe
    async def update_record(
        entity: Annotated[str, Field(description="Entity set, for example ORDERS.")],
        data: Annotated[
            dict[str, Any],
            Field(description="Fields to change. Omitted fields stay as they are (HTTP PATCH)."),
        ],
        key: Annotated[str | None, Field(description="Single key value, or comma-separated KEY=value pairs.")] = None,
        keys: Annotated[
            dict[str, str | int | float | bool] | None,
            Field(description="Composite key object. See get_record."),
        ] = None,
    ) -> dict[str, Any]:
        """Update a record with HTTP PATCH.

        Only fields present in data change. This writes to Priority and consumes
        a billed API transaction, including each subform line in data. Leave the
        public sandbox read-only.
        """

        return await current().update_record(entity, data, key=key, keys=keys)

    @mcp.tool(title="Delete record", annotations=MODIFY)
    @_safe
    async def delete_record(
        entity: Annotated[str, Field(description="Entity set, for example ORDERS.")],
        key: Annotated[str | None, Field(description="Single key value, or comma-separated KEY=value pairs.")] = None,
        keys: Annotated[
            dict[str, str | int | float | bool] | None,
            Field(description="Composite key object. See get_record."),
        ] = None,
    ) -> dict[str, Any]:
        """Delete a record with HTTP DELETE.

        This permanently removes the record in Priority and consumes a billed
        API transaction. Leave the public sandbox read-only.
        """

        return await current().delete_record(entity, key=key, keys=keys)

    return mcp


_CLIENT: PriorityClient | None = None


def _shared_client() -> PriorityClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = PriorityClient.from_env()
    return _CLIENT


mcp = create_server()
