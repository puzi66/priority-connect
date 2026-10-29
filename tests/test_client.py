"""Priority HTTP client: auth, errors, 429 backoff, and read tools."""

import base64

import pytest

from priority_mcp.client import PriorityClient, _parse_retry_after
from priority_mcp.errors import PriorityError
from tests.support import ScriptedApi
from tests.test_odata import SAMPLE_METADATA

SERVICE = {"value": [{"name": "ORDERS", "kind": "EntitySet"}, {"name": "CUSTOMERS", "kind": "EntitySet"}]}


def _auth(request) -> str:
    raw = request.headers["authorization"]
    scheme, token = raw.split(" ", 1)
    assert scheme == "Basic"
    return base64.b64decode(token).decode()


@pytest.mark.asyncio
async def test_basic_auth_app_headers_and_pat():
    script = ScriptedApi([(200, {"value": [{"name": "CUSTOMERS", "kind": "EntitySet"}]})])
    script.client.app_id = "APP001"
    script.client.app_key = "key-1"
    await script.client.aclose()
    script.client._http_client = None
    # Rebuild headers by forcing a new client after the credentials were set.
    script.client.app_id = "APP001"
    script.client.app_key = "key-1"
    await script.client.entity_names()
    request = script.calls[0]
    assert _auth(request) == "api:secret"
    assert request.headers["x-app-id"] == "APP001"
    assert request.headers["x-app-key"] == "key-1"
    assert request.headers["user-agent"].startswith("priority-mcp/")
    assert "secret" not in str(request.url)

    pat = ScriptedApi(
        [(200, {"value": [{"name": "CUSTOMERS", "kind": "EntitySet"}]})],
        username="token-value",
        password="PAT",
        app_id=None,
        app_key=None,
    )
    await pat.client.entity_names()
    assert _auth(pat.calls[0]) == "token-value:PAT"


@pytest.mark.asyncio
async def test_retries_429_with_backoff_then_succeeds():
    script = ScriptedApi(
        [
            (429, ({"error": {"code": "429", "message": "slow down"}}, {"Retry-After": "3"})),
            (200, {"value": [{"CUSTNAME": "1001"}]}),
        ],
        max_retries=4,
        retry_base=1,
    )
    result = await script.client.query_entity("CUSTOMERS", select="CUSTNAME", top=1)
    assert result["value"] == [{"CUSTNAME": "1001"}]
    assert script.delays == [3.0]
    assert len(script.calls) == 2


@pytest.mark.asyncio
async def test_429_exhausted_is_a_clear_error():
    script = ScriptedApi(
        [
            (429, {"error": {"code": "429", "message": "slow down"}}),
            (429, {"error": {"code": "429", "message": "slow down"}}),
            (429, {"error": {"code": "429", "message": "slow down"}}),
        ],
        max_retries=2,
        retry_base=1,
    )
    with pytest.raises(PriorityError) as caught:
        await script.client.query_entity("CUSTOMERS")
    text = str(caught.value)
    assert caught.value.status == 429
    assert "HTTP 429" in text
    assert "Retried 2 time(s)" in text
    assert "100 calls" in text
    assert len(script.calls) == 3
    assert script.delays == [1.0, 2.0]


@pytest.mark.asyncio
async def test_http_errors_include_hints_and_are_not_retried():
    script = ScriptedApi([(401, b"")])
    with pytest.raises(PriorityError) as caught:
        await script.client.query_entity("CUSTOMERS")
    assert caught.value.status == 401
    assert "PRIORITY_PAT" in str(caught.value)
    assert len(script.calls) == 1

    missing = ScriptedApi([(404, b"")])
    with pytest.raises(PriorityError) as caught:
        await missing.client.get_record("ORDERS", key="SO1")
    assert "case-sensitive" in str(caught.value)

    bad = ScriptedApi([(400, {"error": {"code": "400", "message": "bad filter"}})])
    with pytest.raises(PriorityError) as caught:
        await bad.client.query_entity("CUSTOMERS", filter="nope")
    assert "400: bad filter" in str(caught.value)
    assert "$filter" in str(caught.value)

    conflict = ScriptedApi([(409, {"error": {"code": "409", "message": "exists"}})])
    with pytest.raises(PriorityError) as caught:
        await conflict.client.create_record("CUSTOMERS", {"CUSTNAME": "1"})
    assert "already exists" in str(caught.value)


