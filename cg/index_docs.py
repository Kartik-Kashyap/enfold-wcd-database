import json
import os
import chromadb
from sentence_transformers import SentenceTransformer

def chunk_text(text, chunk_size=800, overlap=100):
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start += chunk_size - overlap
    return chunks

def build_vector_database(json_path="translated_docs.json", db_path="./chroma_db"):
    print("Loading multilingual embedding model...")
    embedding_model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    
    client = chromadb.PersistentClient(path=db_path)
    
    try:
        client.delete_collection("child_portal_docs")
    except Exception:
        pass

    collection = client.create_collection(
        name="child_portal_docs",
        metadata={"hnsw:space": "cosine"}
    )

    if not os.path.exists(json_path):
        print(f"Error: {json_path} not found.")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        documents = json.load(f)

    documents_to_add = []
    embeddings_to_add = []
    metadatas_to_add = []
    ids_to_add = []

    chunk_counter = 0

    print(f"Indexing bilingual chunks for {len(documents)} documents...")

    for doc in documents:
        doc_id = doc.get("id", "")
        filename = doc.get("filename", "")
        title_hi = doc.get("inferred_title", "")
        title_en = doc.get("title_english", title_hi)
        link_text = doc.get("link_text", "")
        state = doc.get("state", "Chhattisgarh")
        category = doc.get("category", "General / Uncategorized")
        file_path = doc.get("file_path", "")

        text_en = doc.get("text_english", "")
        text_hi = doc.get("text", "")

        # Chunk English text primarily for clean English excerpts
        primary_text = text_en if text_en.strip() else text_hi
        if not primary_text:
            continue

        chunks = chunk_text(primary_text, chunk_size=800, overlap=100)

        for i, chunk in enumerate(chunks):
            chunk_counter += 1
            documents_to_add.append(chunk)
            
            # Extract corresponding rough slice of original Hindi text
            hi_excerpt = text_hi[i * 700 : (i + 1) * 700] if text_hi else ""

            metadatas_to_add.append({
                "doc_id": doc_id,
                "filename": filename,
                "title_hi": title_hi,
                "title_en": title_en,
                "link_text": link_text,
                "state": state,
                "category": category,
                "file_path": file_path,
                "hindi_excerpt": hi_excerpt[:600],
                "chunk_index": i
            })
            ids_to_add.append(f"chunk_{chunk_counter}")

    print(f"Generating embeddings for {len(documents_to_add)} chunks...")
    embeddings = embedding_model.encode(documents_to_add, batch_size=32, show_progress_bar=True).tolist()

    collection.add(
        documents=documents_to_add,
        embeddings=embeddings,
        metadatas=metadatas_to_add,
        ids=ids_to_add
    )
    print(f"\nIndexed {len(documents_to_add)} bilingual chunks into ChromaDB!")

if __name__ == "__main__":
    build_vector_database()