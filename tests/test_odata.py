"""OData key formatting, query encoding, and metadata parsing."""

from priority_mcp.errors import PriorityError, extract_api_message
from priority_mcp.odata import (
    Metadata,
    entity_sets_from_service_document,
    format_key,
    query_options,
    suggest_entities,
    unknown_entity_message,
)

SAMPLE_METADATA = """<?xml version="1.0" encoding="UTF-8"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">
  <edmx:DataServices>
    <Schema Namespace="Priority.OData" xmlns="http://docs.oasis-open.org/odata/ns/edm">
      <EntityType Name="ORDERS">
        <Key><PropertyRef Name="ORDNAME"/></Key>
        <Property Name="ORDNAME" Type="Edm.String" Nullable="false" MaxLength="16">
          <Annotation Term="Priority.OData.Description" String="Order"/>
          <Annotation Term="Priority.OData.Mandatory" Bool="true"/>
        </Property>
        <Property Name="CURDATE" Type="Edm.DateTimeOffset">
          <Annotation Term="Priority.OData.DateType" String="Date"/>
          <Annotation Term="Priority.OData.Description" String="Date"/>
        </Property>
        <Property Name="TOTPRICE" Type="Edm.Decimal" Precision="16" Scale="2"/>
        <Property Name="POSITIONDES" Type="Edm.String" MaxLength="32">
          <Annotation Term="Org.OData.Core.V1.Permissions">
            <EnumMember>Org.OData.Core.V1.Permission/Read</EnumMember>
          </Annotation>
        </Property>
        <NavigationProperty Name="ORDERITEMS_SUBFORM" Type="Collection(Priority.OData.ORDERITEMS)" ContainsTarget="true">
          <Annotation Term="Priority.OData.Description" String="Lines"/>
        </NavigationProperty>
        <NavigationProperty Name="SHIPTO2_SUBFORM" Type="Priority.OData.SHIPTO2" ContainsTarget="true"/>
      </EntityType>
      <EntityType Name="CHILD" BaseType="Priority.OData.ORDERS">
        <Property Name="EXTRA" Type="Edm.String"/>
      </EntityType>
      <EntityContainer Name="Default">
        <EntitySet Name="ORDERS" EntityType="Priority.OData.ORDERS"/>
        <EntitySet Name="CHILD" EntityType="Priority.OData.CHILD"/>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>
"""


def test_single_and_composite_keys():
    assert format_key(key="SO26000001") == "('SO26000001')"
    assert format_key(key="#098") == "('%23098')"
    assert format_key(key="O'Brien") == "('O''Brien')"
    assert format_key(key="a')") == "('a''%29')"
    assert format_key(key="A B") == "('A%20B')"
    assert format_key(key="DEBIT=D,IVNUM=T4862") == "(DEBIT='D',IVNUM='T4862')"
    assert format_key(key="KLINE=3, TYPE=A") == "(KLINE=3,TYPE='A')"
    assert format_key(keys={"DEBIT": "D", "IVNUM": "T4862"}) == "(DEBIT='D',IVNUM='T4862')"
    assert format_key(keys={"KLINE": 3, "OPEN": True}) == "(KLINE=3,OPEN=true)"


def test_key_rejects_injection_and_missing_values():
    try:
        format_key(key=None, keys={})
    except PriorityError as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("expected empty keys to fail")
    try:
        format_key(key="SO1", keys={"ORDNAME": "SO1"})
    except PriorityError as exc:
        assert "not both" in str(exc)
    else:
        raise AssertionError("expected both key forms to fail")
    try:
        format_key(key="ORDNAME")  # no equals, treated as a literal key, which is valid
    except PriorityError:
        raise AssertionError("a single key without '=' is a literal")
    try:
        format_key(keys={"BAD NAME": "x"})
    except PriorityError as exc:
        assert "key field" in str(exc)
    else:
        raise AssertionError("expected an invalid key field to fail")


