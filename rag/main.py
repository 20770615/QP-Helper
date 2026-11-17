"""Phase 1 CLI entry point.

Usage:
    python rag/main.py            # run from the project root
    python -m rag.main            # equivalent, run as a module

Behaviour:
    1. Ingest every supported file in the samples/ directory.
       Byte-identical duplicates are skipped so the store is not polluted
       with the same content under two different doc_ids.
    2. Run 2-3 sample queries and print the retrieved chunks + metadata.
    3. No login, no Outlook, no transcription, no external LLM call.

The vector store persists to disk under chroma_store/; re-running upserts with
stable ids, so there is no duplicate spam.
"""

import hashlib
import sys
from pathlib import Path

# Make the project root importable whether run as a script or a module.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from rag import config  # noqa: E402
from rag.ingest import ingest_file  # noqa: E402
from rag.retrieve import retrieve, format_results  # noqa: E402

# Queries are tailored to the real samples: Quam Global Equity Opportunities
# Reports (UCITS) + the Greater China UCITS Fund holdings CSV.
SAMPLE_QUERIES = [
    "What is the recommended allocation weight change for Alibaba (9988 HK) and its target price?",
    "Which sectors carry the heaviest weights in the Greater China UCITS fund holdings?",
    "Summarize the long-only investable ideas and recent rating changes from Daiwa.",
]


def _unique_sample_files():
    """Yield supported sample files, skipping byte-identical duplicates.

    Two files with the same content hash get the same text embedded twice
    under different doc_ids, which would pollute retrieval with duplicate
    hits. We keep the first occurrence and skip later copies.
    """
    seen: dict[str, Path] = {}
    for p in sorted(config.SAMPLES_DIR.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in config.SUPPORTED_SUFFIXES:
            continue
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if digest in seen:
            print(f"  [skip] byte-identical duplicate of '{seen[digest].name}': {p.name}")
            continue
        seen[digest] = p
        yield p


def main() -> None:
    print("=" * 72)
    print("Phase 1 — Standalone RAG retrieval pipeline")
    print(f"Samples dir : {config.SAMPLES_DIR}")
    print(f"Store dir   : {config.PERSIST_DIR}")
    print(f"Collection  : {config.COLLECTION_NAME}")
    print("=" * 72)

    print("\n[1/2] Ingesting samples ...")
    if not config.SAMPLES_DIR.exists():
        print(f"  ERROR: samples directory not found: {config.SAMPLES_DIR}")
        sys.exit(1)
    total = 0
    for p in _unique_sample_files():
        n = ingest_file(p)
        total += n
        print(f"  ingested {p.name}: {n} chunk(s)")
    print(f"  done — {total} chunk(s) upserted.")

    print("\n[2/2] Running sample queries ...\n")
    for q in SAMPLE_QUERIES:
        print("#" * 72)
        print(f"QUERY: {q}")
        print("#" * 72)
        results = retrieve(q, n=5)
        print(format_results(results))

    print("=" * 72)
    print("Phase 1 complete. No LLM call was made; retrieval only.")
    print("=" * 72)


if __name__ == "__main__":
    main()
