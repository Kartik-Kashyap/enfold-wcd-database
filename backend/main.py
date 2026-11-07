"""FastAPI backend for the Enfold Child Rights & Policy Document Portal.

Wraps the existing pipeline modules (pipeline.states, pipeline.filters,
pipeline.translate, pipeline.jsonio, pipeline.paths) and exposes them as a
clean REST API for the React frontend.

Run with:
    uvicorn main:app --reload --port 8000
from inside the backend/ directory, or:
    uvicorn backend.main:app --reload --port 8000
from the repo root.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import date as date_cls
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# Make the repo root importable regardless of where uvicorn is launched from.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from pipeline import filters, jsonio, paths, translate  # noqa: E402
from pipeline.states import all_states  # noqa: E402

app = FastAPI(
    title="Enfold Child Rights & Policy Portal API",
    description="Serves document search, translation, and coverage data for the React frontend.",
    version="1.0.0",
)

# Allow origins: by default allow * (or override with CORS_ORIGIN env var)
_CORS_ORIGINS_ENV = os.environ.get("CORS_ORIGIN", "")
if _CORS_ORIGINS_ENV:
    _CORS_ORIGINS = [o.strip() for o in _CORS_ORIGINS_ENV.split(",") if o.strip()]
    _ALLOW_CREDS = True
else:
    _CORS_ORIGINS = ["*"]
    _ALLOW_CREDS = False

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=_ALLOW_CREDS,
    allow_methods=["*"],
    allow_headers=["*"],
)

PREVIEW_CHARS = 900


# ---------------------------------------------------------------------------
# Startup: load shared resources once
# ---------------------------------------------------------------------------
_collection = None
_embed_model = None
_tcache: translate.TranslationCache | None = None
_raw_docs: list[dict] = []
_coverage: dict[str, dict] = {}


def _load_resources():
    global _collection, _embed_model, _tcache, _raw_docs, _coverage

    # Translation cache
    _tcache = translate.TranslationCache()

    # Documents + coverage
    docs: list[dict] = []
    coverage: dict[str, dict] = {}
    for state in all_states():
        processed = jsonio.read_json(state.processed_docs, default=[]) or []
        crawled = jsonio.read_json(state.crawl_metadata, default=[]) or []
        for doc in processed:
            doc.setdefault("state", state.name)
            doc.setdefault("state_key", state.key)
        docs.extend(processed)
        coverage[state.name] = {
            "key": state.key,
            "crawled": len(crawled),
            "processed": len(processed),
            "pdfs_on_disk": sum(1 for _ in state.pdf_dir.glob("*.pdf")) if state.pdf_dir.exists() else 0,
        }
    _raw_docs = docs
    _coverage = coverage

    # Chroma + embeddings (optional — search still degrades gracefully if absent)
    try:
        import chromadb
        from sentence_transformers import SentenceTransformer

        client = chromadb.PersistentClient(path=str(paths.CHROMA_DIR))
        try:
            _collection = client.get_collection(paths.COLLECTION_NAME)
        except Exception:
            _collection = None
        _embed_model = SentenceTransformer(paths.EMBEDDING_MODEL)
    except Exception as exc:
        print(f"[warn] Search index not loaded: {exc}")
        _collection = None
        _embed_model = None


_load_resources()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _strip_count(label: str) -> str:
    return label.rsplit(" (", 1)[0] if label.endswith(")") and " (" in label else label


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class BasicSearchRequest(BaseModel):
    query: str
    state: Optional[str] = None
    category: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    scan_body: bool = False


class SemanticSearchRequest(BaseModel):
    query: str
    state: Optional[str] = None
    category: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    n_results: int = 20


class TranslateRequest(BaseModel):
    text: str
    model: str = translate.DEFAULT_MODEL


class SummariseRequest(BaseModel):
    text: str
    state: str = ""
    category: str = ""
    model: str = translate.DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/status")
def get_status():
    """Coverage per state: crawled / processed / PDFs on disk."""
    return {"coverage": _coverage, "total_docs": len(_raw_docs)}


@app.get("/api/ollama-status")
def ollama_status():
    ok, msg = translate.is_available()
    cached = len(_tcache) if _tcache else 0
    return {"available": ok, "message": msg, "cached_excerpts": cached}


@app.get("/api/states")
def get_states():
    """States that appear in processed documents, with document counts."""
    counts = Counter(d.get("state", "Unknown") for d in _raw_docs)
    return {
        "states": [{"name": name, "count": n} for name, n in sorted(counts.items())]
    }


@app.get("/api/categories")
def get_categories():
    """Categories found in processed documents, with counts."""
    counts = Counter(d.get("category", "General / Uncategorized") for d in _raw_docs)
    return {
        "categories": [{"name": name, "count": n} for name, n in sorted(counts.items())]
    }


@app.get("/api/date-range")
def get_date_range():
    """Min and max document_date across all docs that have one."""
    dated = [d for d in _raw_docs if d.get("document_date")]
    if not dated:
        return {"min": None, "max": None, "dated_count": 0, "total": len(_raw_docs)}
    return {
        "min": min(d["document_date"] for d in dated),
        "max": max(d["document_date"] for d in dated),
        "dated_count": len(dated),
        "total": len(_raw_docs),
    }


@app.get("/api/index-status")
def get_index_status():
    """Whether the vector index is available and how many chunks it holds."""
    if _collection is None:
        return {"available": False, "chunk_count": 0}
    try:
        return {"available": True, "chunk_count": _collection.count()}
    except Exception:
        return {"available": False, "chunk_count": 0}


@app.get("/api/documents")
def list_documents(
    state: Optional[str] = Query(default=None),
    category: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    """Browse all documents with optional filters and pagination.

    Used by the 'Show all documents' view when no search query is entered.
    Returns a paginated list sorted by document_date descending (undated last).
    """
    matched = []
    for doc in _raw_docs:
        if state and doc.get("state") != state:
            continue
        if category and doc.get("category") != category:
            continue
        if date_from or date_to:
            dd = doc.get("document_date")
            if not dd:
                continue
            if date_from and dd < date_from:
                continue
            if date_to and dd > date_to:
                continue
        matched.append(doc)

    # Sort: dated docs newest-first, undated appended at end
    dated   = sorted([d for d in matched if d.get("document_date")],
                     key=lambda d: d["document_date"], reverse=True)
    undated = [d for d in matched if not d.get("document_date")]
    sorted_docs = dated + undated

    total = len(sorted_docs)
    start = (page - 1) * page_size
    page_docs = sorted_docs[start:start + page_size]

    return {
        "results": [_doc_summary(d) for d in page_docs],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }


@app.post("/api/search/basic")
def basic_search(req: BasicSearchRequest):
    """Keyword search over titles, link-text, filenames (and optionally body)."""
    q = req.query.lower().strip()
    if not q:
        return {"results": [], "total": 0}

    matched = []
    for doc in _raw_docs:
        if req.state and doc.get("state") != req.state:
            continue
        if req.category and doc.get("category") != req.category:
            continue
        if req.date_from or req.date_to:
            dd = doc.get("document_date")
            if not dd:
                continue
            if req.date_from and dd < req.date_from:
                continue
            if req.date_to and dd > req.date_to:
                continue

        haystack = " ".join([
            doc.get("inferred_title", ""),
            doc.get("link_text", ""),
            doc.get("filename", ""),
        ]).lower()
        if q in haystack or (req.scan_body and q in (doc.get("text", "") or "").lower()):
            matched.append(_doc_summary(doc))

    return {"results": matched, "total": len(matched)}


@app.post("/api/search/semantic")
def semantic_search(req: SemanticSearchRequest):
    """Vector search over the Chroma index."""
    if _collection is None or _embed_model is None:
        raise HTTPException(
            status_code=503,
            detail="Semantic search is not available yet. The document index is still being built.",
        )
    try:
        count = _collection.count()
    except Exception:
        count = 0
    if count == 0:
        raise HTTPException(status_code=503, detail="The semantic search index is currently empty.")

    query_vector = _embed_model.encode([req.query]).tolist()[0]
    where_filter = filters.build_where_filter(
        state=req.state or None,
        category=req.category or None,
        date_from=req.date_from,
        date_to=req.date_to,
    )

    results = _collection.query(
        query_embeddings=[query_vector],
        n_results=req.n_results,
        where=where_filter,
    )

    ids = results.get("ids", [[]])[0]
    if not ids:
        return {"results": [], "total": 0, "chunk_count": count}

    items = []
    seen: set[str] = set()
    for i in range(len(ids)):
        meta = results["metadatas"][0][i]
        chunk_hi = results["documents"][0][i]
        filename = meta.get("filename", "")
        if filename in seen:
            continue
        seen.add(filename)

        distance = results.get("distances", [[]])[0][i] if results.get("distances") else None
        items.append({
            "id": ids[i],
            "filename": filename,
            "title": meta.get("title_hi") or meta.get("link_text") or "Untitled",
            "state": meta.get("state", "—"),
            "state_key": meta.get("state_key", ""),
            "category": meta.get("category", "—"),
            "document_date": meta.get("document_date"),
            "was_ocr_used": bool(meta.get("was_ocr_used")),
            "file_path": meta.get("file_path", ""),
            "link_text": meta.get("link_text", ""),
            "hindi_excerpt": chunk_hi,
            "similarity": round(max(0.0, 1 - distance), 3) if distance is not None else None,
            "chunk_index": meta.get("chunk_index", 0),
            "hi_start": meta.get("hi_start", 0),
            "hi_end": meta.get("hi_end", 0),
        })
        if len(items) >= 10:
            break

    return {"results": items, "total": len(items), "chunk_count": count}


@app.post("/api/translate")
def translate_text(req: TranslateRequest):
    """On-demand translation of one excerpt. Cached and quality-guarded."""
    if not req.text.strip():
        return {"ok": False, "reasons": ["empty text"]}

    result = translate.translate_excerpt(req.text, model=req.model, cache=_tcache)
    return {
        "ok": result.ok,
        "english": result.english,
        "status": result.status,
        "reasons": result.reasons,
        "cached": result.cached,
        "model": result.model,
        "metrics": result.metrics,
    }


@app.post("/api/summarise")
def summarise_text(req: SummariseRequest):
    """Generate a short AI summary of a Hindi excerpt via local Ollama."""
    ok, msg = translate.is_available(req.model)
    if not ok:
        raise HTTPException(status_code=503, detail=msg)

    try:
        import ollama

        prompt = (
            f"The following is Hindi text from an Indian government document "
            f"({req.state}, {req.category}). It is OCR output and may be imperfect.\n"
            "In English, give at most 3 bullet points covering only what the text "
            "actually states. If the text is too garbled to summarise, say exactly "
            "that instead of guessing. Do not invent policy details.\n\n"
            f"{req.text[:2500]}\n\nSummary:"
        )
        response = ollama.generate(
            model=req.model,
            prompt=prompt,
            options={"temperature": 0.2},
        )
        summary = (response.get("response") if isinstance(response, dict) else response.response) or ""
        return {"ok": True, "summary": summary.strip(), "model": req.model}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ollama error: {exc}")


@app.get("/api/pdf/{state_key}/{filename}")
def serve_pdf(state_key: str, filename: str):
    """Serve a PDF file for download/viewing."""
    # Validate state key
    from pipeline.states import STATES
    if state_key not in STATES:
        raise HTTPException(status_code=404, detail="Unknown state")

    state = STATES[state_key]
    pdf_path = state.pdf_dir / filename

    # Prevent path traversal
    try:
        pdf_path.resolve().relative_to(state.pdf_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename")

    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail=f"PDF not found: {filename}")

    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=filename,
    )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------
def _doc_summary(doc: dict) -> dict:
    """Strip the full text body before sending to the frontend."""
    text = doc.get("text", "") or ""
    return {
        "filename": doc.get("filename", ""),
        "title": doc.get("inferred_title") or doc.get("link_text") or "Untitled",
        "state": doc.get("state", ""),
        "state_key": doc.get("state_key", ""),
        "category": doc.get("category", ""),
        "document_date": doc.get("document_date"),
        "was_ocr_used": bool(doc.get("was_ocr_used")),
        "file_path": doc.get("file_path", ""),
        "link_text": doc.get("link_text", ""),
        "hindi_excerpt": text[:PREVIEW_CHARS],
        "has_more_text": len(text) > PREVIEW_CHARS,
    }


from fastapi.staticfiles import StaticFiles

_FRONTEND_DIST = _REPO_ROOT / "frontend" / "dist"
if _FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
