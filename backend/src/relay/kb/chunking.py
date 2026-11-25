"""Splitting articles into retrievable chunks: one per `##` section, long sections by paragraph."""

from dataclasses import dataclass

from relay.kb.articles import Article

MAX_CHARS = 900


@dataclass(frozen=True, slots=True)
class Chunk:
    ordinal: int
    heading: str  # "Article title > Section"
    text: str


def chunk_article(article: Article, max_chars: int = MAX_CHARS) -> list[Chunk]:
    sections: list[tuple[str, list[str]]] = [(article.title, [])]
    for line in article.body.splitlines():
        if line.startswith("## "):
            sections.append((f"{article.title} > {line[3:].strip()}", []))
        else:
            sections[-1][1].append(line)

    chunks: list[Chunk] = []
    for heading, lines in sections:
        paragraphs = [p.strip() for p in "\n".join(lines).split("\n\n") if p.strip()]
        current: list[str] = []
        for paragraph in paragraphs:
            if current and len("\n\n".join([*current, paragraph])) > max_chars:
                chunks.append(Chunk(len(chunks), heading, "\n\n".join(current)))
                current = []
            current.append(paragraph)
        if current:
            chunks.append(Chunk(len(chunks), heading, "\n\n".join(current)))
    return chunks
