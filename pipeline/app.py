"""Streamlit UI for the Child Rights & Policy database.

Changes from the reviewed version:

* **Hindi is the source of truth.**  Search runs over Hindi chunks and the Hindi
  excerpt is shown first.  The old UI labelled fabricated MT output
  "🇬🇧 Translated English Preview" and fed it to Llama for "policy summaries"
  (review finding #1).
* **English on demand only.**  One button per result translates *that* excerpt
  via local llama3.2, guarded and cached.  Nothing is translated until asked,
  so opening the app costs nothing.
* **Finding #10** -- state and category filters are derived from the data that
  is actually present, with counts, instead of listing Kerala and Maharashtra
  when only Chhattisgarh is indexed.
* **Finding #4** -- a missing PDF says so and prints the command to fetch it,
  instead of silently rendering no download button.
* **Finding #9** -- a coverage panel states plainly how much of each state's
  crawl has actually been processed.

Run with:  python run.py app
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import streamlit as st

# Allow `streamlit run pipeline/app.py` as well as `python run.py app`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import jsonio, paths, translate  # noqa: E402
from pipeline.states import all_states  # noqa: E402

st.set_page_config(page_title="Child Rights Legal & Policy Portal", layout="wide")

st.title("🛡️ Child Rights & Policy Database")
st.caption("Search Acts, Circulars, Rules & SOPs across State Women & Child Development portals")

PREVIEW_CHARS = 900


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner="Loading search index...")
def load_search():
    import chromadb
    from sentence_transformers import SentenceTransformer

    client = chromadb.PersistentClient(path=str(paths.CHROMA_DIR))
    try:
        collection = client.get_collection(paths.COLLECTION_NAME)
    except Exception:
        collection = None
    model = SentenceTransformer(paths.EMBEDDING_MODEL)
    return collection, model


@st.cache_resource
def load_cache() -> translate.TranslationCache:
    return translate.TranslationCache()


@st.cache_data(show_spinner="Loading documents...")
def load_documents() -> tuple[list[dict], dict[str, dict]]:
    """Load Hindi source documents for every state, plus crawl coverage."""
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
    return docs, coverage


collection, embed_model = load_search()
raw_docs, coverage = load_documents()
tcache = load_cache()

if collection is None or collection.count() == 0:
    st.error(
        "No search index found. Build it with:\n\n"
        "```\npython run.py ocr --all\npython run.py index --all\n```"
    )

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
st.sidebar.header("⚙️ Search Configuration")
search_mode = st.sidebar.radio(
    "Select Search Mode:",
    ["🔍 Basic Search (Title / Link Text / Filename)",
     "🧠 Advanced Search (Semantic / Full Document Body)"],
)

st.sidebar.markdown("---")
st.sidebar.header("🎯 Filter Results")

# Finding #10: options come from the data, with counts, so an empty result set
# is never ambiguous between "no matches" and "we don't have this state".
state_counts = Counter(d.get("state", "Unknown") for d in raw_docs)
category_counts = Counter(d.get("category", "General / Uncategorized") for d in raw_docs)

state_options = ["All States"] + [f"{name} ({n})" for name, n in sorted(state_counts.items())]
category_options = ["All Categories"] + [f"{name} ({n})" for name, n in sorted(category_counts.items())]

selected_state_label = st.sidebar.selectbox("State / UT:", state_options)
selected_category_label = st.sidebar.selectbox("Category:", category_options)


def _strip_count(label: str) -> str:
    return label.rsplit(" (", 1)[0] if label.endswith(")") and " (" in label else label


selected_state = None if selected_state_label == "All States" else _strip_count(selected_state_label)
selected_category = None if selected_category_label == "All Categories" else _strip_count(selected_category_label)

st.sidebar.markdown("---")
with st.sidebar.expander("📊 Coverage (what's actually in here)", expanded=False):
    for name, info in coverage.items():
        st.markdown(
            f"**{name}** — {info['processed']} of {info['crawled']} crawled PDFs processed"
            f"  \n_{info['pdfs_on_disk']} PDF files on disk_"
        )
    st.caption(
        "Only processed documents are searchable. To process the rest:\n\n"
        "`python run.py fetch --all` → `python run.py ocr --all` → `python run.py index --all`"
    )

st.sidebar.markdown("---")
ollama_ok, ollama_msg = translate.is_available()
st.sidebar.caption(
    f"🌐 On-demand translation: {'✅ ready' if ollama_ok else '⚠️ ' + ollama_msg}"
    f"  \nCached excerpts: {len(tcache)}"
)

st.info(
    "🇮🇳 **Search runs on the original Hindi text.** English is generated only when you "
    "ask for it, per excerpt, by a local model — and it is checked before being shown. "
    "Always verify against the source PDF before relying on it."
)

query = st.text_input("Enter search keywords, topic, or circular title:")


# ---------------------------------------------------------------------------
# Shared result rendering
# ---------------------------------------------------------------------------
def render_download(file_path: str, filename: str, state_key: str, key: str) -> None:
    """Download button, or an explanation of why there isn't one (finding #4)."""
    resolved = paths.resolve_stored(file_path)
    if resolved.exists() and resolved.is_file():
        with resolved.open("rb") as f:
            st.download_button("⬇️ Download PDF", f, file_name=filename, key=key)
    else:
        st.caption(
            f"📄 `{filename}` is not in the local PDF store. "
            f"Fetch it with `python run.py fetch --state {state_key}`"
        )


