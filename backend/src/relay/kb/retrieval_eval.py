"""The retrieval eval (PLAN.md §9.10): does the right help article come back?

Each labeled query is a customer-style question, paraphrased rather than copied from the
articles, with the slug of the article that answers it. For each search mode we report recall@1,
recall@3 and MRR over the top 5 articles. The numbers decide whether embeddings stay (ADR-0009).
"""

import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from relay.kb.embeddings import Embedder
from relay.kb.search import MAX_LIMIT, Mode, search


class LabeledQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str
    article: str


def load_queries(path: Path) -> list[LabeledQuery]:
    raw = yaml.safe_load(path.read_text("utf-8"))
    return [LabeledQuery.model_validate(item) for item in raw["queries"]]


@dataclass(slots=True)
class ModeScore:
    mode: Mode
    ranks: list[int | None] = field(default_factory=list)  # 1-based rank of the answer, or None

    @property
    def recall_at_1(self) -> float:
        return self._recall(1)

    @property
    def recall_at_3(self) -> float:
        return self._recall(3)

    @property
    def mrr(self) -> float:
        return sum(1 / rank for rank in self.ranks if rank) / len(self.ranks)

    def hits_at(self, k: int) -> int:
        return sum(1 for rank in self.ranks if rank and rank <= k)

    def _recall(self, k: int) -> float:
        return self.hits_at(k) / len(self.ranks)


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion."""
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass(slots=True)
class RetrievalReport:
    queries: list[LabeledQuery]
    scores: dict[Mode, ModeScore]
    embedder: str

    def misses(self, mode: Mode, k: int = 3) -> list[tuple[str, str, int | None]]:
        ranks = self.scores[mode].ranks
        return [
            (q.query, q.article, rank)
            for q, rank in zip(self.queries, ranks, strict=True)
            if not rank or rank > k
        ]

    def markdown(self, command: str) -> str:
        lines = [
            f"Retrieval eval: {len(self.queries)} queries, embedder `{self.embedder}`.",
            f"Command: `{command}`",
            "",
            "| Mode | Recall@1 | Recall@3 (95% CI) | MRR@5 |",
            "|---|---|---|---|",
        ]
        total = len(self.queries)
        for mode, score in self.scores.items():
            low, high = wilson_interval(score.hits_at(3), total)
            lines.append(
                f"| {mode.value} | {score.recall_at_1:.3f} | {score.recall_at_3:.3f} "
                f"({low:.2f}-{high:.2f}) | {score.mrr:.3f} |"
            )
        for mode in self.scores:
            misses = self.misses(mode)
            lines += ["", f"Misses at 3 for {mode.value} ({len(misses)}):"]
            lines += [
                f"- {query!r} -> {article} (rank {rank or '>5'})" for query, article, rank in misses
            ]
        return "\n".join(lines)


async def run_eval(
    session: AsyncSession, queries: list[LabeledQuery], embedder: Embedder
) -> RetrievalReport:
    scores = {mode: ModeScore(mode) for mode in Mode}
    for labeled in queries:
        for mode, score in scores.items():
            hits = await search(
                session, labeled.query, mode=mode, limit=MAX_LIMIT, embedder=embedder
            )
            slugs = [hit.slug for hit in hits]
            score.ranks.append(
                slugs.index(labeled.article) + 1 if labeled.article in slugs else None
            )
    return RetrievalReport(queries=queries, scores=scores, embedder=embedder.name)
