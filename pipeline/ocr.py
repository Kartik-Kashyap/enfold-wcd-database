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

Batching the pdfplumber read (above) bounds peak memory by chunk size for an
*average* PDF, but a large scanned-looking booklet still peaks in the hundreds
of MB per ``pdfplumber.open()`` regardless of how small the chunk is -- the
cost is pdfminer decoding the embedded images each page holds, not the number
of pages resident at once. Above ``TEXT_LAYER_PDFTOTEXT_MB`` the text layer is
read with poppler's ``pdftotext`` in a subprocess instead: it walks the same
xref without pulling any decoded image data into this process, so a 46 MB
booklet costs a pipe buffer here rather than proportional resident memory.
Smaller PDFs keep the pdfplumber path, which reads embedded fonts/layout
pdftotext sometimes gets wrong on typeset text.
"""

from __future__ import annotations

import gc
import os
import re
import subprocess
from pathlib import Path

import pdfplumber
import pytesseract
from pdf2image import convert_from_path

from . import dates, jsonio, paths, quality
from .states import StateConfig

SAMPLE_PAGES = 3
RASTER_CHUNK_PAGES = 5
OCR_DPI = 150
# Pages per reopen when reading an *embedded* text layer.  Larger than the OCR
# chunk because extraction is far cheaper per page than rasterising, but still
# bounded on purpose -- see _read_text_layer for why the bound matters.
TEXT_CHUNK_PAGES = 24
# File-size threshold, in MB, above which the text layer is read with the
# pdftotext subprocess instead of pdfplumber -- see the module docstring.
# Overridable per-run via the same-named environment variable, so a
# memory-constrained box can lower it without a code change.
TEXT_LAYER_PDFTOTEXT_MB = 10
# Wall-clock budget for a single pdftotext/pdfinfo subprocess call.
PDFTOTEXT_TIMEOUT = 120


def _pdftotext_threshold_mb() -> float:
    """Read ``TEXT_LAYER_PDFTOTEXT_MB`` at call time, falling back to the default."""
    try:
        return float(os.environ.get("TEXT_LAYER_PDFTOTEXT_MB", TEXT_LAYER_PDFTOTEXT_MB))
    except ValueError:
        return TEXT_LAYER_PDFTOTEXT_MB
# Fallback when no state resolves an explicit language set; the default
# StateConfig.ocr_langs is the same.  Odisha opts into 'ori' via its state entry.
OCR_LANGS = "hin+eng"


def _tessdata_config() -> str:
    """Return a tesseract ``--tessdata-dir`` config from the ``TESSDATA_DIR`` env var.

    An escape hatch for swapping OCR models without touching code: point
    ``TESSDATA_DIR`` at a directory of ``*.traineddata`` files (they are plain
    data, so no sudo needed) to override the system tessdata directory.  Unset
    -- the default -- means "use the system models".

    Measured on the deployment box: ``tessdata_fast`` ran at the same speed as
    the stock Debian models (~35 s per dense Hindi page, identical output), so
    this stays unset in production; the OCR cost there is CPU-bound, not
    model-bound.
    """
    td = os.environ.get("TESSDATA_DIR", "").strip()
    return f"--tessdata-dir {td}" if td else ""


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
            "  The Hindi language pack ('hin') is required; Odisha PDFs also need\n"
            "  the Odia pack ('ori', e.g. sudo apt install tesseract-ocr-ori)."
        )
    pytesseract.pytesseract.tesseract_cmd = cmd
    return cmd


def check_hindi_langpack() -> bool:
    try:
        return "hin" in set(pytesseract.get_languages(config=""))
    except Exception:
        return True  # can't tell; let the OCR call surface any real problem


def _read_text_layer(pdf_path: Path, total_pages: int,
                     chunk_size: int = TEXT_CHUNK_PAGES) -> str:
    """Read the embedded text layer in page batches, reopening per batch.

    Reopening is the whole point.  pdfplumber -- and pdfminer beneath it --
    retains a ``PDFPage`` for every page touched, and each one holds its
    decoded resources, so reading a document in a single pass keeps all of it
    resident.  On the deployment box (956 MiB, no swap headroom) a 47 MB Delhi
    e-booklet did exactly that and the OOM killer took the process, losing the
    63 documents the run had already banked.

    Batching bounds peak memory by ``chunk_size`` rather than by page count,
    and ``flush_cache()`` drops each page's decoded images as soon as its text
    is out.  The cost is re-parsing the xref once per batch, which is small
    next to extraction.

    Note this is the *text layer* path, which the old code reached rarely
    because the OCR gate was broken (finding #2).  Now that documents with a
    usable text layer actually take it, its memory profile matters.
    """
    parts: list[str] = []
    for start in range(0, total_pages, chunk_size):
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages[start:start + chunk_size]:
                text = page.extract_text()
                if text:
                    parts.append(text.strip())
                page.flush_cache()
        gc.collect()
    return "\n\n".join(parts)


def _pdftotext_page_count(pdf_path: Path) -> int:
    """Page count via ``pdfinfo``, without loading the document into Python."""
    result = subprocess.run(
        ["pdfinfo", str(pdf_path)],
        capture_output=True, text=True, timeout=PDFTOTEXT_TIMEOUT, check=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    raise ValueError("pdfinfo output had no 'Pages:' line")


def _pdftotext_range(pdf_path: Path, first: int, last: int) -> list[str]:
    """Per-page text for pages ``[first, last]`` (1-indexed, inclusive).

    ``pdftotext`` separates pages with a form-feed character; splitting on it
    gives the same one-string-per-page shape ``_read_text_layer`` builds from
    pdfplumber, so the two paths are interchangeable to their caller.
    """
    result = subprocess.run(
        ["pdftotext", "-enc", "UTF-8", "-f", str(first), "-l", str(last), str(pdf_path), "-"],
        capture_output=True, timeout=PDFTOTEXT_TIMEOUT, check=True,
    )
    pages = result.stdout.decode("utf-8", errors="replace").split("\x0c")
    # pdftotext trails the last page in the range with a form feed too, which
    # leaves an empty string after the split -- not a blank page, drop it.
    if pages and pages[-1] == "":
        pages = pages[:-1]
    return pages


def _read_text_layer_pdftotext(pdf_path: Path, total_pages: int,
                               chunk_size: int = TEXT_CHUNK_PAGES) -> str:
    """Read the embedded text layer via the ``pdftotext`` subprocess.

    Chunked the same way as :func:`_read_text_layer` -- one subprocess call
    per range rather than one for the whole document -- so a page range large
    enough to matter still bounds how much text sits in memory here at once.
    """
    parts: list[str] = []
    for start in range(1, total_pages + 1, chunk_size):
        end = min(start + chunk_size - 1, total_pages)
        for page_text in _pdftotext_range(pdf_path, start, end):
            text = page_text.strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def extract_text(pdf_path: Path | str, chunk_size: int = RASTER_CHUNK_PAGES,
                 lang: str = OCR_LANGS) -> tuple[str, bool]:
    """Return ``(text, was_ocr_used)`` for one PDF."""
    pdf_path = Path(pdf_path)
    parts: list[str] = []
    force_ocr = False
    total_pages = 0
    sample = ""

    size_mb = pdf_path.stat().st_size / (1024 * 1024) if pdf_path.exists() else 0.0
    use_pdftotext = size_mb > _pdftotext_threshold_mb()

    if use_pdftotext:
        try:
            total_pages = _pdftotext_page_count(pdf_path)
            sample = "\n".join(_pdftotext_range(pdf_path, 1, min(SAMPLE_PAGES, total_pages) or 1))
        except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError) as exc:
            print(f"    [pdftotext unavailable for {pdf_path.name}: {exc}] falling back to pdfplumber")
            use_pdftotext = False
            total_pages = 0

    if not use_pdftotext:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                total_pages = len(pdf.pages)
                sample = ""
                for page in pdf.pages[:SAMPLE_PAGES]:
                    sample += page.extract_text() or ""
        except Exception as exc:
            print(f"    [pdfplumber failed: {exc}] falling back to OCR")
            force_ocr = True

    if not force_ocr:
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
            layer = ("Unicode Indic"
                     if (quality.DEVANAGARI_RE.search(sample) or quality.ODIA_RE.search(sample))
                     else "readable Latin")
            reader = "pdftotext" if use_pdftotext else "pdfplumber"
            print(f"    [usable text layer: {layer}] using {reader}, skipping OCR")

    # Deliberately outside the `with`/subprocess above: the handle is released
    # first, so the batched read below is the only thing holding the document
    # open.  A large PDF pinned by both would defeat the batching.
    if not force_ocr:
        if use_pdftotext:
            try:
                return _read_text_layer_pdftotext(pdf_path, total_pages), False
            except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
                print(f"    [pdftotext failed for {pdf_path.name}: {exc}] falling back to pdfplumber")
        return _read_text_layer(pdf_path, total_pages), False

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
                ocr_text = pytesseract.image_to_string(img, lang=lang,
                                                       config=_tessdata_config())
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


def process_state(state: StateConfig, limit: int | None = None, flush_every: int = 1) -> int:
    """OCR/extract every un-processed PDF for one state. Returns count processed."""
    paths.configure_stdout()
    cmd = configure_tesseract()
    print(f"\n=== Extracting text: {state.name} ===")
    print(f"  tesseract: {cmd}")
    langs = getattr(state, "ocr_langs", OCR_LANGS)
    print(f"  ocr langs: {langs}")
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
            text, was_ocr_used = extract_text(pdf_path, lang=langs)

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
                # Full-text extraction beats the crawl-time best effort (which
                # only saw the text layer); fall back to it for scanned PDFs
                # that had no text layer at crawl time.
                "document_date": dates.extract_date(text) or meta.get("document_date"),
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
