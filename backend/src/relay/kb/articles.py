"""Help-center articles: Markdown files with policy placeholders, rendered from `policy.yaml`.

An article says "within {{returns.window_days}} days" rather than "within 30 days", so the help
center, the policy engine and the system prompt can't drift apart. Rendering fails on an unknown
placeholder, which a test runs over every article.

Placeholders take an optional format: `{{refunds.auto_approve_limit_cents | money}}` -> "$100.00".
"""

import re
from dataclasses import dataclass
from importlib import resources

import yaml

from relay.domain.money import format_cents
from relay.domain.policy_config import PolicyConfig

PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+(?:\.[a-z_]+)+)\s*(?:\|\s*([a-z]+)\s*)?\}\}")
FORMATS = {
    "money": lambda value: format_cents(int(value)),
    "list": lambda value: ", ".join(str(item) for item in value),
}


@dataclass(frozen=True, slots=True)
class Article:
    slug: str
    title: str
    category: str
    body: str  # rendered Markdown, without the front matter


def render(template: str, policy: PolicyConfig) -> str:
    def substitute(match: re.Match[str]) -> str:
        path, fmt = match.group(1), match.group(2)
        try:
            value = policy.lookup(path)
        except KeyError:
            raise ValueError(f"unknown policy placeholder {{{{{path}}}}}") from None
        if fmt is None:
            return str(value)
        if fmt not in FORMATS:
            raise ValueError(f"unknown placeholder format {fmt!r} in {{{{{path} | {fmt}}}}}")
        return FORMATS[fmt](value)

    rendered = PLACEHOLDER.sub(substitute, template)
    if "{{" in rendered:
        raise ValueError(
            "a placeholder could not be parsed: " + rendered[rendered.index("{{") :][:60]
        )
    return rendered


def parse(slug: str, source: str, policy: PolicyConfig) -> Article:
    if not source.startswith("---\n"):
        raise ValueError(f"{slug}: missing front matter")
    _, front, body = source.split("---\n", 2)
    meta = yaml.safe_load(front)
    if not isinstance(meta, dict) or not {"title", "category"} <= meta.keys():
        raise ValueError(f"{slug}: front matter needs title and category")
    return Article(
        slug=slug,
        title=str(meta["title"]),
        category=str(meta["category"]),
        body=render(body.strip(), policy),
    )


def load_articles(policy: PolicyConfig) -> list[Article]:
    folder = resources.files("relay.kb").joinpath("articles")
    articles = [
        parse(entry.name.removesuffix(".md"), entry.read_text("utf-8"), policy)
        for entry in folder.iterdir()
        if entry.name.endswith(".md")
    ]
    return sorted(articles, key=lambda article: article.slug)
