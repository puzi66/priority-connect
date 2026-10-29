"""OData URL helpers and CSDL ($metadata) parsing for Priority."""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

from priority_mcp.errors import PriorityError

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INT = re.compile(r"-?\d+")
_DECIMAL = re.compile(r"-?\d+\.\d+")
# Keep OData punctuation intact. Encode everything else (spaces, '#', '+', ...).
_QUERY_SAFE = "$,()'=/:"


def validate_ident(name: str, what: str) -> str:
    """Reject anything that is not a Priority form or field name."""

    cleaned = name.strip()
    if not cleaned or len(cleaned) > 128 or not _IDENT.fullmatch(cleaned):
        raise PriorityError(
            f"Invalid {what} {name!r}. Use the Priority name (letters, digits, underscore; usually UPPERCASE)."
        )
    return cleaned


def odata_string(value: str) -> str:
    """Format a string as an OData literal, URL-encoded inside the quotes.

    Single quotes are doubled first, which is the OData escape. Every other
    reserved character, including ``)`` and ``/``, is percent-encoded so a key
    cannot break out of the path segment.
    """

    if len(value) > 500:
        raise PriorityError("Key value is too long (limit 500 characters).")
    escaped = value.replace("'", "''")
    return "'" + urllib.parse.quote(escaped, safe="'") + "'"


