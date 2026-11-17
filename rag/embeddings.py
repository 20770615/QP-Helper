"""Embedding function: the single swap point for the whole RAG pipeline.

Uses sentence-transformers directly (no chromadb required) so the default
LanceDB backend can start on macOS with a smaller dependency set.
"""

from __future__ import annotations

from rag import config

_MODEL = None


class _SentenceTransformerEF:
    """Minimal callable matching what LanceDB / Chroma paths expect."""

    def __init__(self, model_name: str):
        self.model_name = model_name

    def _model(self):
        global _MODEL
        if _MODEL is None:
            from sentence_transformers import SentenceTransformer

            _MODEL = SentenceTransformer(self.model_name)
        return _MODEL

    def __call__(self, input: list[str]):
        # Chroma embedding functions use the keyword ``input``.
        vectors = self._model().encode(list(input), show_progress_bar=False)
        return [v.tolist() for v in vectors]

    def embed_documents(self, documents: list[str]):
        return self(documents)

    def embed_query(self, query: str):
        return self([query])[0]


def get_embedding_function():
    """Return the embedding function used for both ingestion and retrieval."""
    model_name = getattr(
        config, "EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2"
    )
    return _SentenceTransformerEF(model_name)