def render_translation(hindi_text: str, key: str) -> None:
    """On-demand, cached, guarded translation of one excerpt."""
    cached = tcache.get(hindi_text.strip()[:translate.MAX_EXCERPT_CHARS], translate.DEFAULT_MODEL)
    state_key = f"xlate_{key}"

    if state_key not in st.session_state and cached is not None:
        st.session_state[state_key] = cached

    result = st.session_state.get(state_key)

    if result is None:
        clicked = st.button(
            "🌐 Translate this excerpt to English",
            key=f"btn_{key}",
            help="Runs the local llama3.2 on this excerpt only, then caches the result. "
                 "Nothing is translated until you click.",
            disabled=not ollama_ok,
        )
        if not ollama_ok:
            st.caption(f"Translation unavailable: {ollama_msg}")
        if clicked:
            with st.spinner("Translating this excerpt locally..."):
                result = translate.translate_excerpt(hindi_text, cache=tcache)
            st.session_state[state_key] = result
            st.rerun()
        return

    if result.ok:
        st.success(result.english)
        st.caption(
            f"🤖 Machine translation ({result.model}"
            f"{', cached' if result.cached else ''}) — passed automated quality checks, "
            "but not human-verified. Check against the Hindi original."
        )
    elif result.status == "unavailable":
        st.warning("Translation could not run: " + "; ".join(result.reasons))
    else:
        st.error(
            "⚠️ Translation rejected by the quality guard and **not shown**: "
            + "; ".join(result.reasons)
            + "\n\nThis is the guard working as intended — a fluent but wrong translation "
            "is worse than none. Read the Hindi original."
        )


def render_summary(hindi_text: str, doc_state: str, doc_cat: str, key: str) -> None:
    if st.button("✨ Summarise with Llama 3.2", key=f"sum_{key}", disabled=not ollama_ok):
        with st.spinner("Generating summary..."):
            try:
                import ollama
                prompt = (
                    f"The following is Hindi text from an Indian government document "
                    f"({doc_state} — {doc_cat}). It is OCR output and may be imperfect.\n"
                    "In English, give at most 3 bullet points covering only what the text "
                    "actually states. If the text is too garbled to summarise, say exactly "
                    "that instead of guessing. Do not invent policy details.\n\n"
                    f"{hindi_text[:2500]}\n\nSummary:"
                )
                response = ollama.generate(
                    model=translate.DEFAULT_MODEL,
                    prompt=prompt,
                    options={"temperature": 0.2},
                )
                st.markdown("#### 🤖 AI Summary")
                st.success(response["response"])
                st.caption("⚠️ AI-generated from OCR text. Unverified — confirm against the source PDF.")
            except Exception as exc:
                st.error(f"Ollama error: {exc}")


