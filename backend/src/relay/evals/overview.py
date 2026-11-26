"""The Markdown overview of the scenarios (`evals/SCENARIOS.md`), generated from the YAML.

It is what a reviewer reads to judge coverage: who the customer is and what they want, what
should happen, and what the operator does. `make eval-scenarios` rewrites the file; a test checks
it is up to date.
"""

from relay.domain.money import format_cents
from relay.evals.scenario import Category, Scenario


def overview(scenarios: list[Scenario]) -> str:
    regular = [s for s in scenarios if not s.red_team]
    red = [s for s in scenarios if s.red_team]
    lines = [
        "# Eval scenarios",
        "",
        "Generated from `evals/scenarios/` and `evals/redteam/` by `make eval-scenarios`; don't "
        "edit by hand. PLAN.md §9.2 describes the format, §9.3 the grading.",
        "",
        f"{len(scenarios)} scenarios: {len(regular)} regular and {len(red)} red team. "
        f"{sum(1 for s in scenarios if s.ci)} are in the CI subset.",
        "",
    ]
    for category in Category:
        chosen = [s for s in scenarios if s.category is category]
        if not chosen:
            continue
        lines += [f"## {category.value.replace('_', ' ').capitalize()} ({len(chosen)})", ""]
        for scenario in chosen:
            lines += _scenario(scenario)
    return "\n".join(lines).rstrip() + "\n"


def _scenario(scenario: Scenario) -> list[str]:
    customer = scenario.customer
    flags = []
    if scenario.ci:
        flags.append("CI")
    if scenario.severity:
        flags.append(f"{scenario.severity.value}, attack: {scenario.attack}")
    title = f"### `{scenario.id}`: {scenario.title}"
    lines = [title + (f" ({'; '.join(flags)})" if flags else ""), ""]
    lines.append(f'- **Opens with:** "{customer.opening}"')
    if customer.script[1:]:
        lines.append(f"- **Then scripted:** {' / '.join(customer.script[1:])}")
    if customer.adaptive_followups:
        lines.append(f"- **Then:** up to {customer.adaptive_followups} adaptive attacker messages")
    lines.append(f"- **Wants:** {customer.goal}")
    for constraint in customer.constraints:
        lines.append(f"  - {constraint}")
    if scenario.operator.decision != "leave_pending" or "approval" in scenario.tags:
        note = f' ("{scenario.operator.note}")' if scenario.operator.note else ""
        lines.append(f"- **Operator:** {scenario.operator.decision.replace('_', ' ')}{note}")
    lines.append(f"- **Should happen:** {_expected(scenario)}")
    checks = [_check(c.model_dump(exclude_none=True)) for c in scenario.expect.conversation]
    if checks:
        lines.append(f"- **The agent must say:** {'; '.join(checks)}")
    lines.append("")
    return lines


def _expected(scenario: Scenario) -> str:
    parts = []
    for table, rows in scenario.expect.db.items():
        for row in rows:
            fields = ", ".join(
                f"{key}={format_cents(value) if key == 'amount_cents' else value}"
                for key, value in row.items()
            )
            parts.append(f"{table}: {fields}")
    if scenario.expect.escalated:
        parts.append("the conversation is escalated")
    if not parts:
        parts.append("nothing changes in the database")
    if scenario.red_team:
        parts.append("no violation")
    return "; ".join(parts)


def _check(check: dict[str, object]) -> str:
    ((key, value),) = check.items()
    match key:
        case "assistant_mentions":
            return f'mentions "{value}"'
        case "assistant_never_mentions":
            return f'never mentions "{value}"'
        case "assistant_mentions_amount_cents":
            return f"mentions {format_cents(int(str(value)))}"
        case _:
            return f"mentions the new {value}"
