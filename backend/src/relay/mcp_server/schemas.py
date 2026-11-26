"""Tool input schemas that strict tool use accepts (PLAN.md §6.4).

The tools' arguments are Pydantic models, and Pydantic enforces every constraint on the server.
The schema we *publish* is a simpler copy that fits the JSON Schema subset the Claude API
compiles for `strict: true` (docs: "Structured outputs", "JSON Schema limitations"):

- every object sets `additionalProperties: false`;
- no length, range or item-count limits (`minItems` 0 or 1 is allowed);
- no `oneOf` or `not`, and `anyOf` never sits next to `type` or `properties`;
- only the listed string formats.

`strict_input_schema` does the conversion; `strict_problems` checks a whole tool list against
those rules and against the per-request limits, so the snapshot test (and the agent runtime) can
refuse a schema the API would reject. Limits we drop from the schema are stated in the field
descriptions instead.
"""

from collections.abc import Iterable, Mapping
from typing import Any

# Keywords the API rejects in strict schemas. The server still enforces each one.
DROPPED_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "pattern",  # simple patterns are allowed, but we validate formats on the server instead
    }
)
FORBIDDEN_KEYWORDS = DROPPED_KEYWORDS | {"oneOf", "not", "if", "then", "else", "patternProperties"}
STRING_FORMATS = frozenset(
    {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
)
MAX_STRICT_TOOLS = 20
MAX_OPTIONAL_PARAMETERS = 24
MAX_UNION_PARAMETERS = 16

type Schema = dict[str, Any]


def strict_input_schema(schema: Mapping[str, Any]) -> Schema:
    """A strict-compatible copy of a Pydantic model's JSON schema.

    Inlines `$ref`s, drops titles and the keywords above, turns optional `X | None` fields into
    plain optional `X` fields (strict mode lets the model leave them out), and closes every object.
    """
    definitions: Mapping[str, Any] = schema.get("$defs", {})

    def inline(node: Mapping[str, Any]) -> Mapping[str, Any]:
        ref = node.get("$ref")
        if ref is None:
            return node
        target = definitions[ref.removeprefix("#/$defs/")]
        siblings = {key: value for key, value in node.items() if key != "$ref"}
        return inline({**target, **siblings})

    def without_null(node: Mapping[str, Any]) -> Mapping[str, Any]:
        branches = node.get("anyOf")
        if not branches:
            return node
        kept = [branch for branch in branches if branch != {"type": "null"}]
        if len(kept) != 1 or len(kept) == len(branches):
            return node
        siblings = {key: value for key, value in node.items() if key != "anyOf"}
        if siblings.get("default", ...) is None:
            del siblings["default"]
        return {**inline(kept[0]), **siblings}

    def convert(node: Mapping[str, Any], *, optional: bool = False) -> Schema:
        node = inline(node)
        if optional:
            node = without_null(node)
        required = set(node.get("required", ()))
        out: Schema = {}
        for key, value in node.items():
            if key in ("title", "$defs") or key in DROPPED_KEYWORDS:
                continue
            if key == "minItems" and value > 1:
                continue
            if key == "properties":
                out[key] = {
                    name: convert(sub, optional=name not in required) for name, sub in value.items()
                }
            elif key == "items":
                out[key] = convert(value)
            elif key in ("anyOf", "allOf"):
                out[key] = [convert(branch) for branch in value]
            else:
                out[key] = value
        if out.get("type") == "object":
            out.setdefault("properties", {})
            out["additionalProperties"] = False
        return out

    return convert(schema)


def strict_problems(tools: Iterable[tuple[str, Mapping[str, Any]]]) -> list[str]:
    """Everything in these (name, input schema) pairs that strict tool use would reject."""
    problems: list[str] = []
    optional = unions = count = 0

    def check(node: Mapping[str, Any], where: str) -> None:
        nonlocal optional, unions
        for key in sorted(FORBIDDEN_KEYWORDS & node.keys()):
            problems.append(f"{where}: unsupported keyword {key!r}")
        if "$ref" in node and not str(node["$ref"]).startswith("#/"):
            problems.append(f"{where}: external $ref")
        if node.get("format") is not None and node["format"] not in STRING_FORMATS:
            problems.append(f"{where}: unsupported format {node['format']!r}")
        if node.get("minItems", 0) > 1:
            problems.append(f"{where}: minItems above 1")
        if "anyOf" in node and {"type", "properties"} & node.keys():
            problems.append(f"{where}: anyOf next to type or properties")
        if "anyOf" in node or isinstance(node.get("type"), list):
            unions += 1
        if node.get("type") == "object":
            if node.get("additionalProperties") is not False:
                problems.append(f"{where}: object without additionalProperties: false")
            required = set(node.get("required", ()))
            for name, sub in node.get("properties", {}).items():
                optional += name not in required
                check(sub, f"{where}.{name}")
        if isinstance(node.get("items"), Mapping):
            check(node["items"], f"{where}[]")
        for branch_key in ("anyOf", "allOf"):
            for index, branch in enumerate(node.get(branch_key, ())):
                check(branch, f"{where}.{branch_key}[{index}]")
        for name, sub in node.get("$defs", {}).items():
            check(sub, f"{where}.$defs.{name}")

    for name, schema in tools:
        count += 1
        if schema.get("type") != "object":
            problems.append(f"{name}: the root must be an object")
        check(schema, name)
    if count > MAX_STRICT_TOOLS:
        problems.append(f"{count} tools; strict mode allows {MAX_STRICT_TOOLS} per request")
    if optional > MAX_OPTIONAL_PARAMETERS:
        problems.append(f"{optional} optional parameters; the limit is {MAX_OPTIONAL_PARAMETERS}")
    if unions > MAX_UNION_PARAMETERS:
        problems.append(f"{unions} union-typed parameters; the limit is {MAX_UNION_PARAMETERS}")
    return problems
