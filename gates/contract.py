"""Checks lines against the log contract's JSON schemas (docs/contracts/, ADR-024, ADR-036).

The schemas are JSON Schema (2020-12), so the observability team can use them with their own
tools. This checker covers only the keywords those files use, with the Python standard library,
so the gate needs no new package; a schema using any other keyword is refused, never ignored.
"""

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
CONTRACTS = os.path.join(os.path.dirname(HERE), "docs", "contracts")
ANNOTATIONS = {"$schema", "title", "description"}
KEYWORDS = ANNOTATIONS | {"type", "required", "properties", "additionalProperties", "items",
                          "pattern", "enum", "const", "minimum"}
TYPES = {"object": dict, "array": list, "string": str, "null": type(None), "boolean": bool}


# The schema of each row of an aggregated file, by its report_type (contract v2).
ROW_SCHEMAS = {"summary": "summary", "resource": "resource", "principal": "principal",
               "app_dropped": "app-dropped"}


def load(name):
    """`processed`, or a name in ROW_SCHEMAS: docs/contracts/polaris-logs-<name>.schema.json."""
    with open(os.path.join(CONTRACTS, f"polaris-logs-{name}.schema.json")) as f:
        return json.load(f)


def row_problems(row):
    """A row of an aggregated file against the schema its report_type names."""
    kind = row.get("report_type") if isinstance(row, dict) else None
    if kind not in ROW_SCHEMAS:
        return [f"report_type: {kind!r} is no row type of the contract"]
    return problems(row, load(ROW_SCHEMAS[kind]))


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


def check_lines(text, schema):
    """JSON lines against `schema` -> (lines that hold, ['line N: problem', ...])."""
    good, bad = 0, []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            bad.append(f"line {n}: not JSON")
            continue
        found = problems(doc, schema)
        if found:
            bad += [f"line {n}: {p}" for p in found]
        else:
            good += 1
    return good, bad


# Contract v3 (ADR-081): three line and aggregate schemas, and the JSON files OpenSearch takes.
def load_v3(name):
    """docs/contracts/polaris-logs-v3-<name>.schema.json for `candidate-line`, `error-line` and
    `aggregate`; polaris-logs-v3-<name>.json for the templates, ISM policies, transform and
    routing. FileNotFoundError for any other name."""
    base = os.path.join(CONTRACTS, f"polaris-logs-v3-{name}")
    path = base + ".schema.json" if name in V3_SCHEMAS else base + ".json"
    with open(path) as f:
        return json.load(f)


V3_SCHEMAS = ("candidate-line", "error-line", "aggregate")


def templates():
    return {kind: load_v3(f"{kind}-template") for kind in ("candidate", "error", "aggregate")}


def ism_policies():
    return {kind: load_v3(f"{kind}-ism") for kind in ("candidate", "error")}


def transform():
    return load_v3("transform")


def routing():
    return load_v3("routing")


def routing_examples():
    return routing()["examples"]


def kept_examples():
    """The examples the sidecar keeps: the ones with an output record."""
    return [e for e in routing_examples() if e["output"] is not None]


def line_problems(output):
    """A record the main Fluent Bit receives against the schema its index_class names."""
    kind = output.get("index_class") if isinstance(output, dict) else None
    if kind not in ("candidate", "error"):
        return [f"index_class: {kind!r} is no index class of the contract"]
    return problems(output, load_v3(f"{kind}-line"))


def aggregate_problems(doc):
    """An aggregate document against its schema. `response_bytes` as a float with no fraction
    (OpenSearch's `sum` may write 360.0) is the integer JSON Schema 2020-12 calls it; 360.5 still
    fails, and so does a float in any other field."""
    value = doc.get("response_bytes") if isinstance(doc, dict) else None
    if isinstance(value, float) and value.is_integer():
        doc = {**doc, "response_bytes": int(value)}
    return problems(doc, load_v3("aggregate"))
