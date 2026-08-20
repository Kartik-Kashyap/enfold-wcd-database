import os
import json
import torch
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

def chunk_text_for_translation(text, max_chars=400):
    """
    Safely splits long text into chunks of at most `max_chars`
    without risking infinite loops or memory leaks.
    """
    if not text:
        return []
        
    chunks = []
    paragraphs = [p.strip() for p in text.split('\n') if p.strip()]
    
    for p in paragraphs:
        while len(p) > max_chars:
            # Look for a clean split point (period, comma, danda, or space)
            split_idx = -1
            for sep in ['। ', '. ', '।', '. ', ', ', ' ']:
                idx = p.rfind(sep, 1, max_chars)
                if idx > 0:
                    split_idx = idx + len(sep)
                    break
            
            # If no separator found, hard slice at max_chars
            if split_idx <= 0:
                split_idx = max_chars
                
            chunk = p[:split_idx].strip()
            if chunk:
                chunks.append(chunk)
                
            p = p[split_idx:].strip()
            
        if p:
            chunks.append(p)
            
    return chunks

def translate_batch(texts, model, tokenizer, device):
    """Translates a list/batch of Hindi text strings into English."""
    if not texts:
        return []
        
    # Clean empty/whitespace-only items
    valid_texts = [t if t.strip() else "." for t in texts]
    
    inputs = tokenizer(
        valid_texts, 
        return_tensors="pt", 
        padding=True, 
        truncation=True, 
        max_length=512
    ).to(device)
    
    with torch.no_grad():
        translated_tokens = model.generate(**inputs, max_length=512)
        
    return tokenizer.batch_decode(translated_tokens, skip_special_tokens=True)

def translate_documents(input_file="processed_docs.json", output_file="translated_docs.json"):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Loading Helsinki-NLP model on: {device.upper()}...")
    
    model_name = "Helsinki-NLP/opus-mt-hi-en"
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_name).to(device)
    model.eval()

    if not os.path.exists(input_file):
        print(f"Error: {input_file} not found!")
        return
        
    with open(input_file, "r", encoding="utf-8") as f:
        documents = json.load(f)

    # Resume capability
    translated_docs = []
    processed_ids = set()
    if os.path.exists(output_file):
        try:
            with open(output_file, "r", encoding="utf-8") as f:
                translated_docs = json.load(f)
                processed_ids = {doc["id"] for doc in translated_docs}
            print(f"Resuming translation: {len(processed_ids)} files already completed.")
        except Exception:
            pass

    remaining_docs = [d for d in documents if d["id"] not in processed_ids]
    print(f"Total documents to translate: {len(remaining_docs)}")

    for idx, doc in enumerate(remaining_docs, start=1):
        print(f"\n[{idx}/{len(remaining_docs)}] Translating: {doc['filename']}")
        
        # 1. Translate Title
        original_title = doc.get("inferred_title", "")
        english_title = original_title
        if original_title and original_title.strip():
            try:
                english_title = translate_batch([original_title], model, tokenizer, device)[0]
            except Exception as e:
                print(f"  Title translation failed: {e}")

        # 2. Translate Body Text
        original_text = doc.get("text", "")
        english_text_chunks = []
        
        if original_text:
            chunks = chunk_text_for_translation(original_text, max_chars=400)
            print(f"  Total chunks to translate: {len(chunks)}")
            
            batch_size = 16  # Batch size for RTX 2050
            for i in range(0, len(chunks), batch_size):
                batch = chunks[i:i + batch_size]
                try:
                    translations = translate_batch(batch, model, tokenizer, device)
                    english_text_chunks.extend(translations)
                except Exception as e:
                    print(f"  Batch {i} failed: {e}")
                    english_text_chunks.extend(batch)

        english_text = "\n\n".join(english_text_chunks)
        
        translated_doc = doc.copy()
        translated_doc["title_english"] = english_title
        translated_doc["text_english"] = english_text
        
        translated_docs.append(translated_doc)
        
        # Save to disk incrementally after each document
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(translated_docs, f, ensure_ascii=False, indent=2)
            
        print(f"  Saved! EN Title: '{english_title[:60]}...'")

    print(f"\n All translations finished! Saved to '{output_file}'.")

if __name__ == "__main__":
    translate_documents()