"""Checks on each completed assistant reply before it counts as delivered (§5.12).

A reply is retracted (the customer gets a safe replacement, the stored transcript keeps the
original for review) when it:

- contains the system prompt's canary string;
- repeats a long stretch of the system prompt word for word (`LEAK_WORDS` consecutive words; a
  policy explanation reuses much shorter phrases);
- mentions an order number that is neither the customer's nor one the customer wrote in this
  conversation. That should be impossible, since the tools only return the customer's orders;
  this is the backstop. Numbers the customer typed are allowed so the agent can say "I couldn't
  find PV-800201 on your account".
"""

import re

from relay.agent.prompts import SystemPrompt

LEAK_WORDS = 25
ORDER_NUMBER = re.compile(r"\bPV[\s-]?(\d{6})\b", re.IGNORECASE)
WORD = re.compile(r"[a-z0-9']+")

RETRACTION_TEXT = (
    "Sorry, I can't share that. Is there anything else I can help you with on your orders?"
)


def order_numbers(text: str) -> set[str]:
    return {f"PV-{digits}" for digits in ORDER_NUMBER.findall(text)}


def output_problems(text: str, prompt: SystemPrompt, allowed_orders: set[str]) -> list[str]:
    problems = []
    if prompt.canary in text:
        problems.append("canary")
    if _repeats_prompt(text, prompt.text):
        problems.append("prompt_verbatim")
    if order_numbers(text) - allowed_orders:
        problems.append("foreign_order_number")
    return problems


def _repeats_prompt(text: str, prompt: str) -> bool:
    reply = WORD.findall(text.lower())
    if len(reply) < LEAK_WORDS:
        return False
    source = WORD.findall(prompt.lower())
    windows = {tuple(source[i : i + LEAK_WORDS]) for i in range(len(source) - LEAK_WORDS + 1)}
    return any(
        tuple(reply[i : i + LEAK_WORDS]) in windows for i in range(len(reply) - LEAK_WORDS + 1)
    )