@pytest.mark.asyncio
async def test_query_get_and_subform_urls():
    script = ScriptedApi(
        [
            (200, {"@odata.context": "meta", "value": [{"ORDNAME": "SO1"}], "@odata.nextLink": "https://next"}),
            (200, {"@odata.context": "meta", "ORDNAME": "SO1", "CUSTNAME": "C1"}),
            (
                200,
                {
                    "@odata.context": "meta",
                    "value": [{"PARTNAME": "10006"}],
                },
            ),
        ]
    )
    listed = await script.client.query_entity(
        "ORDERS",
        filter="CUSTNAME eq 'C1'",
        select="ORDNAME",
        expand="ORDERITEMS_SUBFORM($select=PARTNAME)",
        orderby="CURDATE desc",
        top=5,
        skip=10,
    )
    assert listed["count"] == 1
    assert listed["next_link"].endswith("next")
    assert "@odata.context" not in listed["value"][0]
    path = str(script.calls[0].url)
    assert "/ORDERS?" in path
    assert "$filter=CUSTNAME%20eq%20'C1'" in path
    assert "$top=5" in path
    assert "$skip=10" in path

    record = await script.client.get_record("ORDERS", key="#098", select="ORDNAME,CUSTNAME")
    assert record["record"]["ORDNAME"] == "SO1"
    assert "@odata.context" not in record["record"]
    assert "ORDERS('%23098')" in str(script.calls[1].url)

    sub = await script.client.get_subform(
        "ORDERS",
        "ORDERITEMS_SUBFORM",
        keys={"DEBIT": "D", "IVNUM": "T4862"},
        select="PARTNAME",
        top=2,
    )
    assert sub["subform"] == "ORDERITEMS_SUBFORM"
    assert sub["value"] == [{"PARTNAME": "10006"}]
    assert "ORDERS(DEBIT='D',IVNUM='T4862')/ORDERITEMS_SUBFORM" in str(script.calls[2].url)


@pytest.mark.asyncio
async def test_writes_send_the_right_method_and_read_only_blocks_them():
    script = ScriptedApi(
        [
            (201, {"@odata.context": "meta", "ORDNAME": "SO9"}),
            (200, {"ORDNAME": "SO9", "ORDSTATUSDES": "Draft"}),
            (201, b""),
        ]
    )
    created = await script.client.create_record(
        "ORDERS",
        {"CUSTNAME": "C1", "ORDERITEMS_SUBFORM": [{"PARTNAME": "10006", "TQUANT": 1}]},
    )
    assert created["record"]["ORDNAME"] == "SO9"
    assert script.calls[0].method == "POST"
    assert script.calls[0].headers["prefer"] == "return=representation"
    assert b"ORDERITEMS_SUBFORM" in script.calls[0].content

    updated = await script.client.update_record("ORDERS", {"ORDSTATUSDES": "Draft"}, key="SO9")
    assert updated["record"]["ORDSTATUSDES"] == "Draft"
    assert script.calls[1].method == "PATCH"

    deleted = await script.client.delete_record("ORDERS", key="SO9")
    assert deleted["deleted"] is True
    assert deleted["status"] == 201
    assert script.calls[2].method == "DELETE"

    blocked = ScriptedApi([], read_only=True)
    with pytest.raises(PriorityError) as caught:
        await blocked.client.create_record("ORDERS", {"CUSTNAME": "C1"})
    assert "read-only" in str(caught.value)
    assert blocked.calls == []
    with pytest.raises(PriorityError):
        await blocked.client.delete_record("ORDERS", key="SO1")
    assert blocked.calls == []


