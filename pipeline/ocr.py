"""PDF text extraction: cheap path first, Tesseract OCR only when needed.

Two fixes from the review live here.

Finding #2 (high).  The old gate was::

    def is_krutidev(text):
        kruti_signatures = ['NRR', 'kklu', '<+', 'f', 'j', 'd', 's', '=kk', 'â', 'ã']
        matches = sum(1 for char in kruti_signatures if char in text)
        return matches >= 2

Four of the ten "signatures" were the single letters f, j, d, s, so any English
sentence tripped it and ``force_ocr`` was set for essentially every document --
all four committed records show ``"was_ocr_used": true``.  The cheap pdfplumber
path was unreachable.  The gate is now ``quality.needs_ocr``: if the embedded
text layer already contains Devanagari, Tesseract has nothing to add.

Finding #3 (medium).  The Tesseract binary is resolved from ``TESSERACT_CMD`` or
``PATH`` instead of a hardcoded ``C:\\Program Files\\...`` path, and stored paths
use forward slashes relative to the repo root.

Kept from the original, because it was right: page-chunked rasterisation with
``del images`` + ``gc.collect()`` between chunks, which is what lets this handle
a 500-page PDF, and resume-on-restart.
"""

from __future__ import annotations

import gc
import re
from pathlib import Path

import pdfplumber
import pytesseract
from pdf2image import convert_from_path

from . import jsonio, paths, quality
from .states import StateConfig

SAMPLE_PAGES = 3
RASTER_CHUNK_PAGES = 5
OCR_DPI = 150
OCR_LANGS = "hin+eng"


class TesseractMissing(RuntimeError):
    pass


def configure_tesseract() -> str:
    """Point pytesseract at a real binary, or explain what to install."""
    cmd = paths.tesseract_cmd()
    if not cmd:
        raise TesseractMissing(
            "Tesseract OCR was not found.\n"
            "  Install it, then either put it on PATH or set TESSERACT_CMD.\n"
            "    Windows: https://github.com/UB-Mannheim/tesseract/wiki\n"
            "             set TESSERACT_CMD=C:\\Program Files\\Tesseract-OCR\\tesseract.exe\n"
            "    macOS:   brew install tesseract tesseract-lang\n"
            "    Debian:  sudo apt install tesseract-ocr tesseract-ocr-hin\n"
            "  The Hindi language pack ('hin') is required."
        )
    pytesseract.pytesseract.tesseract_cmd = cmd
    return cmd


def check_hindi_langpack() -> bool:
    try:
        return "hin" in set(pytesseract.get_languages(config=""))
    except Exception:
        return True  # can't tell; let the OCR call surface any real problem


def extract_text(pdf_path: Path | str, chunk_size: int = RASTER_CHUNK_PAGES) -> tuple[str, bool]:
    """Return ``(text, was_ocr_used)`` for one PDF."""
    pdf_path = Path(pdf_path)
    parts: list[str] = []
    force_ocr = False
    total_pages = 0

    try:
        with pdfplumber.open(pdf_path) as pdf:
            total_pages = len(pdf.pages)
            sample = ""
            for page in pdf.pages[:SAMPLE_PAGES]:
                sample += page.extract_text() or ""

            if quality.needs_ocr(sample):
                if not sample.strip():
                    reason = "no text layer (scanned image)"
                elif quality.looks_like_legacy_font(sample):
                    reason = "legacy font garble (Kruti Dev)"
                elif len(sample.strip()) < 100:
                    reason = "text layer too sparse"
                else:
                    reason = "text layer not readable"
                print(f"    [OCR needed: {reason}] rasterising at {OCR_DPI} DPI...")
                force_ocr = True
            else:
                # Fast path -- this is the branch the old heuristic made unreachable.
                layer = "Unicode Hindi" if quality.DEVANAGARI_RE.search(sample) else "readable Latin"
                print(f"    [usable text layer: {layer}] using pdfplumber, skipping OCR")
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        parts.append(text.strip())
                return "\n\n".join(parts), False
    except Exception as exc:
        print(f"    [pdfplumber failed: {exc}] falling back to OCR")
        force_ocr = True

    if not force_ocr:
        return "\n\n".join(parts), False

    if total_pages == 0:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                total_pages = len(pdf.pages)
        except Exception as exc:
            print(f"    [Error opening PDF]: {exc}")
            return "", True

    try:
        for start_page in range(1, total_pages + 1, chunk_size):
            end_page = min(start_page + chunk_size - 1, total_pages)
            images = convert_from_path(
                str(pdf_path), first_page=start_page, last_page=end_page, dpi=OCR_DPI
            )
            for img in images:
                ocr_text = pytesseract.image_to_string(img, lang=OCR_LANGS)
                if ocr_text.strip():
                    parts.append(ocr_text.strip())
            # Memory discipline: release each page chunk before rasterising the
            # next one, so peak RSS is bounded by chunk_size, not page count.
            del images
            gc.collect()
    except Exception as exc:
        print(f"    [Error during OCR]: {exc}")

    return "\n\n".join(parts), True


