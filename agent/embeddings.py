"""Semantic embeddings for code retrieval — ONNX via fastembed, no GPU needed."""

import hashlib
import logging
import os

import numpy as np

logger = logging.getLogger("sre-agent-webhook")

_MODEL_NAME = "BAAI/bge-small-en-v1.5"
_model = None
_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".sre-agent", "embeddings")


def _get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        logger.info("Loading embedding model %s (first run downloads ~60MB)...", _MODEL_NAME)
        _model = TextEmbedding(_MODEL_NAME)
        logger.info("Embedding model ready.")
    return _model


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:20]


def embed(text: str) -> np.ndarray:
    """Return embedding vector for text, caching to disk by content hash."""
    h = _hash(text)
    os.makedirs(_CACHE_DIR, exist_ok=True)
    path = os.path.join(_CACHE_DIR, f"{h}.npy")
    if os.path.exists(path):
        return np.load(path)
    vec = next(_get_model().embed([text]))
    np.save(path, vec)
    return vec


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / denom) if denom > 0 else 0.0


def semantic_scores(query: str, corpus: dict) -> dict:
    """Return {path: cosine_similarity} between query and each file's content."""
    query_vec = embed(query)
    return {path: cosine(query_vec, embed(text)) for path, text in corpus.items()}


def chunk_scores(query: str, file_chunks: dict) -> dict:
    """Return {path: best_chunk_cosine_similarity} using function-level chunks.

    file_chunks — {path: [chunk_dict, ...]} from chunker.chunks_for_repo()

    For each file we embed every function separately and take the MAX similarity
    across all its chunks. A file scores high if even one of its functions is
    semantically close to the error — much more precise than averaging the whole
    file together.
    """
    query_vec = embed(query)
    scores = {}
    for path, chunks in file_chunks.items():
        if not chunks:
            scores[path] = 0.0
            continue
        scores[path] = max(cosine(query_vec, embed(c["text"])) for c in chunks)
    return scores