def test_query_encoding_preserves_odata_punctuation():
    query = query_options(
        filter="CUSTNAME eq 'C1' and CURDATE ge 2025-01-01T00:00:00+02:00",
        select="ORDNAME,TOTPRICE",
        expand="ORDERITEMS_SUBFORM($select=PARTNAME,TQUANT)",
        orderby="CURDATE desc",
        top=5,
        skip=10,
    )
    assert query.startswith("?")
    assert "$filter=CUSTNAME%20eq%20'C1'%20and%20CURDATE%20ge%202025-01-01T00:00:00%2B02:00" in query
    assert "$select=ORDNAME,TOTPRICE" in query
    assert "$expand=ORDERITEMS_SUBFORM($select=PARTNAME,TQUANT)" in query
    assert "$orderby=CURDATE%20desc" in query
    assert "$top=5" in query
    assert "$skip=10" in query
    assert query_options() == ""


def test_metadata_describes_fields_subforms_and_base_type():
    meta = Metadata(SAMPLE_METADATA)
    orders = meta.describe("ORDERS")
    assert orders["entity_set"] == "ORDERS"
    assert orders["entity_type"] == "ORDERS"
    assert orders["keys"] == ["ORDNAME"]
    by_name = {field["name"]: field for field in orders["fields"]}
    assert by_name["ORDNAME"]["nullable"] is False
    assert by_name["ORDNAME"]["max_length"] == 16
    assert by_name["ORDNAME"]["mandatory"] is True
    assert by_name["ORDNAME"]["description"] == "Order"
    assert by_name["CURDATE"]["date_type"] == "Date"
    assert by_name["TOTPRICE"]["precision"] == 16
    assert by_name["TOTPRICE"]["scale"] == 2
    assert by_name["POSITIONDES"]["read_only"] is True
    subforms = {item["name"]: item for item in orders["subforms"]}
    assert subforms["ORDERITEMS_SUBFORM"]["collection"] is True
    assert subforms["ORDERITEMS_SUBFORM"]["type"] == "ORDERITEMS"
    assert subforms["ORDERITEMS_SUBFORM"]["contains_target"] is True
    assert subforms["SHIPTO2_SUBFORM"]["collection"] is False

    folded = meta.describe("orders")
    assert folded["entity_set"] == "ORDERS"
    assert "canonical" in folded["name_note"]

    child = meta.describe("CHILD")
    assert child["keys"] == ["ORDNAME"]
    assert [field["name"] for field in child["fields"]][-1] == "EXTRA"
    assert any(item["name"] == "ORDERITEMS_SUBFORM" for item in child["subforms"])


def test_unknown_entity_suggestions_and_service_document():
    names = entity_sets_from_service_document(
        {
            "value": [
                {"name": "ORDERS", "kind": "EntitySet"},
                {"name": "PORDERS", "kind": "EntitySet"},
                {"name": "GetVersion", "kind": "FunctionImport"},
                {"name": "CUSTOMERS", "kind": "EntitySet"},
            ]
        }
    )
    assert names == ["CUSTOMERS", "ORDERS", "PORDERS"]
    assert suggest_entities("ord", names) == ["ORDERS", "PORDERS"]
    message = unknown_entity_message("ORD", names)
    assert "ORDERS" in message
    assert "case-sensitive" in message
    try:
        Metadata(SAMPLE_METADATA).describe("NOPE")
    except PriorityError as exc:
        assert exc.status == 404
        assert "NOPE" in str(exc)
    else:
        raise AssertionError("expected a missing entity to fail")


def test_error_body_parsing():
    assert extract_api_message('{"error":{"code":"400","message":"bad filter"}}') == "400: bad filter"
    assert extract_api_message('{"odata.error":{"message":{"value":"nope"}}}') == "nope"
    assert extract_api_message("plain text failure") == "plain text failure"
    assert extract_api_message("") == ""
