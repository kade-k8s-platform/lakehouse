"""Checks a document against a JSON schema of the log contract (docs/contracts/): contract v3's
schemas (ADR-081, ADR-082).

The schemas are JSON Schema (2020-12), so the observability team can use them with their own
tools. This checker covers only the keywords those files use, with the Python standard library,
so the gate needs no new package; a schema using any other keyword is refused, never ignored.
"""

import re

ANNOTATIONS = {"$schema", "title", "description"}
KEYWORDS = ANNOTATIONS | {"type", "required", "properties", "additionalProperties", "items",
                          "pattern", "enum", "const", "minimum"}
TYPES = {"object": dict, "array": list, "string": str, "null": type(None), "boolean": bool}


def _is(value, kind):
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, TYPES[kind])


def problems(value, schema, at=""):
    """Every way `value` breaks `schema`, as `path: problem` strings; [] when it holds."""
    unknown = set(schema) - KEYWORDS
    if unknown:
        raise ValueError(f"{at or 'schema'}: keywords this checker does not know: "
                         f"{sorted(unknown)}")
    name = at or "value"
    if "type" in schema:
        kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is(value, k) for k in kinds):
            return [f"{name}: expected {' or '.join(kinds)}"]
    if "const" in schema and value != schema["const"]:
        return [f"{name}: expected {schema['const']!r}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{name}: not one of {schema['enum']}"]
    out = []
    if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
        out.append(f"{name}: does not match {schema['pattern']}")
    if "minimum" in schema and _is(value, "integer") and value < schema["minimum"]:
        out.append(f"{name}: below {schema['minimum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                out.append(f"{_join(at, key)}: required")
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                out += problems(item, props[key], _join(at, key))
            elif extra is False:
                out.append(f"{_join(at, key)}: not allowed")
            elif isinstance(extra, dict):
                out += problems(item, extra, _join(at, key))
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            out += problems(item, schema["items"], f"{name}[{i}]" if at else f"[{i}]")
    return out


def _join(at, key):
    return f"{at}.{key}" if at else key
