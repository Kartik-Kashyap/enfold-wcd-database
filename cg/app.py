import streamlit as st
import json
import os
import chromadb
from sentence_transformers import SentenceTransformer
import ollama

st.set_page_config(page_title="Child Rights Legal & Policy Portal", layout="wide")

st.title("🛡️ Child Rights & Policy Database")
st.caption("Search Acts, Circulars, Rules & SOPs across State Portals")

@st.cache_resource
def load_resources():
    client = chromadb.PersistentClient(path="./chroma_db")
    collection = client.get_collection("child_portal_docs")
    model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    
    with open("translated_docs.json", "r", encoding="utf-8") as f:
        raw_docs = json.load(f)
        
    return collection, model, raw_docs

collection, model, raw_docs = load_resources()

# Sidebar Controls
st.sidebar.header("⚙️ Search Configuration")
search_mode = st.sidebar.radio(
    "Select Search Mode:",
    ["🔍 Basic Search (Keyword / Title / Link Text)", "🧠 Advanced Search (Semantic / Full-Text Body)"]
)

st.sidebar.markdown("---")
st.sidebar.header("🎯 Filter Results")
selected_state = st.sidebar.selectbox(
    "State / UT:",
    ["All States", "Chhattisgarh", "Andhra Pradesh", "Delhi", "Kerala", "Maharashtra", "Uttar Pradesh", "West Bengal"]
)

selected_category = st.sidebar.selectbox(
    "Category:",
    ["All Categories", "Acts & Rules", "Circulars & Orders", "Schemes & Programs", "SOPs & Guidelines", "Reports"]
)

query = st.text_input("Enter search keywords, topic, or circular title:")

# -------------------------------------------------------------
# 1. BASIC SEARCH LOGIC
# -------------------------------------------------------------
if search_mode.startswith("🔍 Basic Search"):
    st.info("💡 **Basic Mode:** Scanning Document Titles (EN/HI), Hyperlink Text, and Filenames.")
    
    if query:
        q_lower = query.lower().strip()
        matched_docs = []

        for doc in raw_docs:
            doc_state = doc.get("state", "Chhattisgarh")
            doc_cat = doc.get("category", "General / Uncategorized")

            if selected_state != "All States" and doc_state != selected_state:
                continue
            if selected_category != "All Categories" and doc_cat != selected_category:
                continue

            t_hi = doc.get("inferred_title", "").lower()
            t_en = doc.get("title_english", "").lower()
            l_text = doc.get("link_text", "").lower()
            fname = doc.get("filename", "").lower()

            if q_lower in t_hi or q_lower in t_en or q_lower in l_text or q_lower in fname:
                matched_docs.append(doc)

        st.markdown(f"### Results Found: {len(matched_docs)}")

        for idx, doc in enumerate(matched_docs, start=1):
            t_en = doc.get("title_english", "Untitled")
            t_hi = doc.get("inferred_title", "")
            fname = doc.get("filename", "")
            fpath = doc.get("file_path", "")
            ltext = doc.get("link_text", "N/A")
            en_preview = doc.get("text_english", "")[:600]
            hi_preview = doc.get("text", "")[:600]

            with st.expander(f"📄 [{doc.get('state', 'State')}] [{doc.get('category', 'Category')}] — {t_en}", expanded=True):
                st.write(f"**Original Hindi Title:** {t_hi}")
                st.write(f"**Source Page Link Text:** `{ltext}`")
                st.write(f"**File Name:** `{fname}`")

                # Bilingual Excerpt Tabs
                tab1, tab2 = st.tabs(["🇬🇧 Translated English Preview", "🇮🇳 Original Hindi Preview"])
                with tab1:
                    st.info(en_preview + "..." if en_preview else "No English text available.")
                with tab2:
                    st.info(hi_preview + "..." if hi_preview else "No Hindi text available.")

                col1, col2 = st.columns([1, 4])
                with col1:
                    if os.path.exists(fpath):
                        with open(fpath, "rb") as f:
                            st.download_button("⬇️ Download PDF", f, file_name=fname, key=f"basic_dl_{idx}")

# -------------------------------------------------------------
# 2. ADVANCED SEARCH LOGIC
# -------------------------------------------------------------
else:
    st.info("🧠 **Advanced Mode:** Performing Semantic Vector Search over Full Document Bodies.")
    
    if query:
        query_vector = model.encode([query]).tolist()[0]
        
        filter_conditions = []
        if selected_state != "All States":
            filter_conditions.append({"state": selected_state})
        if selected_category != "All Categories":
            filter_conditions.append({"category": selected_category})

        where_filter = None
        if len(filter_conditions) == 1:
            where_filter = filter_conditions[0]
        elif len(filter_conditions) > 1:
            where_filter = {"$and": filter_conditions}

        results = collection.query(
            query_embeddings=[query_vector],
            n_results=10,
            where=where_filter
        )

        seen_files = set()
        matches_found = False

        if results and len(results["ids"][0]) > 0:
            st.markdown("### Semantically Relevant Documents")
            
            for idx in range(len(results["ids"][0])):
                meta = results["metadatas"][0][idx]
                chunk_text_en = results["documents"][0][idx]
                chunk_text_hi = meta.get("hindi_excerpt", "")
                fname = meta["filename"]

                if fname in seen_files:
                    continue
                seen_files.add(fname)
                matches_found = True

                t_en = meta.get("title_en", "Untitled")
                t_hi = meta.get("title_hi", "")
                doc_state = meta.get("state", "N/A")
                doc_cat = meta.get("category", "N/A")
                fpath = meta.get("file_path", "")

                with st.expander(f"📄 [{doc_state}] [{doc_cat}] — {t_en}", expanded=True):
                    st.write(f"**Hindi Title:** {t_hi}")

                    # Bilingual Excerpt Tabs
                    tab1, tab2 = st.tabs(["🇬🇧 Translated English Excerpt", "🇮🇳 Original Hindi Excerpt"])
                    with tab1:
                        st.info(chunk_text_en[:600] + "...")
                    with tab2:
                        st.info(chunk_text_hi + "..." if chunk_text_hi else "Hindi excerpt not available.")

                    col1, col2 = st.columns([1, 4])
                    with col1:
                        if os.path.exists(fpath):
                            with open(fpath, "rb") as f:
                                st.download_button("⬇️ Download PDF", f, file_name=fname, key=f"adv_dl_{idx}")

                    if st.button("✨ Summarize with Llama 3.2", key=f"adv_sum_{idx}"):
                        with st.spinner("Generating policy summary..."):
                            try:
                                prompt = (
                                    f"Summarize the following English excerpt from a government circular ({doc_state} - {doc_cat}).\n"
                                    "Provide 3 concise bullet points in English outlining actionable directives or policies:\n\n"
                                    f"{chunk_text_en[:2000]}\n\nSummary:"
                                )
                                response = ollama.generate(model="llama3.2", prompt=prompt)
                                st.markdown("#### 🤖 Policy Summary")
                                st.success(response["response"])
                            except Exception as e:
                                st.error(f"Ollama connection error: {e}")

        if not matches_found:
            st.warning("No documents matched the semantic query with current filters.")