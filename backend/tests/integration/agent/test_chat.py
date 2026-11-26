"""`relay chat` drives the same loop and prints what the customer sees."""

from collections.abc import Iterator

from tests.integration.agent.conftest import MakeHarness
from tests.support.factories import Factory

from relay.agent.chat import TerminalView, run_chat
from relay.agent.providers.scripted import Call, Reply


async def test_a_chat_session_prints_replies_status_and_turn_summaries(
    make_harness: MakeHarness, factory: Factory
) -> None:
    customer = await factory.customer("Dana Whitfield")
    order = await factory.order(customer)
    harness = make_harness(
        [
            Reply(calls=(Call("get_order", {"order_number": order.number}),)),
            Reply(text=f"Your order {order.number} was delivered."),
        ]
    )
    out: list[str] = []
    view = TerminalView(out.append)
    harness.agent.listener = view.on_event
    lines: Iterator[str] = iter([f"Where is {order.number}?", "", "x" * 2001, "/quit"])

    async def read_line(prompt: str) -> str | None:
        return next(lines)

    await run_chat(harness.agent, customer.id, view, read_line=read_line)

    printed = "".join(out)
    assert "Relay chat as Dana Whitfield, scripted scripted" in printed
    assert "  · Checking your order…\n" in printed
    assert f"agent> Your order {order.number} was delivered.\n" in printed
    assert "[turn 1 completed · 2 model calls · 1 tool calls · 2,200 tokens" in printed
    assert "[messages are limited to 2000 characters]" in printed
