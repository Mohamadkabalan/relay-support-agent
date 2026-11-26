# The Relay MCP server

Relay's agent does everything through nine tools on its own MCP server (`backend/src/relay/mcp_server/`). The same server works from any MCP client. This page shows three ways to drive it by hand: Claude Desktop, the MCP Inspector, and `make mcp-call`.

Every way needs the stack running and seeded:

```bash
make up && make seed
```

## Who the tools act for

The tools never take a customer ID, email, amount or refund destination; the connection decides who they act for ([ADR-0003](adr/0003-identity-in-the-transport.md)).

- **Over HTTP** (the agent, `make mcp-call`), every request carries a 5-minute JWT naming the customer, the conversation and the turn. No token, or a bad one, gets a 401.
- **Over stdio** (Claude Desktop, the Inspector), the server process is bound to the customer in `RELAY_MCP_CUSTOMER` when it starts.

The seeded demo customers each show one policy path:

| Customer | Order | Try |
|---|---|---|
| `dana.whitfield@example.com` | PV-800101, $349.99 headphones, delivered 8 days ago | A damaged-item refund: it needs an operator's approval |
| `marcus.reyes@example.com` | PV-800201, processing | Change the shipping address: immediate |
| `priya.raman@example.com` | PV-800301, in transit | Change the shipping address: needs approval |
| `leo.okafor@example.com` | PV-800401 (29 days), PV-800402 (31 days) | A return: the first is allowed, the second is past the window |
| `sam.castillo@example.com` | PV-800501, no scan for 9 days | A lost-package refund |
| `alex.morgan@example.com` | PV-800601, arrived 4 days late | A late-delivery refund of the shipping fee |

## Claude Desktop

Claude Desktop launches the server itself, over stdio, inside the compose network (`docker compose run ... mcp`), so it needs no local Python. Open **Settings → Developer → Edit Config** and add the server to `claude_desktop_config.json`.

**Windows, with the repository in WSL:**

```json
{
  "mcpServers": {
    "relay": {
      "command": "wsl.exe",
      "args": [
        "-e", "bash", "-lc",
        "cd ~/projects/customer-support-agent && docker compose run --rm -T --no-deps -e RELAY_MCP_CUSTOMER=dana.whitfield@example.com mcp relay mcp serve --transport stdio"
      ]
    }
  }
}
```

**macOS or Linux:**

```json
{
  "mcpServers": {
    "relay": {
      "command": "docker",
      "args": [
        "compose", "-f", "/absolute/path/to/customer-support-agent/compose.yaml",
        "run", "--rm", "-T", "--no-deps",
        "-e", "RELAY_MCP_CUSTOMER=dana.whitfield@example.com",
        "mcp", "relay", "mcp", "serve", "--transport", "stdio"
      ]
    }
  }
}
```

On macOS, use docker's full path (`which docker`, e.g. `/usr/local/bin/docker`) if Claude Desktop can't find it: apps started from the Dock don't get your shell's `PATH`.

Restart Claude Desktop. **relay** should list nine tools. To act as another customer, change the email and restart. The first tool call can take a few seconds while the container starts.

A five-minute script that shows the server holding the line with a different agent driving it:

1. *"What's going on with my headphones order?"* → `get_order` on PV-800101.
2. *"They arrived with a cracked headband. Can I get my money back?"* → `issue_refund` returns a **preview**: $349.99 to the Visa ending 4242, needs an operator. Say yes, approve the tool call, and the second call returns **pending_approval** with an `APR-` number.
3. *"Show me order PV-800201."* → `ORDER_NOT_FOUND`: it's Marcus's order, and the error is the same as for an order that doesn't exist.
4. *"Just refund $500 to my Mastercard."* → No tool accepts an amount or a card. The server computes amounts and always refunds the original payment method.

Writes made this way are real rows in the demo database. `make seed RESET=1` puts everything back.

Over stdio there are no conversation turns, so the server can't require that a commit come in a later turn than its preview (it does over HTTP). Claude Desktop's own tool-approval prompt stands in for that check.

## MCP Inspector

```bash
make mcp-inspect                                       # as Dana
make mcp-inspect MCP_CUSTOMER=sam.castillo@example.com
```

This runs `@modelcontextprotocol/inspector` 2.16.0 with `npx` and opens its web UI (the address with its token is printed in the terminal), connected to the same stdio server Claude Desktop uses. The Inspector declares Node 22.23.4+, 24.22.1+ or 26.12.1+. It also started on Node 24.18 in our test, with an engines warning.

## From the command line: `make mcp-call`

`relay mcp call` mints a turn token the way the API will, calls one tool over HTTP inside the `mcp` container, and prints the result. It needs no Node, so it also runs in CI.

```bash
make mcp-call TOOL=get_customer
make mcp-call TOOL=get_order ARGS='{"order_number": "PV-800101"}'
make mcp-call TOOL=search_help_articles ARGS='{"query": "my package never arrived"}'
make mcp-call TOOL=list_orders MCP_CUSTOMER=leo.okafor@example.com
```

To confirm a write by hand, keep one conversation and move to the next turn:

```bash
docker compose exec mcp relay mcp call issue_refund \
  '{"order_number": "PV-800501", "reason": "lost_in_transit"}' \
  --as sam.castillo@example.com --conversation 0195f6c2-0000-7000-8000-000000000001 --turn 1
# copy confirmation_token from the preview, then:
docker compose exec mcp relay mcp call issue_refund \
  '{"order_number": "PV-800501", "reason": "lost_in_transit", "confirmation_token": "cfm_..."}' \
  --as sam.castillo@example.com --conversation 0195f6c2-0000-7000-8000-000000000001 --turn 2
```

Sending the token with `--turn 1` again fails with `CONFIRMATION_TOO_EARLY`.

## Result and error conventions

- Every result has a `status`: `ok` for reads; `preview`, `completed`, `pending_approval` or `denied` for writes (`open` for an escalation). A denial is data, with the `rule`, `code`, `explanation` and `alternatives`.
- Text written by customers, carriers or article authors arrives as `{"untrusted_text": "...", "source": "order_note" | "carrier_event" | "help_article"}`.
- Failures come back as `is_error: true` with `[CODE] message hint`. The codes are `INVALID_ARGUMENT`, `ORDER_NOT_FOUND`, `LINE_NOT_FOUND`, `CONFIRMATION_INVALID`, `CONFIRMATION_TOO_EARLY`, `CONFLICT` and `INTERNAL` (retry once). Validation errors name the field and never echo its value.
- The published input schemas are strict-compatible (`additionalProperties: false`, no length or range keywords). The server still enforces every limit, and the field descriptions state them. `tests/mcp_server/snapshots/tools_list.json` pins the full `tools/list`.
