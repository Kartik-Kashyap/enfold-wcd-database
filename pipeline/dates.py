"""Document date extraction.

Government circulars carry their issue date in the header -- ``दिनांक 15/03/2024``
("dated 15/03/2024").  That date is what researchers mean by "show me documents
from 2020-2023", so it is extracted from the PDF text and stored alongside every
other metadata field (``document_date`` in crawl_metadata.json and
processed_docs.json, ``document_date_epoch`` in the vector index).

Not a crawl timestamp: ``crawled_at`` in the crawler records when *we* scraped a
document, which is useful in its own right but is not the document's date.
``extract_date`` looks for the document's own date, not the crawl date.

Rules that keep this honest:

* **Anchored dates win.**  ``दिनांक``/``दि.``/``dated`` carry the document date.
  A bare ``15/03/2024`` later in the text (a reference, a form date) only wins
  when no anchored date exists.
* **Deadlines lose.**  ``अंतिम तिथि 30/06/2026`` ("last date for application")
  is a deadline, not the document date.  It is only picked when nothing else
  exists.
* **Validation.**  Month 1-12, day 1-31, year 1990-2035 (rejects ``31/02/2024``
  and page-number/phone junk).
* **Hindi handled natively.**  Devanagari digits (``१५/०३/२०२४``) and Hindi
  month words (``15 मार्च 2024``) are recognised -- OCR output of scanned
  circulars is the common case here.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from . import jsonio, paths

# ---------------------------------------------------------------------------
# Hindi digit / month tables
# ---------------------------------------------------------------------------

HINDI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

_MONTH_BY_NAME: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "जनवरी": 1, "फरवरी": 2, "फ़रवरी": 2, "मार्च": 3, "अप्रैल": 4, "मई": 5,
    "जून": 6, "जुलाई": 7, "अगस्त": 8, "सितंबर": 9, "सितम्बर": 9,
    "अक्टूबर": 10, "नवंबर": 11, "दिसंबर": 12,
}

# Longest Hindi month names first is not needed (alternation matches a fixed
# string), but English names need the optional suffixes from longest to
# shortest for a greedy-but-correct match, e.g. "sep(?:tember)?".
_MONTH_ALT = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
    r"जनवरी|फ़रवरी|फरवरी|मार्च|अप्रैल|मई|जून|जुलाई|अगस्त|सितंबर|सितम्बर|"
    r"अक्टूबर|नवंबर|दिसंबर"
)

DATE_HEADER_CHARS = 4000   # documents carry their date in the header
MIN_YEAR, MAX_YEAR = 1990, 2035
ANCHOR_WINDOW = 60         # chars looked back before a date for an anchor

# Issue-date anchors: the words that *introduce* a document date.
_ISSUE_ANCHOR_RE = re.compile(r"दिनांक\s*[:ः]?|दि\.|dated|date\s*[:]")
# Deadline anchors: dates introduced by these are not the document date.
_DEADLINE_RE = re.compile(r"अंतिम तिथि|अंतिम दिनांक|अंतिम तारीख|last date")

# dd/mm/yyyy, dd-mm-yyyy, dd.mm.yyyy (digit-normalised; Hindi digits already
# translated by the caller before these run -- hence no Devanagari here).
_DATE_NUMERIC_RE = re.compile(
    r"(?<![\d/.-])(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})(?![\d/.-])"
)
# ISO order: yyyy-mm-dd.
_DATE_ISO_RE = re.compile(
    r"(?<![\d/.-])(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})(?![\d/.-])"
)
# Month-word form: "15 मार्च 2024", "12 March, 2024".
_DATE_MONTH_RE = re.compile(
    rf"(?<![\d/.-])(\d{{1,2}})\s*[,.\-]?\s+({_MONTH_ALT})\s*[,.\-]?\s+(\d{{4}})(?!\d)",
    re.IGNORECASE,
)


def _valid_iso(day: int, month: int, year: int) -> str | None:
    """ISO string for a real calendar date, or None (rejects 31/02/2024)."""
    if not (MIN_YEAR <= year <= MAX_YEAR):
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def date_epoch(iso: str | None) -> int | None:
    """Epoch seconds for a ``document_date`` ISO string, or None.

    Stored in the vector index alongside the ISO string because Chroma's range
    where-filters (``$gte``/``$lte``) work on numbers only -- ISO strings sort
    fine for display but cannot be ranged over.  Computed as UTC day
    arithmetic (not ``.timestamp()``, which doesn't exist on ``date`` and would
    bake in the machine's timezone offset), so the index and the app agree.
    """
    if not iso:
        return None
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return None
    return (d - date(1970, 1, 1)).days * 86400


def extract_date(text: str | None, limit: int = DATE_HEADER_CHARS) -> str | None:
    """Best date in ``text`` as ISO ``YYYY-MM-DD``, or None.

    Scans only the first ``limit`` characters -- the header, where circulars
    state their date.  All candidate dates are collected and scored:

    * +2 for a month-word form (stronger than a bare number),
    * +4 if introduced by an issue-date anchor (``दिनांक`` / ``दि.`` / ``dated``),
    * -3 if introduced by a deadline anchor (``अंतिम तिथि`` / ``last date``).

    Highest score wins; ties go to the earliest position in the text.
    """
    if not text:
        return None
    norm = text[:limit].translate(HINDI_DIGITS)

    candidates: list[tuple[int, int, str]] = []  # (score, position, iso)
    for match in _DATE_NUMERIC_RE.finditer(norm):
        iso = _valid_iso(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if iso:
            candidates.append((_score(norm, match.start(), is_month_word=False),
                               match.start(), iso))
    for match in _DATE_ISO_RE.finditer(norm):
        iso = _valid_iso(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        if iso:
            candidates.append((_score(norm, match.start(), is_month_word=False),
                               match.start(), iso))
    for match in _DATE_MONTH_RE.finditer(norm):
        month = _MONTH_BY_NAME.get(match.group(2).lower())
        iso = _valid_iso(int(match.group(1)), month, int(match.group(3))) if month else None
        if iso:
            candidates.append((_score(norm, match.start(), is_month_word=True),
                               match.start(), iso))

    if not candidates:
        return None
    return max(candidates, key=lambda c: (c[0], -c[1]))[2]  # score desc, position asc


def _score(norm: str, pos: int, is_month_word: bool) -> int:
    score = 2 if is_month_word else 1
    window = norm[max(0, pos - ANCHOR_WINDOW):pos]
    if _ISSUE_ANCHOR_RE.search(window):
        score += 4
    if _DEADLINE_RE.search(window):
        score -= 3
    return score


def extract_date_from_pdf(pdf_path: str | Path, sample_pages: int = 3) -> str | None:
    """Best-effort date from a PDF's text layer, no OCR.

    Used at crawl time: the PDF has just been downloaded, so reading the first
    pages' embedded text is cheap.  Scanned PDFs have no text layer and return
    None here; the OCR stage re-runs date extraction on the full extracted
    text, which is where those documents get their date.
    """
    try:
        import pdfplumber
    except ImportError:
        return None
    try:
        with pdfplumber.open(pdf_path) as pdf:
            sample = ""
            for page in pdf.pages[:sample_pages]:
                sample += page.extract_text() or ""
            return extract_date(sample)
    except Exception:
        return None  # never let a missing date fail a crawl


def backfill_state(state) -> tuple[int, int]:
    """Extract dates from already-extracted text into existing JSON metadata.

    Re-runs ``extract_date`` over every processed document's full text (the
    first 4,000 chars are what get scanned) and writes ``document_date`` into
    ``processed_docs.json``, then syncs matching entries into
    ``crawl_metadata.json`` by filename.  Gives the pre-existing cg/bihar data
    dates without re-OCR.  Returns ``(docs_total, docs_with_date)``.
    """
    paths.configure_stdout()
    docs = jsonio.read_json(state.processed_docs, default=[]) or []
    dated = 0
    for doc in docs:
        iso = extract_date(doc.get("text") or "")
        if iso:
            doc["document_date"] = iso
            dated += 1
    if docs:
        jsonio.write_json_atomic(state.processed_docs, docs)

    by_filename = {d.get("filename"): d.get("document_date") for d in docs}
    metadata = jsonio.read_json(state.crawl_metadata, default=[]) or []
    changed = 0
    for entry in metadata:
        iso = by_filename.get(entry.get("filename"))
        if iso and entry.get("document_date") != iso:
            entry["document_date"] = iso
            changed += 1
    if changed:
        jsonio.write_json_atomic(state.crawl_metadata, metadata)

    return len(docs), dated
