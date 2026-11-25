"""Text embeddings for the knowledge base.

`FastEmbedder` runs BAAI/bge-small-en-v1.5 locally through fastembed (ONNX Runtime, 384
dimensions, about 67 MB): no API key and no network once the model is cached. Tests use
`HashingEmbedder`, which needs no model at all.

ONNX Runtime 1.32 sends usage telemetry by default. We switch it off before it loads (and the
Docker image sets the same variable), so running Relay never reports anything to anyone.
"""

import hashlib
import math
import os
import re
from collections.abc import Sequence
from typing import Any, Protocol

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384


class Embedder(Protocol):
    name: str

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedder:
    name = MODEL_NAME

    def __init__(self, cache_dir: str | None = None) -> None:
        self._cache_dir = cache_dir or os.environ.get("FASTEMBED_CACHE_PATH")
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(MODEL_NAME, cache_dir=self._cache_dir)
        return self._model

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._load().passage_embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        return [float(x) for x in next(iter(self._load().query_embed(text))).tolist()]


def disable_onnxruntime_telemetry() -> None:
    """Opt out unless the operator explicitly opted in. Must run before onnxruntime loads."""
    os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")


class HashingEmbedder:
    """A deterministic bag-of-words embedder for tests: similar words, similar vectors."""

    name = "hashing-test-embedder"

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * DIMENSIONS
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            bucket = int(hashlib.md5(word.encode(), usedforsecurity=False).hexdigest(), 16)
            vector[bucket % DIMENSIONS] += 1.0
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        return [x / norm for x in vector]
