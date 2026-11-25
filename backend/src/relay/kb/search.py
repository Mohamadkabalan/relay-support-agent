"""Help-center search: full-text, vector, or a hybrid of the two (reciprocal rank fusion).

Results are per article: each article appears once, represented by its best-matching chunk.
The retrieval eval chose vector search as the default (ADR-0009); the other modes stay for the
eval and for comparison.
"""

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from relay.kb.embeddings import Embedder

CANDIDATES = 20  # chunks taken from each ranker before fusing
RRF_K = 60  # the standard reciprocal-rank-fusion constant
MAX_LIMIT = 5


class Mode(StrEnum):
    FULL_TEXT = "fts"
    VECTOR = "vector"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class Hit:
    slug: str
    title: str
    heading: str
    excerpt: str
    score: float


# Any query word may match (OR), and matches in a section heading weigh more than in its text.
FULL_TEXT_SQL = text("""
    WITH q AS (
        SELECT replace(plainto_tsquery('english', :query)::text, ' & ', ' | ') AS expr
    )
    SELECT c.id, ts_rank_cd(c.tsv, q.expr::tsquery) AS score
    FROM kb_chunks c, q
    WHERE q.expr <> '' AND c.tsv @@ q.expr::tsquery
    ORDER BY score DESC, c.id
    LIMIT :candidates
""")

VECTOR_SQL = text("""
    SELECT c.id, 1 - (c.embedding <=> CAST(:embedding AS vector)) AS score
    FROM kb_chunks c
    WHERE c.embedding IS NOT NULL
    ORDER BY c.embedding <=> CAST(:embedding AS vector), c.id
    LIMIT :candidates
""")

CHUNKS_SQL = text("""
    SELECT c.id, a.slug, a.title, c.heading, c.text
    FROM kb_chunks c JOIN help_articles a ON a.id = c.article_id
    WHERE c.id = ANY(:ids)
""")


async def search(
    session: AsyncSession,
    query: str,
    *,
    mode: Mode = Mode.VECTOR,
    limit: int = 3,
    embedder: Embedder | None = None,
) -> list[Hit]:
    limit = max(1, min(limit, MAX_LIMIT))
    query = query.strip()
    if not query:
        return []
    if mode is not Mode.FULL_TEXT and embedder is None:
        raise ValueError(f"{mode.value} search needs an embedder")

    rankings: list[list[tuple[object, float]]] = []
    if mode in (Mode.FULL_TEXT, Mode.HYBRID):
        rows = await session.execute(FULL_TEXT_SQL, {"query": query, "candidates": CANDIDATES})
        rankings.append([(row.id, float(row.score)) for row in rows])
    if mode in (Mode.VECTOR, Mode.HYBRID) and embedder is not None:
        vector = embedder.embed_query(query)
        rows = await session.execute(
            VECTOR_SQL, {"embedding": _vector_literal(vector), "candidates": CANDIDATES}
        )
        rankings.append([(row.id, float(row.score)) for row in rows])

    scores = rankings[0] if len(rankings) == 1 else _fuse(rankings)
    if not scores:
        return []
    details = {
        row.id: row
        for row in await session.execute(CHUNKS_SQL, {"ids": [chunk_id for chunk_id, _ in scores]})
    }
    hits: list[Hit] = []
    seen: set[str] = set()
    for chunk_id, score in scores:
        row = details[chunk_id]
        if row.slug in seen:
            continue
        seen.add(row.slug)
        hits.append(Hit(row.slug, row.title, row.heading, _excerpt(row.text), round(score, 4)))
        if len(hits) == limit:
            break
    return hits


def _fuse(rankings: list[list[tuple[object, float]]]) -> list[tuple[object, float]]:
    fused: dict[object, float] = {}
    for ranking in rankings:
        for rank, (chunk_id, _) in enumerate(ranking, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
    return sorted(fused.items(), key=lambda item: (-item[1], str(item[0])))


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vector) + "]"


def _excerpt(body: str, max_chars: int = 400) -> str:
    flat = " ".join(body.split())
    return flat if len(flat) <= max_chars else flat[: max_chars - 1].rsplit(" ", 1)[0] + "…"