def odata_literal(value: Any) -> str:
    """Format a key value. Numbers and booleans stay bare; strings are quoted."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # NaN / infinity
            raise PriorityError("Key values must be finite numbers.")
        return format(value, "g")
    if isinstance(value, str):
        return odata_string(value)
    raise PriorityError(
        f"Unsupported key value {value!r}. Use a string, number, or boolean, or pass keys as an object."
    )


def format_key(*, key: str | None = None, keys: dict[str, Any] | None = None) -> str:
    """Build the OData key segment, including the surrounding parentheses.

    ``key`` is a single value (``SO26000001``) or ``KEY=value`` pairs separated
    by commas. ``keys`` is the composite-key object. Numbers in ``KEY=value``
    text stay unquoted; pass a JSON number in ``keys`` for the same effect.
    """

    if key is not None and keys is not None:
        raise PriorityError("Pass either key or keys, not both.")
    if keys is not None:
        if not keys:
            raise PriorityError("keys is empty. Provide each field of the composite key.")
        parts: list[str] = []
        for name, value in keys.items():
            field = validate_ident(str(name), "key field")
            if value is None:
                raise PriorityError(f"Key field {field} is null.")
            parts.append(f"{field}={odata_literal(value)}")
        return "(" + ",".join(parts) + ")"
    if key is None or key.strip() == "":
        raise PriorityError("A key is required. Pass key for a single key, or keys for a composite key.")
    text = key.strip()
    if "=" not in text:
        return "(" + odata_string(text) + ")"
    parts = []
    for piece in text.split(","):
        piece = piece.strip()
        if "=" not in piece:
            raise PriorityError(
                f"Invalid composite key segment {piece!r}. Use KEY=value pairs separated by commas, or pass keys."
            )
        name, raw_value = piece.split("=", 1)
        field = validate_ident(name, "key field")
        raw_value = raw_value.strip()
        if _INT.fullmatch(raw_value) or _DECIMAL.fullmatch(raw_value) or raw_value in {"true", "false"}:
            literal = raw_value
        else:
            literal = odata_string(raw_value)
        parts.append(f"{field}={literal}")
    return "(" + ",".join(parts) + ")"


def encode_query(options: dict[str, str]) -> str:
    """Encode OData query options. Empty dict returns an empty string."""

    if not options:
        return ""
    return "?" + urllib.parse.urlencode(options, quote_via=urllib.parse.quote, safe=_QUERY_SAFE)


def query_options(
    *,
    filter: str | None = None,
    select: str | None = None,
    expand: str | None = None,
    orderby: str | None = None,
    top: int | None = None,
    skip: int | None = None,
) -> str:
    options: dict[str, str] = {}
    if filter:
        options["$filter"] = filter
    if select:
        options["$select"] = select
    if expand:
        options["$expand"] = expand
    if orderby:
        options["$orderby"] = orderby
    if top is not None:
        options["$top"] = str(top)
    if skip is not None:
        options["$skip"] = str(skip)
    return encode_query(options)


def entity_sets_from_service_document(document: dict[str, Any]) -> list[str]:
    """Names of entity sets in an OData service document."""

    value = document.get("value")
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind not in (None, "EntitySet"):
            continue
        name = item.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return sorted(set(names))


def suggest_entities(requested: str, names: list[str], limit: int = 8) -> list[str]:
    """Entity names a model might have meant. Prefix matches come first."""

    query = requested.lower()
    if not query:
        return []
    starts = [name for name in names if name.lower().startswith(query)]
    contains = [name for name in names if query in name.lower() and name not in starts]
    return (starts + contains)[:limit]


def unknown_entity_message(requested: str, names: list[str]) -> str:
    suggestions = suggest_entities(requested, names)
    message = f"Entity {requested!r} was not found. Names are case-sensitive and are usually UPPERCASE."
    if suggestions:
        message += " Did you mean: " + ", ".join(suggestions) + "?"
    return message


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _annotation(element: ET.Element, term_suffix: str) -> ET.Element | None:
    for child in element:
        if _local(child.tag) != "Annotation":
            continue
        term = child.attrib.get("Term", "")
        if term == term_suffix or term.endswith("." + term_suffix):
            return child
    return None


def _annotation_string(element: ET.Element, term_suffix: str) -> str | None:
    ann = _annotation(element, term_suffix)
    if ann is None:
        return None
    if "String" in ann.attrib:
        return ann.attrib["String"]
    return None


def _annotation_bool(element: ET.Element, term_suffix: str) -> bool:
    ann = _annotation(element, term_suffix)
    if ann is None:
        return False
    return ann.attrib.get("Bool", "").lower() == "true"


def _is_read_only(element: ET.Element) -> bool:
    ann = _annotation(element, "Permissions")
    if ann is None:
        return False
    members = [node.text.strip() for node in ann.iter() if _local(node.tag) == "EnumMember" and node.text]
    return bool(members) and all(member.endswith("/Read") for member in members)


def _optional_int(value: str | None) -> int | None:
    if value is None or not value.isdigit():
        return None
    return int(value)


def _parse_property(element: ET.Element) -> dict[str, Any]:
    field: dict[str, Any] = {
        "name": element.attrib["Name"],
        "type": element.attrib.get("Type", ""),
    }
    if element.attrib.get("Nullable", "true").lower() == "false":
        field["nullable"] = False
    max_length = _optional_int(element.attrib.get("MaxLength"))
    if max_length is not None:
        field["max_length"] = max_length
    precision = _optional_int(element.attrib.get("Precision"))
    if precision is not None:
        field["precision"] = precision
    scale = _optional_int(element.attrib.get("Scale"))
    if scale is not None:
        field["scale"] = scale
    description = _annotation_string(element, "Description")
    if description:
        field["description"] = description
    date_type = _annotation_string(element, "DateType")
    if date_type:
        field["date_type"] = date_type
    if _is_read_only(element):
        field["read_only"] = True
    if _annotation_bool(element, "Mandatory"):
        field["mandatory"] = True
    if _annotation_bool(element, "AutoUnique"):
        field["auto_unique"] = True
    return field


def _parse_navigation(element: ET.Element) -> dict[str, Any]:
    type_name = element.attrib.get("Type", "")
    collection = type_name.startswith("Collection(") and type_name.endswith(")")
    inner = type_name[len("Collection(") : -1] if collection else type_name
    nav: dict[str, Any] = {
        "name": element.attrib["Name"],
        "type": inner.split(".")[-1] if inner else type_name,
        "collection": collection,
    }
    if element.attrib.get("ContainsTarget", "").lower() == "true":
        nav["contains_target"] = True
    description = _annotation_string(element, "Description")
    if description:
        nav["description"] = description
    return nav


class Metadata:
    """Entity sets and entity types parsed from an OData CSDL document."""

    def __init__(self, xml_text: str) -> None:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise PriorityError(f"Priority returned metadata that is not valid XML: {exc}") from exc
        self.entity_types: dict[str, ET.Element] = {}
        self.entity_sets: dict[str, str] = {}
        for element in root.iter():
            kind = _local(element.tag)
            if kind == "EntityType" and "Name" in element.attrib:
                self.entity_types[element.attrib["Name"]] = element
            elif kind == "EntitySet" and "Name" in element.attrib:
                target = element.attrib.get("EntityType", "")
                self.entity_sets[element.attrib["Name"]] = target.split(".")[-1]

    def describe(self, name: str) -> dict[str, Any]:
        """Fields, keys, and subforms for one entity set or entity type."""

        requested = name.strip()
        set_name: str | None
        type_name: str | None
        note: str | None = None
        if requested in self.entity_sets:
            set_name = requested
            type_name = self.entity_sets[requested]
        elif requested in self.entity_types:
            set_name = next((item for item, target in self.entity_sets.items() if target == requested), None)
            type_name = requested
        else:
            set_matches = [item for item in self.entity_sets if item.lower() == requested.lower()]
            type_matches = [item for item in self.entity_types if item.lower() == requested.lower()]
            if len(set_matches) == 1:
                set_name = set_matches[0]
                type_name = self.entity_sets[set_name]
                note = f"Requested {requested!r}; using canonical entity set {set_name}."
            elif len(set_matches) > 1:
                raise PriorityError(
                    f"Entity name {requested!r} is ambiguous: {', '.join(set_matches)}.",
                    hint="Names are case-sensitive. Pass the canonical entity set name.",
                )
            elif len(type_matches) == 1:
                type_name = type_matches[0]
                set_name = next((item for item, target in self.entity_sets.items() if target == type_name), None)
                note = f"Requested {requested!r}; using canonical entity type {type_name}."
            else:
                known = sorted(set(self.entity_sets) | set(self.entity_types))
                raise PriorityError(unknown_entity_message(requested, known), status=404)
        if not type_name or type_name not in self.entity_types:
            raise PriorityError(
                f"Entity set {set_name or requested} points at unknown type {type_name!r}.",
                status=404,
            )
        lineage = self._lineage(type_name)
        keys = self._keys(lineage)
        fields = self._fields(lineage)
        subforms = self._subforms(lineage)
        described: dict[str, Any] = {
            "entity_type": type_name,
            "keys": keys,
            "fields": fields,
            "subforms": subforms,
        }
        if set_name:
            described["entity_set"] = set_name
        if note:
            described["name_note"] = note
        return described

    def _lineage(self, type_name: str) -> list[ET.Element]:
        elements: list[ET.Element] = []
        seen: set[str] = set()
        current = type_name
        while current and current not in seen:
            seen.add(current)
            element = self.entity_types.get(current)
            if element is None:
                break
            elements.append(element)
            base = element.attrib.get("BaseType", "")
            current = base.split(".")[-1] if base else ""
        elements.reverse()
        return elements

    def _keys(self, lineage: list[ET.Element]) -> list[str]:
        keys: list[str] = []
        for element in lineage:
            for child in element:
                if _local(child.tag) != "Key":
                    continue
                for ref in child:
                    if _local(ref.tag) == "PropertyRef" and "Name" in ref.attrib:
                        name = ref.attrib["Name"]
                        if name not in keys:
                            keys.append(name)
        return keys

    def _fields(self, lineage: list[ET.Element]) -> list[dict[str, Any]]:
        order: list[str] = []
        fields: dict[str, dict[str, Any]] = {}
        for element in lineage:
            for child in element:
                if _local(child.tag) != "Property" or "Name" not in child.attrib:
                    continue
                parsed = _parse_property(child)
                if parsed["name"] not in fields:
                    order.append(parsed["name"])
                fields[parsed["name"]] = parsed
        return [fields[name] for name in order]

    def _subforms(self, lineage: list[ET.Element]) -> list[dict[str, Any]]:
        order: list[str] = []
        navs: dict[str, dict[str, Any]] = {}
        for element in lineage:
            for child in element:
                if _local(child.tag) != "NavigationProperty" or "Name" not in child.attrib:
                    continue
                parsed = _parse_navigation(child)
                if parsed["name"] not in navs:
                    order.append(parsed["name"])
                navs[parsed["name"]] = parsed
        return [navs[name] for name in order]


def slim(value: Any) -> Any:
    """Drop OData control properties the model does not need."""

    if isinstance(value, list):
        return [slim(item) for item in value]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key.startswith("@odata."):
                continue
            cleaned[key] = slim(item)
        return cleaned
    return value