@pytest.mark.asyncio
async def test_entity_catalog_metadata_and_unknown_name():
    script = ScriptedApi(
        [
            (200, SERVICE),
            (200, SAMPLE_METADATA),
            (500, {"error": {"code": "500", "message": "entity NOPE not found: XML decode: EOF"}}),
        ]
    )
    listed = await script.client.list_entities(grep="cust")
    assert listed == {"count": 1, "entities": ["CUSTOMERS"]}
    again = await script.client.list_entities()
    assert again["count"] == 2
    assert len(script.calls) == 1

    described = await script.client.describe_entity("orders")
    assert described["keys"] == ["ORDNAME"]
    assert described["name_note"].startswith("Requested 'orders'")
    assert "GetMetadataFor(entity='ORDERS')" in str(script.calls[1].url)
    cached = await script.client.describe_entity("ORDERS")
    assert "name_note" not in cached
    assert len(script.calls) == 2

    with pytest.raises(PriorityError) as caught:
        await script.client.describe_entity("NOPE")
    assert "not found" in str(caught.value).lower()
    assert "XML decode" not in str(caught.value)
    assert all("$metadata" not in str(call.url) for call in script.calls)


@pytest.mark.asyncio
async def test_metadata_function_404_falls_back_to_full_document():
    script = ScriptedApi(
        [
            (200, SERVICE),
            (404, {"error": {"code": "404", "message": "no such function"}}),
            (200, SAMPLE_METADATA),
        ]
    )
    described = await script.client.describe_entity("ORDERS")
    assert described["entity_set"] == "ORDERS"
    assert str(script.calls[2].url).endswith("/$metadata")


@pytest.mark.asyncio
async def test_rejects_path_injection_and_oversized_responses():
    script = ScriptedApi([])
    with pytest.raises(PriorityError):
        await script.client.query_entity("../CUSTOMERS")
    with pytest.raises(PriorityError):
        await script.client.get_subform("ORDERS", "ORDERITEMS_SUBFORM/SECRET", key="SO1")
    assert script.calls == []

    huge = ScriptedApi([(200, b"x" * 50)], max_response_bytes=16)
    with pytest.raises(PriorityError) as caught:
        await huge.client.query_entity("CUSTOMERS")
    assert "exceeded" in str(caught.value)
    assert "$top" in str(caught.value)


def test_retry_after_http_date_and_env(monkeypatch):
    import email.utils
    import time

    parsed = _parse_retry_after(email.utils.formatdate(time.time() + 10, usegmt=True))
    assert parsed is not None and 8 <= parsed <= 12
    assert _parse_retry_after("nope") is None
    assert _parse_retry_after(None) is None

    monkeypatch.delenv("PRIORITY_URL", raising=False)
    with pytest.raises(PriorityError) as caught:
        PriorityClient.from_env()
    assert "PRIORITY_URL" in str(caught.value)

    monkeypatch.setenv("PRIORITY_URL", "https://erp.example/odata/Priority/tabula.ini/demo")
    monkeypatch.delenv("PRIORITY_PAT", raising=False)
    monkeypatch.delenv("PRIORITY_USER", raising=False)
    monkeypatch.delenv("PRIORITY_PASSWORD", raising=False)
    with pytest.raises(PriorityError) as caught:
        PriorityClient.from_env()
    assert "PRIORITY_PAT" in str(caught.value)

    monkeypatch.setenv("PRIORITY_PAT", "token-1")
    monkeypatch.setenv("PRIORITY_READ_ONLY", "yes")
    monkeypatch.setenv("PRIORITY_APP_ID", "APP001")
    client = PriorityClient.from_env()
    assert client.username == "token-1"
    assert client.password == "PAT"
    assert client.read_only is True
    assert client.app_id == "APP001"
    assert "token-1" not in str(caught.value)


@pytest.mark.asyncio
async def test_redirect_is_not_followed():
    script = ScriptedApi([(302, b"",)])
    # 302 with empty body. Response constructor above uses content.
    with pytest.raises(PriorityError) as caught:
        await script.client.entity_names()
    assert caught.value.status == 302
    assert "not followed" in str(caught.value)
    assert len(script.calls) == 1