def infer_title(text: str, fallback: str) -> str:
    """First plausible line of the document, else the source link text."""
    for line in (ln.strip() for ln in text.split("\n")):
        if len(line) < 6 or len(line) > 200:
            continue
        if quality.looks_like_legacy_font(line):
            continue
        if not re.search(r"[\w\u0900-\u097F]", line):
            continue
        return line[:150]
    return fallback


def _next_doc_number(existing: list[dict]) -> int:
    highest = 0
    for doc in existing:
        match = re.search(r"(\d+)$", str(doc.get("id", "")))
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def process_state(state: StateConfig, limit: int | None = None, flush_every: int = 5) -> int:
    """OCR/extract every un-processed PDF for one state. Returns count processed."""
    paths.configure_stdout()
    cmd = configure_tesseract()
    print(f"\n=== Extracting text: {state.name} ===")
    print(f"  tesseract: {cmd}")
    if not check_hindi_langpack():
        print("  WARNING: Tesseract has no 'hin' language pack -- Hindi OCR will be poor.")

    crawl_meta = {m["filename"]: m for m in (jsonio.read_json(state.crawl_metadata, default=[]) or [])}

    if not state.pdf_dir.exists():
        print(f"  PDF store missing: {state.pdf_dir}")
        print(f"  Run:  python run.py fetch --state {state.key}")
        return 0

    existing: list[dict] = jsonio.read_json(state.processed_docs, default=[]) or []
    done = {doc["filename"] for doc in existing}
    doc_number = _next_doc_number(existing)

    pdf_files = sorted(p.name for p in state.pdf_dir.iterdir() if p.suffix.lower() == ".pdf")
    todo = [f for f in pdf_files if f not in done]
    if limit is not None:
        todo = todo[:limit]

    print(f"  {len(pdf_files)} PDFs on disk, {len(done)} already processed, {len(todo)} to do.")
    if not todo:
        return 0

    processed = 0
    with jsonio.BatchedJsonWriter(state.processed_docs, existing=existing, flush_every=flush_every) as writer:
        for idx, fname in enumerate(todo, start=1):
            pdf_path = state.pdf_dir / fname
            print(f"\n[{idx}/{len(todo)}] {fname}")
            text, was_ocr_used = extract_text(pdf_path)

            meta = crawl_meta.get(fname, {})
            fallback_title = meta.get("link_text") or Path(fname).stem
            writer.add({
                "id": f"doc_{doc_number}",
                "filename": fname,
                "inferred_title": infer_title(text, fallback_title),
                "file_path": paths.repo_relative(pdf_path),
                "pdf_url": meta.get("pdf_url", ""),
                "source_page": meta.get("source_page", ""),
                "state": meta.get("state", state.name),
                "state_key": state.key,
                "category": meta.get("category", "General / Uncategorized"),
                "link_text": meta.get("link_text", fname),
                "char_count": len(text),
                "was_ocr_used": was_ocr_used,
                "text": text,
            })
            doc_number += 1
            processed += 1
            print(f"    {len(text):,} chars extracted (ocr={was_ocr_used})")

    print(f"\n Extraction complete for {state.name}: {processed} documents.")
    print(f" Output: {state.processed_docs}")
    return processed
