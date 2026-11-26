"""The tool manifest: the MCP server's tools as the model sees them, pinned per conversation.

A conversation stores its manifest as JSON text when it starts and renders every request's
`tools` from that text, so a deploy that changes the tools only affects new conversations, and a
request's tool definitions stay byte-identical for the conversation's lifetime (§5.4).

Normalization makes any MCP server's tools usable with strict tool use: schemas pass through
`strict_input_schema` (our own server already publishes them that way), the read-only hint
decides scheduling (§5.8), and the `relay/status_label` meta gives the customer-facing label.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from mcp_types import Tool

from relay.agent.transcript import dump
from relay.mcp_server.schemas import strict_input_schema
from relay.mcp_server.tools import STATUS_LABEL_KEY

DEFAULT_STATUS_LABEL = "Working on it…"


@dataclass(frozen=True, slots=True)
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    status_label: str


@dataclass(frozen=True, slots=True)
class Manifest:
    tools: tuple[ToolDef, ...]  # sorted by name

    @classmethod
    def from_mcp(cls, tools: list[Tool]) -> Manifest:
        defs = []
        for tool in tools:
            meta = tool.meta or {}
            label = meta.get(STATUS_LABEL_KEY)
            defs.append(
                ToolDef(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=strict_input_schema(dict(tool.input_schema)),
                    read_only=bool(tool.annotations and tool.annotations.read_only_hint),
                    status_label=label if isinstance(label, str) else DEFAULT_STATUS_LABEL,
                )
            )
        return cls(tuple(sorted(defs, key=lambda tool: tool.name)))

    def to_json(self) -> str:
        return dump(
            [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    "read_only": tool.read_only,
                    "status_label": tool.status_label,
                }
                for tool in self.tools
            ]
        )

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        return cls(tuple(ToolDef(**entry) for entry in json.loads(text)))

    def get(self, name: str) -> ToolDef | None:
        return next((tool for tool in self.tools if tool.name == name), None)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
