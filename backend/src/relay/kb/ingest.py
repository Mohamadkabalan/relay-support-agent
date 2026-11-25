"""Loading the rendered help articles into Postgres: articles, chunks, full-text vectors and
(optionally) embeddings. Re-running it replaces the knowledge base."""

import uuid
from dataclasses import dataclass

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from relay.db.models import HelpArticle, KbChunk
from relay.domain.policy_config import PolicyConfig
from relay.kb.articles import load_articles
from relay.kb.chunking import chunk_article
from relay.kb.embeddings import Embedder

# Article and chunk IDs derive from the slug, so re-ingesting gives the same IDs (and the seed's
# data hash stays stable).
NAMESPACE = uuid.UUID("5f0c1c7e-6a4e-4d0b-9a57-1c2a43a0c0de")


@dataclass(frozen=True, slots=True)
class IngestReport:
    articles: int
    chunks: int
    embedded: bool


async def ingest(
    session: AsyncSession, policy: PolicyConfig, embedder: Embedder | None
) -> IngestReport:
    articles = load_articles(policy)
    await session.execute(delete(KbChunk))
    await session.execute(delete(HelpArticle))
    chunk_count = 0
    for article in articles:
        article_id = uuid.uuid5(NAMESPACE, f"article:{article.slug}")
        chunks = chunk_article(article)
        vectors = (
            embedder.embed_documents([f"{c.heading}\n{c.text}" for c in chunks])
            if embedder is not None
            else [None] * len(chunks)
        )
        session.add(
            HelpArticle(
                id=article_id,
                slug=article.slug,
                title=article.title,
                category=article.category,
                body=article.body,
                policy_version=policy.version,
            )
        )
        session.add_all(
            KbChunk(
                id=uuid.uuid5(NAMESPACE, f"chunk:{article.slug}:{chunk.ordinal}"),
                article_id=article_id,
                ordinal=chunk.ordinal,
                heading=chunk.heading,
                text=chunk.text,
                embedding=vector,
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        )
        chunk_count += len(chunks)
    await session.flush()
    return IngestReport(articles=len(articles), chunks=chunk_count, embedded=embedder is not None)
