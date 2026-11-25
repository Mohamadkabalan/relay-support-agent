# ADR-0009: Help-center retrieval uses local embeddings (vector search)

- **Status:** Accepted
- **Date:** 2026-11-25

## Context

`search_help_articles` answers policy questions from 30 help articles (104 chunks). PLAN.md set the default to a hybrid of Postgres full-text search and local embeddings, with one rule decided in advance: if full-text search alone did as well, we would drop the embedding model, because it adds about 270 MB to the backend image (measured: 207 MB of ONNX Runtime, numpy, tokenizers, Pillow and their dependencies, plus the 65 MB model) and a model to manage.

## Measurement

40 customer-style questions, paraphrased rather than copied from the articles and labeled with the article that answers them (`evals/retrieval/queries.yaml`), written before the first run and not tuned afterwards. Results are per article, top 5. Embedder: `BAAI/bge-small-en-v1.5` through fastembed 0.9.1.

Command: `make up && make seed && make eval-retrieval`, run on 2026-11-25. The full output, including every miss, is in `evals/retrieval/report.md`.

| Mode | Recall@1 | Recall@3 (95% Wilson CI) | MRR@5 |
|---|---|---|---|
| Full-text (`ts_rank_cd`, any query word) | 0.375 | 0.625 (0.47-0.76) | 0.529 |
| Vector (cosine, HNSW) | 0.625 | 0.825 (0.68-0.91) | 0.729 |
| Hybrid (reciprocal rank fusion) | 0.650 | 0.775 (0.62-0.88) | 0.731 |

Paired comparison at recall@3, on the queries where only one method finds the answer:

- **Vector against full-text: 9 against 1** (exact McNemar test, two-sided, p ≈ 0.02). Full-text misses paraphrases with no shared words, such as "swap the black watch for the silver one" (exchanges) and "do you take american express" (payment methods).
- **Vector against hybrid: 3 against 1** (p ≈ 0.63). Fusing in full-text lowered three answers below the top 3 and lifted one.

## Decision

- Keep the embeddings. Full-text search alone is clearly worse, so the rule in PLAN.md keeps fastembed.
- The agent's tool uses **vector search** (`relay/kb/search.py`, `Mode.VECTOR`). The tool returns 3 articles by default, so recall@3 is the number that matters, and vector is the best point estimate there. The gap to hybrid is within noise; between two equivalent options we take the one with a single ranker.
- Keep the full-text index and the hybrid mode. They cost little, the eval reports all three modes on every run, and an article set with product names and codes may favor hybrid later.
- The model is baked into the backend image at build time, and containers run with `HF_HUB_OFFLINE=1` and `ORT_DISABLE_TELEMETRY=1`: nothing is downloaded at runtime, and ONNX Runtime's usage telemetry (on by default in 1.32) stays off.

## Consequences

- The backend image is 555 MB (`docker image ls relay-backend`), about 270 MB of it for embeddings.
- Tests use a deterministic hashing embedder, so CI needs no model download. The real model is exercised by `make seed`, by the CI stack job's seed step, and by `make eval-retrieval`.
- Seven questions still miss at 3 with vector search. Several share a pattern: casual wording ("send something back", "showed up four days after you said") against articles that use our own vocabulary ("return window", "promised date"). We revisit this when the end-to-end evals show whether retrieval misses actually hurt answers (Phase 4 and 7), rather than tuning on these 40 questions.
- Changing an article changes the numbers. This report was produced after the REF-1 change to "Items that can't be returned"; an earlier run on the previous text gave full-text recall@3 of 0.650 and the same vector and hybrid recall@3.
