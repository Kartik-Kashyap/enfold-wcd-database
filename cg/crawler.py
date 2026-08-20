import os
import re
import sys
import json
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

VISITED_PAGES = set()
DOWNLOADED_PDFS = set()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
}

def determine_category(url, page_title, link_text):
    """Infer document category from page URL, heading, or link text."""
    combined_context = f"{url} {page_title} {link_text}".lower()
    
    if any(k in combined_context for k in ["act", " अधिनियम", "rules", "नियम"]):
        return "Acts & Rules"
    elif any(k in combined_context for k in ["circular", "परिपत्र", "notification", "अधिसूचना", "order", "आदेश"]):
        return "Circulars & Orders"
    elif any(k in combined_context for k in ["scheme", "योजना", "program", "programme"]):
        return "Schemes & Programs"
    elif any(k in combined_context for k in ["sop", "guideline", "दिशा-निर्देश", "disha-nirdesh"]):
        return "SOPs & Guidelines"
    elif any(k in combined_context for k in ["report", "प्रतिवेदन", "annual"]):
        return "Reports"
    return "General / Uncategorized"

def sanitize_filename(text, fallback_index):
    clean_text = re.sub(r'[^\w\s-]', '', text).strip()
    clean_ascii = re.sub(r'[^\x00-\x7F]+', '', clean_text).strip()
    if clean_ascii:
        return clean_ascii[:50]
    return f"document_{fallback_index}"

def crawl_and_scrape(current_url, base_domain, state_name, download_dir="cgwcd_all_pdfs", metadata_file="crawl_metadata.json", max_depth=2, current_depth=0):
    if current_depth > max_depth or current_url in VISITED_PAGES:
        return
        
    VISITED_PAGES.add(current_url)
    print(f"\n Scanning [{state_name}] [Depth {current_depth}]: {current_url}")
    
    try:
        response = requests.get(current_url, headers=HEADERS, timeout=12)
        if "text/html" not in response.headers.get("Content-Type", ""):
            return
        response.raise_for_status()
    except Exception as e:
        print(f" Skipping {current_url}: {e}")
        return

    soup = BeautifulSoup(response.text, "html.parser")
    page_title = soup.title.string.strip() if soup.title and soup.title.string else ""

    metadata_list = []
    if os.path.exists(metadata_file):
        try:
            with open(metadata_file, "r", encoding="utf-8") as f:
                metadata_list = json.load(f)
        except Exception:
            pass

    for link in soup.find_all("a", href=True):
        href = link.get("href").strip()
        absolute_url = urljoin(current_url, href)
        
        if href.lower().endswith(".pdf") and absolute_url not in DOWNLOADED_PDFS:
            DOWNLOADED_PDFS.add(absolute_url)
            os.makedirs(download_dir, exist_ok=True)
            
            link_text = link.text.strip() or "Untitled_Document"
            file_index = len(DOWNLOADED_PDFS)
            safe_filename = sanitize_filename(link_text, file_index)
            file_path = os.path.join(download_dir, f"{safe_filename}_{file_index}.pdf")
            category = determine_category(current_url, page_title, link_text)
            
            print(f"  [PDF Found] Category: '{category}' | File: {safe_filename}...")
            
            try:
                pdf_res = requests.get(absolute_url, headers=HEADERS, stream=True, timeout=20)
                pdf_res.raise_for_status()
                with open(file_path, "wb") as f:
                    for chunk in pdf_res.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                
                meta_entry = {
                    "filename": f"{safe_filename}_{file_index}.pdf",
                    "file_path": file_path,
                    "pdf_url": absolute_url,
                    "source_page": current_url,
                    "state": state_name,
                    "category": category,
                    "link_text": link_text
                }
                metadata_list.append(meta_entry)
                
                with open(metadata_file, "w", encoding="utf-8") as f:
                    json.dump(metadata_list, f, ensure_ascii=False, indent=2)

            except Exception as e:
                print(f"  Failed download {absolute_url}: {e}")

    internal_links = set()
    for link in soup.find_all("a", href=True):
        href = link.get("href").strip()
        absolute_url = urljoin(current_url, href)
        parsed = urlparse(absolute_url)
        
        if parsed.netloc == base_domain and not href.startswith("#"):
            if not any(href.lower().endswith(ext) for ext in [".pdf", ".jpg", ".png", ".zip", ".xlsx"]):
                internal_links.add(absolute_url)

    for next_url in internal_links:
        crawl_and_scrape(next_url, base_domain, state_name, download_dir, metadata_file, max_depth, current_depth + 1)

if __name__ == "__main__":
    START_URL = "https://cgwcd.gov.in/"
    STATE = "Chhattisgarh"
    TARGET_DOMAIN = urlparse(START_URL).netloc
    
    crawl_and_scrape(
        current_url=START_URL,
        base_domain=TARGET_DOMAIN,
        state_name=STATE,
        download_dir="cgwcd_all_pdfs",
        metadata_file="crawl_metadata.json",
        max_depth=2
    )