import os
import gc
import json
import pytesseract
import pdfplumber
from pdf2image import convert_from_path

pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

def is_krutidev(text):
    """Detects legacy font garbled character signatures."""
    kruti_signatures = ['NRR', 'kklu', '<+', 'f', 'j', 'd', 's', '=kk', 'â', 'ã']
    matches = sum(1 for char in kruti_signatures if char in text)
    return matches >= 2

def extract_text_with_ocr_fallback(pdf_path, chunk_size=5):
    """Extracts text using pdfplumber, but forces Tesseract OCR if Kruti Dev is detected."""
    full_text = []
    force_ocr = False
    
  
    try:
        with pdfplumber.open(pdf_path) as pdf:
            sample_text = ""
            for page in pdf.pages[:3]:  
                t = page.extract_text() or ""
                sample_text += t
                
            if is_krutidev(sample_text):
                print(f"    [Kruti Dev Detected] Forcing Tesseract Image OCR to extract clean Unicode Hindi...")
                force_ocr = True
            elif len(sample_text.strip()) > 100:
               
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        full_text.append(t.strip())
                return "\n\n".join(full_text), False
    except Exception:
        force_ocr = True


    try:
        with pdfplumber.open(pdf_path) as pdf:
            total_pages = len(pdf.pages)
            
        for start_page in range(1, total_pages + 1, chunk_size):
            end_page = min(start_page + chunk_size - 1, total_pages)
            images = convert_from_path(pdf_path, first_page=start_page, last_page=end_page, dpi=150)
            
            for img in images:
                ocr_text = pytesseract.image_to_string(img, lang='hin+eng')
                if ocr_text.strip():
                    full_text.append(ocr_text.strip())
            
            del images
            gc.collect()

    except Exception as e:
        print(f"    [Error processing PDF]: {e}")

    return "\n\n".join(full_text), True

def process_all_pdfs(metadata_file="crawl_metadata.json", output_file="processed_docs.json"):

    crawl_meta = {}
    if os.path.exists(metadata_file):
        with open(metadata_file, "r", encoding="utf-8") as f:
            meta_list = json.load(f)
            crawl_meta = {m["filename"]: m for m in meta_list}

    processed_data = []
    processed_files = set()
    
    if os.path.exists(output_file):
        try:
            with open(output_file, "r", encoding="utf-8") as f:
                processed_data = json.load(f)
                processed_files = {doc["filename"] for doc in processed_data}
        except Exception:
            pass

    input_dir = "cgwcd_all_pdfs"
    pdf_files = [f for f in os.listdir(input_dir) if f.lower().endswith('.pdf')]
    
    for idx, fname in enumerate(pdf_files, start=1):
        if fname in processed_files:
            continue
            
        pdf_path = os.path.join(input_dir, fname)
        print(f"[{idx}/{len(pdf_files)}] Processing {fname}...")
        
        text, was_ocr_used = extract_text_with_ocr_fallback(pdf_path)
        
        meta = crawl_meta.get(fname, {})
        state = meta.get("state", "Chhattisgarh")
        category = meta.get("category", "General / Uncategorized")
        link_text = meta.get("link_text", fname)

        lines = [l.strip() for l in text.split('\n') if len(l.strip()) > 5]
        title = lines[0][:100] if lines and not is_krutidev(lines[0]) else link_text
        
        doc_entry = {
            "id": f"doc_{len(processed_data) + 1}",
            "filename": fname,
            "inferred_title": title,
            "file_path": pdf_path,
            "state": state,
            "category": category,
            "char_count": len(text),
            "was_ocr_used": was_ocr_used,
            "text": text
        }
        processed_data.append(doc_entry)
        
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(processed_data, f, ensure_ascii=False, indent=2)

    print(f"\n OCR & Processing Complete! Saved to '{output_file}'.")

if __name__ == "__main__":
    process_all_pdfs()