# ---------------------------------------------------------------------------
# 1. Basic search
# ---------------------------------------------------------------------------
if search_mode.startswith("🔍"):
    st.caption("💡 **Basic mode:** scans Hindi document titles, source hyperlink text, and filenames.")
    scan_body = st.checkbox("Also scan full document body (slower)", value=False)

    if query:
        q = query.lower().strip()
        matched = []
        for doc in raw_docs:
            if selected_state and doc.get("state") != selected_state:
                continue
            if selected_category and doc.get("category") != selected_category:
                continue
            haystack = " ".join([
                doc.get("inferred_title", ""),
                doc.get("link_text", ""),
                doc.get("filename", ""),
            ]).lower()
            if q in haystack or (scan_body and q in doc.get("text", "").lower()):
                matched.append(doc)

        st.markdown(f"### Results found: {len(matched)}")
        if not matched:
            st.warning(
                f"No documents matched “{query}” in the "
                f"{len(raw_docs)} processed documents currently indexed."
            )

        for idx, doc in enumerate(matched, start=1):
            title = doc.get("inferred_title") or doc.get("link_text") or "Untitled"
            header = f"📄 [{doc.get('state', '—')}] [{doc.get('category', '—')}] — {title}"
            with st.expander(header, expanded=idx <= 3):
                st.write(f"**Source page link text:** `{doc.get('link_text', 'N/A')}`")
                st.write(f"**File name:** `{doc.get('filename', '')}`")
                if doc.get("was_ocr_used"):
                    st.caption("ℹ️ Text extracted by OCR — expect some noise.")

                hindi_preview = (doc.get("text", "") or "")[:PREVIEW_CHARS]
                tab_hi, tab_en = st.tabs(["🇮🇳 Original Hindi (source)", "🇬🇧 English (on demand)"])
                with tab_hi:
                    st.info(hindi_preview + ("..." if len(doc.get("text", "")) > PREVIEW_CHARS else "")
                            if hindi_preview else "No text extracted from this PDF.")
                with tab_en:
                    if hindi_preview:
                        render_translation(hindi_preview, key=f"basic_{idx}")
                    else:
                        st.caption("Nothing to translate — no text was extracted.")

                col1, col2 = st.columns([1, 3])
                with col1:
                    render_download(doc.get("file_path", ""), doc.get("filename", ""),
                                    doc.get("state_key", "cg"), key=f"basic_dl_{idx}")
                with col2:
                    if hindi_preview:
                        render_summary(hindi_preview, doc.get("state", "—"),
                                       doc.get("category", "—"), key=f"basic_{idx}")

# ---------------------------------------------------------------------------
# 2. Advanced (semantic) search
# ---------------------------------------------------------------------------
else:
    st.caption(
        "🧠 **Advanced mode:** semantic vector search over full Hindi document bodies. "
        "The embedding model is multilingual, so an English query still matches Hindi text."
    )

    if query and collection is not None and collection.count() > 0:
        query_vector = embed_model.encode([query]).tolist()[0]

        conditions = []
        if selected_state:
            conditions.append({"state": selected_state})
        if selected_category:
            conditions.append({"category": selected_category})
        where_filter = None
        if len(conditions) == 1:
            where_filter = conditions[0]
        elif len(conditions) > 1:
            where_filter = {"$and": conditions}

        results = collection.query(
            query_embeddings=[query_vector],
            n_results=20,
            where=where_filter,
        )

        ids = results.get("ids", [[]])[0]
        if not ids:
            st.warning(
                "No documents matched the semantic query with the current filters. "
                f"({collection.count()} chunks indexed across {len(state_counts)} state(s).)"
            )
        else:
            st.markdown("### Semantically relevant documents")
            seen: set[str] = set()
            shown = 0

            for i in range(len(ids)):
                meta = results["metadatas"][0][i]
                chunk_hi = results["documents"][0][i]
                filename = meta.get("filename", "")
                if filename in seen:
                    continue
                seen.add(filename)
                shown += 1
                if shown > 10:
                    break

                distance = results.get("distances", [[]])[0][i] if results.get("distances") else None
                title = meta.get("title_hi") or meta.get("link_text") or "Untitled"
                header = f"📄 [{meta.get('state', '—')}] [{meta.get('category', '—')}] — {title}"

                with st.expander(header, expanded=shown <= 3):
                    if distance is not None:
                        st.caption(f"Similarity: {max(0.0, 1 - distance):.0%} · "
                                   f"chunk {meta.get('chunk_index', 0)} "
                                   f"(source chars {meta.get('hi_start', 0)}–{meta.get('hi_end', 0)})")
                    if meta.get("was_ocr_used"):
                        st.caption("ℹ️ Text extracted by OCR — expect some noise.")

                    tab_hi, tab_en = st.tabs(
                        ["🇮🇳 Matching Hindi excerpt (source)", "🇬🇧 English (on demand)"]
                    )
                    with tab_hi:
                        st.info(chunk_hi)
                    with tab_en:
                        render_translation(chunk_hi, key=f"adv_{i}")

                    col1, col2 = st.columns([1, 3])
                    with col1:
                        render_download(meta.get("file_path", ""), filename,
                                        meta.get("state_key", "cg"), key=f"adv_dl_{i}")
                    with col2:
                        render_summary(chunk_hi, meta.get("state", "—"),
                                       meta.get("category", "—"), key=f"adv_{i}")
