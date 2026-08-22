"""Build the vector index.

**This is the critical fix (review finding #1).**

The old ``cg/index_docs.py:63`` did::

    primary_text = text_en if text_en.strip() else text_hi

so every one of the 526 indexed chunks was embedded from machine-translated
English that was, on inspection, fabricated -- three of four documents had
unusable titles and all four carried Watchtower/Jehovah's-Witnesses
contamination from the OPUS training corpus.  A staff member searching the
database was being shown invented government policy.

The embedding model already in use --
``paraphrase-multilingual-MiniLM-L12-v2`` -- handles Hindi natively, so the
translation was never needed for search in the first place.  This module
embeds the **Hindi source text** and nothing else.  Translation cannot corrupt
the index because translation is no longer anywhere near it: English is
generated on demand at display time (``pipeline/translate.py``).

Also fixed here:

* **Finding #11** -- chunk offsets.  The old code re-derived a Hindi excerpt as
  ``text_hi[i * 700 : (i + 1) * 700]``, guessing offsets from the *English*
  chunk index, so the two drifted apart chunk over chunk.  Exact source offsets
  are now recorded at chunk time.
* **Finding #5** -- ``delete_collection`` + ``create_collection`` on every run
  left orphaned on-disk segment directories behind (four of them were
  committed).  Indexing is now incremental per state, and ``--prune`` removes
  unreferenced segment directories.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer

from . import jsonio, paths
from .chunking import CHUNK_OVERLAP, CHUNK_SIZE, chunk_with_offsets
from .states import StateConfig

EMBED_BATCH = 32

__all__ = ["chunk_with_offsets", "index_states", "prune_orphan_segments",
           "CHUNK_SIZE", "CHUNK_OVERLAP"]


def _open_collection(client: chromadb.ClientAPI):
    try:
        return client.get_or_create_collection(
            name=paths.COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
    except Exception:
        # Older/newer Chroma may reject the metadata hint on an existing
        # collection; fall back to plain get_or_create.
        return client.get_or_create_collection(name=paths.COLLECTION_NAME)


def prune_orphan_segments(db_path: Path) -> int:
    """Delete on-disk segment directories no longer referenced by the DB."""
    sqlite_path = db_path / "chroma.sqlite3"
    if not sqlite_path.exists():
        return 0
    try:
        conn = sqlite3.connect(str(sqlite_path))
        referenced = {row[0] for row in conn.execute("select id from segments")}
        referenced |= {row[0] for row in conn.execute("select id from collections")}
        conn.close()
    except sqlite3.Error:
        return 0

    removed = 0
    for child in db_path.iterdir():
        if child.is_dir() and child.name not in referenced and len(child.name) == 36:
            shutil.rmtree(child, ignore_errors=True)
            print(f"  pruned orphaned segment dir: {child.name}")
            removed += 1
    return removed


def index_states(states: list[StateConfig], db_path: Path | None = None,
                 prune: bool = True) -> int:
    """Index the Hindi text of every processed document for the given states."""
    paths.configure_stdout()
    db_path = Path(db_path) if db_path else paths.CHROMA_DIR
    db_path.mkdir(parents=True, exist_ok=True)

    print("Loading multilingual embedding model (handles Hindi natively)...")
    model = SentenceTransformer(paths.EMBEDDING_MODEL)

    client = chromadb.PersistentClient(path=str(db_path))
    collection = _open_collection(client)

    total_chunks = 0
    for state in states:
        documents = jsonio.read_json(state.processed_docs, default=[]) or []
        if not documents:
            print(f"\n[{state.name}] no processed_docs.json -- run `ocr` first. Skipping.")
            continue

        print(f"\n=== Indexing {state.name}: {len(documents)} documents ===")

        # Replace this state's chunks only; other states stay put.  This is what
        # makes a shared collection safe, and it stops the old
        # delete-the-whole-collection dance that orphaned segment dirs.
        try:
            collection.delete(where={"state_key": state.key})
        except Exception as exc:
            print(f"  (no prior chunks removed: {exc})")

        texts: list[str] = []
        metadatas: list[dict] = []
        ids: list[str] = []
        skipped = 0

        for doc in documents:
            # Hindi source text -- the ground truth, and the only thing embedded.
            text_hi = doc.get("text", "") or ""
            if not text_hi.strip():
                skipped += 1
                continue

            doc_id = doc.get("id", "")
            chunks = chunk_with_offsets(text_hi)

            for i, (chunk, start, end) in enumerate(chunks):
                texts.append(chunk)
                metadatas.append({
                    "doc_id": doc_id,
                    "filename": doc.get("filename", ""),
                    "title_hi": doc.get("inferred_title", ""),
                    "link_text": doc.get("link_text", ""),
                    "state": doc.get("state", state.name),
                    "state_key": state.key,
                    "category": doc.get("category", "General / Uncategorized"),
                    "file_path": doc.get("file_path", ""),
                    "pdf_url": doc.get("pdf_url", ""),
                    "chunk_index": i,
                    # Exact source span (finding #11): text[hi_start:hi_end] IS
                    # this chunk, so the app can widen context without guessing.
                    "hi_start": start,
                    "hi_end": end,
                    "was_ocr_used": bool(doc.get("was_ocr_used", False)),
                    "language": "hi",
                })
                # Namespaced so doc_1 in Bihar cannot collide with doc_1 in
                # Chhattisgarh inside the shared collection.
                ids.append(f"{state.key}:{doc_id}:{i}")

        if skipped:
            print(f"  {skipped} document(s) had no extracted text and were skipped.")
        if not texts:
            print("  nothing to index.")
            continue

        print(f"  embedding {len(texts)} Hindi chunks...")
        embeddings = model.encode(texts, batch_size=EMBED_BATCH, show_progress_bar=True).tolist()

        # Chroma caps a single add(); stay well under it.
        add_batch = 2000
        for i in range(0, len(texts), add_batch):
            collection.add(
                documents=texts[i:i + add_batch],
                embeddings=embeddings[i:i + add_batch],
                metadatas=metadatas[i:i + add_batch],
                ids=ids[i:i + add_batch],
            )
        print(f"  indexed {len(texts)} chunks for {state.name}.")
        total_chunks += len(texts)

    if prune:
        prune_orphan_segments(db_path)

    print(f"\nDone. {total_chunks} Hindi chunks indexed this run; "
          f"collection now holds {collection.count()} chunks.")
    print(f"Vector store: {db_path}")
    return total_chunks
