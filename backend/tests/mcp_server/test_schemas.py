"""What clients see in tools/list: a fixed order, strict-compatible schemas, no identity inputs.

The snapshot pins the full listing, so any change to a tool's name, description, schema or
annotations shows up in review. Regenerate it on purpose with:

    UPDATE_SNAPSHOTS=1 uv run pytest tests/mcp_server/test_schemas.py
"""

import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp_types import Tool
from sqlalchemy.ext.asyncio import create_async_engine
from tests.conftest import UNREACHABLE_DATABASE_URL

from relay.db.session import make_sessionmaker
from relay.domain.scope import CustomerScope
from relay.kb.embeddings import HashingEmbedder
from relay.mcp_server.identity import BoundIdentity
from relay.mcp_server.runtime import ToolRuntime
from relay.mcp_server.schemas import strict_input_schema, strict_problems
from relay.mcp_server.server import build_server

SNAPSHOT = Path(__file__).parent / "snapshots" / "tools_list.json"
IDENTITY_OR_MONEY = {"customer", "customer_id", "email", "amount", "amount_cents", "destination"}


@pytest.fixture(scope="module")
def tools() -> list[Tool]:
    import asyncio

    async def listing() -> list[Tool]:
        engine = create_async_engine(UNREACHABLE_DATABASE_URL)
        runtime = ToolRuntime(
            sessionmaker=make_sessionmaker(engine),
            identity=BoundIdentity(CustomerScope(customer_id=uuid.uuid7())),
            embedder=HashingEmbedder(),
        )
        async with Client(build_server(runtime)) as client:
            first = (await client.list_tools()).tools
            second = (await client.list_tools(cache_mode="refresh")).tools
        await engine.dispose()
        assert first == second  # the same listing every time
        return first

    return asyncio.run(listing())


def test_tools_list_matches_the_snapshot(tools: list[Tool]) -> None:
    listing = [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]
    rendered = json.dumps(listing, indent=2, ensure_ascii=False) + "\n"
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(rendered, encoding="utf-8")
    assert rendered == SNAPSHOT.read_text(encoding="utf-8")


def test_every_input_schema_is_strict_compatible(tools: list[Tool]) -> None:
    assert strict_problems((tool.name, tool.input_schema) for tool in tools) == []


def test_no_tool_takes_an_identity_or_an_amount(tools: list[Tool]) -> None:
    def names(schema: dict[str, Any]) -> set[str]:
        found = set(schema.get("properties", {}))
        for sub in schema.get("properties", {}).values():
            found |= names(sub) | names(sub.get("items", {}))
        return found

    for tool in tools:
        assert not names(tool.input_schema) & IDENTITY_OR_MONEY, tool.name


def test_annotations_mark_reads_and_idempotent_writes(tools: list[Tool]) -> None:
    for tool in tools:
        hints = tool.annotations
        assert hints is not None
        assert hints.open_world_hint is False
        if hints.read_only_hint:
            assert tool.name in {
                "get_customer",
                "list_orders",
                "get_order",
                "track_shipment",
                "search_help_articles",
            }
        else:
            assert (hints.destructive_hint, hints.idempotent_hint) == (False, True), tool.name
        assert tool.meta is not None
        assert tool.meta["relay/status_label"].endswith("…")


def test_descriptions_quote_the_current_policy(tools: list[Tool]) -> None:
    by_name = {tool.name: tool.description or "" for tool in tools}
    assert "within 30 days of delivery (15 for refurbished items)" in by_name["create_return"]
    assert "no tracking scan for 7+ days" in by_name["issue_refund"]
    assert "Refunds over $100.00 wait for an operator" in by_name["issue_refund"]
    assert "{" not in "".join(by_name.values())  # every placeholder was filled


# ---------------------------------------------------------------- the converter and the checker


def test_strict_input_schema_inlines_refs_and_drops_what_strict_mode_rejects() -> None:
    pydantic_style = {
        "title": "Arguments",
        "type": "object",
        "$defs": {"Color": {"title": "Color", "type": "string", "enum": ["red", "blue"]}},
        "properties": {
            "name": {"title": "Name", "type": "string", "maxLength": 20, "pattern": "^a"},
            "color": {"$ref": "#/$defs/Color", "description": "Pick one."},
            "nickname": {
                "anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}],
                "default": None,
            },
            "tags": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 5},
            "box": {"type": "object", "properties": {"size": {"type": "integer", "maximum": 9}}},
        },
        "required": ["name", "color"],
    }

    assert strict_input_schema(pydantic_style) == {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "color": {"type": "string", "enum": ["red", "blue"], "description": "Pick one."},
            "nickname": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "box": {
                "type": "object",
                "properties": {"size": {"type": "integer"}},
                "additionalProperties": False,
            },
        },
        "required": ["name", "color"],
        "additionalProperties": False,
    }


def test_strict_problems_names_each_violation() -> None:
    bad = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "maxLength": 3},
            "b": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            "c": {"type": "string", "format": "regex"},
            "d": {"type": "object", "properties": {}},
            "e": {"type": "string", "anyOf": [{"const": "x"}, {"const": "y"}]},
        },
    }

    assert strict_problems([("bad", bad)]) == [
        "bad: object without additionalProperties: false",
        "bad.a: unsupported keyword 'maxLength'",
        "bad.b: unsupported keyword 'oneOf'",
        "bad.c: unsupported format 'regex'",
        "bad.d: object without additionalProperties: false",
        "bad.e: anyOf next to type or properties",
    ]


def test_strict_problems_counts_optional_parameters_across_tools() -> None:
    many = {
        "type": "object",
        "properties": {f"p{i}": {"type": "string"} for i in range(13)},
        "additionalProperties": False,
    }

    assert strict_problems([("one", many), ("two", many)]) == [
        "26 optional parameters; the limit is 24"
    ]
