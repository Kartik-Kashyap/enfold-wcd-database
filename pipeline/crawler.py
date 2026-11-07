"""Polite, state-parameterised crawler.

One crawler for every state (review finding #7 -- this replaces the byte-identical
``cg/crawler.py`` and ``bihar/scrap.py`` pair, whose only differences were the
start URL, state name and download directory; those now live in
``pipeline/states.py``).

Politeness, review finding #8.  The old crawler recursed into every same-domain
link with no delay, no ``robots.txt`` check, and a ``User-Agent`` spoofing
Chrome on Windows.  Getting Enfold's IP blocked by a state government server
would stop the project cold.  This version:

* reads and obeys ``robots.txt`` (including any ``Crawl-delay`` it declares),
* sleeps between every request,
* identifies itself honestly, with a contact address,
* walks breadth-first with an explicit queue instead of recursing (no
  ``RecursionError`` on a densely linked site),
* resumes: PDFs already recorded or already on disk are not re-fetched,
* batches metadata writes instead of rewriting the file per PDF.
"""

from __future__ import annotations

import os
import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urldefrag, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from requests import certs as requests_certs

from . import dates, jsonio, paths
from .states import StateConfig

DEFAULT_DELAY = 1.0
SKIP_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".xlsx", ".xls",
                   ".doc", ".docx", ".ppt", ".pptx", ".mp4", ".mp3", ".rar", ".csv")

PROJECT_URL = "https://github.com/Kartik-Kashyap/Scraper"


def response_is_pdf(content_type: str, sniff: bytes = b"") -> bool:
    """True when an HTTP response is a PDF, decided from its Content-Type.

    State portals frequently serve documents through handler URLs whose path has
    no file extension -- UP's ``DownloadFileGO.ashx?Id=1031``, for example --
    so a link branch that only matches ``*.pdf`` never fires for them.  When the
    crawler follows such a link and the server answers ``application/pdf``, the
    body is a document to save, not a page to discard.  Some servers use
    ``application/octet-stream`` for PDFs too, where a ``%PDF`` magic-byte sniff
    disambiguates; anything else (Word/Excel handlers, images) stays out of
    scope, matching the ``SKIP_EXTENSIONS`` policy on the link branch.
    """
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct.startswith("application/pdf"):
        return True
    if ct in ("application/octet-stream", "binary/octet-stream", "application/download"):
        return sniff.startswith(b"%PDF")
    return False


def build_user_agent() -> str:
    """Identify the project, not a fake browser.

    Set ``CRAWLER_CONTACT`` to a real address you monitor -- a server admin who
    can reach you sends an email; one who cannot sends a firewall rule.
    """
    contact = os.environ.get("CRAWLER_CONTACT", "").strip()
    suffix = f"; contact: {contact}" if contact else "; contact: set CRAWLER_CONTACT env var"
    return f"EnfoldChildRightsCrawler/1.0 (+{PROJECT_URL}{suffix})"


def verify_for(state: StateConfig) -> bool | str:
    """The ``verify=`` argument to use for this state's requests.

    ``True`` -- the requests default, verify against the standard trust store --
    for every state whose portal presents a complete TLS chain, which is all of
    them but Karnataka.

    Karnataka's portal renewed its leaf certificate onto GlobalSign's newer R46
    intermediate but still serves the *previous* intermediate in its handshake,
    so the chain it sends cannot be verified by anyone: ``requests`` raises
    ``CERTIFICATE_VERIFY_FAILED`` and ``curl`` exits 60.  Browsers appear to work
    only because they chase the certificate's AIA extension and cache the
    missing intermediate themselves.  A state that names an ``extra_ca`` file
    (``pipeline/certs/``) gets the same treatment: that certificate is written
    alongside the standard roots into a combined bundle, and the handshake is
    then verified normally.

    Appending a certificate only ever *adds* a trusted intermediate.  Nothing is
    skipped: a chain that does not reach a real, already-trusted root still
    fails, so this is not the same as ``verify=False``.  The alternative --
    disabling verification for the whole portal -- would leave the corpus open
    to a forged certificate and a tampered document.
    """
    if not state.extra_ca:
        return True

    extra = paths.CERTS_DIR / state.extra_ca
    if not extra.exists():
        print(f"  [tls] {state.key}: {state.extra_ca} is not in {paths.CERTS_DIR} -- "
              f"using the default trust store; requests will fail if this portal's "
              f"chain is still incomplete")
        return True

    combined = paths.CA_BUNDLE_CACHE / f"{state.key}.pem"
    # Rebuild when absent, or when the vendored certificate has been replaced.
    if not combined.exists() or combined.stat().st_mtime < extra.stat().st_mtime:
        paths.CA_BUNDLE_CACHE.mkdir(parents=True, exist_ok=True)
        base = Path(requests_certs.where()).read_bytes().rstrip() + b"\n"
        combined.write_bytes(base + extra.read_bytes())
        print(f"  [tls] {state.key}: trust store = default roots + {state.extra_ca}")
    return str(combined)


def determine_category(url: str, page_title: str, link_text: str) -> str:
    """Infer document category from URL, page heading, or link text.

    Checks English, Devanagari and Kannada keywords side by side -- the first two
    are kept as-is from the original, which got them right.  Kannada is needed
    because Karnataka's own link text is just "Download"; the signal comes from
    the table's subject cell, which the caller passes in as ``link_text`` once it
    has been captured (see :func:`parse_listing_row`).
    """
    combined = f"{url} {page_title} {link_text}".lower()

    if any(k in combined for k in ["act", "अधिनियम", "rules", "नियम",
                                   "ಅಧಿನಿಯಮ", "ಕಾಯ್ದೆ", "ನಿಯಮ"]):
        return "Acts & Rules"
    if any(k in combined for k in ["circular", "परिपत्र", "notification", "अधिसूचना", "order", "आदेश",
                                   "ಸುತ್ತೋಲೆ", "ಪರಿಪತ್ರ", "ಅಧಿಸೂಚನೆ", "ಆದೇಶ"]):
        return "Circulars & Orders"
    if any(k in combined for k in ["scheme", "योजना", "program", "programme",
                                   "ಯೋಜನೆ", "ಕಾರ್ಯಕ್ರಮ"]):
        return "Schemes & Programs"
    if any(k in combined for k in ["sop", "guideline", "दिशा-निर्देश", "disha-nirdesh",
                                   "ಮಾರ್ಗಸೂಚಿ", "ನಿರ್ದೇಶನ"]):
        return "SOPs & Guidelines"
    if any(k in combined for k in ["report", "प्रतिवेदन", "annual",
                                   "ವರದಿ", "ವಾರ್ಷಿಕ"]):
        return "Reports"
    return "General / Uncategorized"


def sanitize_filename(text: str, fallback_index: int) -> str:
    clean = re.sub(r"[^\w\s-]", "", text or "").strip()
    ascii_only = re.sub(r"[^\x00-\x7F]+", "", clean).strip()
    ascii_only = re.sub(r"\s+", "_", ascii_only)
    return ascii_only[:50] if ascii_only else f"document_{fallback_index}"


def filename_from_url(url: str, fallback_index: int, taken: set[str] | None = None) -> str:
    """Name a PDF from its URL's basename rather than its link text.

    Karnataka links every document through ``<a>Download</a>`` pointing at
    ``uploads/media_to_upload<id>.pdf``, so link-text naming would produce
    ``Download_1.pdf``, ``Download_2.pdf``, ... -- names that carry no
    information and would collide the moment the crawl order changed.  The URL
    basename is unique per document and stable across re-crawls, which also
    keeps the resume logic honest.  Falls back to the ``document_<n>``
    convention when a URL has no usable basename; ``taken`` disambiguates two
    URLs that happen to share one.
    """
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(urlparse(url).path).name).strip("._-")
    if stem.lower().endswith(".pdf"):
        stem = stem[:-4]
    if not stem:
        return f"document_{fallback_index}.pdf"
    candidate = f"{stem}.pdf"
    if taken and candidate in taken:
        candidate = f"{stem}_{fallback_index}.pdf"
    return candidate


# Karnataka's listing tables label their columns in Kannada.  Each header cell is
# matched to the field it carries, so the mapping is driven by the table's own
# header row and not by column position: a table that gains, loses or reorders a
# column then degrades to "field missing" rather than to "field silently wrong".
_LISTING_COLUMNS = (
    ("ವಿಷಯ", "subject"),          # subject / title of the order
    ("ದಿನಾಂಕ", "order"),          # "ಆದೇಶ ಸಂಖ್ಯೆ / ದಿನಾಂಕ" -- order number + date
    ("ಮೂಲ", "source_org"),        # originating body, e.g. ಇಲಾಖೆ (department)
    ("ಮಾಧ್ಯಮ", "medium"),         # language of the document, e.g. ಕನ್ನಡ
    ("ಗಾತ್ರ", "size_reported"),   # portal-reported size (often boilerplate)
)

# A trailing dd.mm.yyyy / dd-mm-yyyy / dd/mm/yyyy at the end of the order cell,
# which is where the issue date sits: "ಮಮಇ 293 ಮಮಅ 2005, 05.05.2007".
_TRAILING_DATE_RE = re.compile(r"[,;]?\s*\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4}\s*$")
# Some rows label the date inline -- "ಮಮಇ 81 ಮಮಅ 2025, ದಿನಾಂಕ:04.04.2025" -- which
# leaves the bare label behind once the date is stripped.
_ORDER_LABEL_RE = re.compile(r"[,;]?\s*ದಿನಾಂಕ\s*[:ः]?\s*$")


def _cell_text(cell) -> str:
    """Visible text of a table cell, collapsed to single spaces."""
    return re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()


def listing_headers(table, data_row=None) -> list[str | None]:
    """Map a listing table's columns to canonical field names, from its header.

    A row only counts as a header if it looks like one -- a ``<th>`` row, or a
    first row carrying ``<strong>`` cells -- *and* it has at least two cells.
    When the caller passes the row it is reading, the header must also not be
    wider than that row, since the mapping is positional.

    That guard is not hypothetical (wcd /84/gradation-lists.../kn).  The table
    there opens with a single caption cell spanning all five columns::

        ಮಹಿಳಾ ಮತ್ತು ಮಕ್ಕಳ ಅಭಿವೃದ್ದಿ ... ವಿವಿಧ ವೃಂದಗಳ ದಿನಾಂಕ:01.01.2025ರಲ್ಲಿದ್ದಂತೆ
        ಜ್ಯೇಷ್ಠತಾ ಪಟ್ಟಿಗಳು.

    Its text contains the ದಿನಾಂಕ marker, so reading it as a header maps column 0
    to ``order`` -- and every row's *serial number* is then captured as its
    government order number.  A column we fail to read is recoverable later; a
    column we read wrongly is not, so an unrecognisable table yields nothing.
    """
    header_row = None
    for row in table.find_all("tr"):
        if row.find("th"):
            header_row = row
            break
    if header_row is None:
        # Some portals style the header as a plain first row of <td><strong>.
        first = table.find("tr")
        if first is not None and first.find("strong"):
            header_row = first
    if header_row is None:
        return []

    cells = header_row.find_all(["th", "td"])
    # One cell means a caption spanning the table, not a header.
    if len(cells) < 2:
        return []
    if data_row is not None and len(cells) > len(data_row.find_all(["td", "th"])):
        return []
    return [
        next((field for marker, field in _LISTING_COLUMNS if marker in _cell_text(cell)), None)
        for cell in cells
    ]


def parse_listing_row(link) -> dict:
    """Capture the listing-table row a PDF link sits in, if there is one.

    Returns canonical fields (``subject``, ``order_no``, ``table_date``,
    ``source_org``, ``medium``, ``size_reported``), or ``{}`` when the link is
    not inside a table we recognise.  Nothing is guessed: a column is read only
    when the table's own header names it.

    The issue date is the real prize.  It comes from the portal's structured
    markup rather than from OCR, so it is available for scanned documents that
    have no text layer at all.
    """
    row = link.find_parent("tr")
    table = row.find_parent("table") if row is not None else None
    if row is None or table is None:
        return {}
    columns = listing_headers(table, row)
    if not columns:
        return {}

    cells: dict[str, str] = {}
    for field, cell in zip(columns, row.find_all(["td", "th"])):
        text = _cell_text(cell)
        if field and text:
            cells[field] = text

    captured: dict[str, str] = {
        field: cells[field]
        for field in ("subject", "source_org", "medium", "size_reported")
        if field in cells
    }
    if "order" in cells:
        order_text = cells["order"]
        # The order number is the cell minus its trailing date and date label.
        order_no = _TRAILING_DATE_RE.sub("", order_text)
        order_no = _ORDER_LABEL_RE.sub("", order_no).strip(" ,;:")
        if order_no:
            captured["order_no"] = order_no
        iso = dates.extract_date(order_text)
        if iso:
            captured["table_date"] = iso
    return captured


class Politeness:
    """robots.txt compliance plus a rate limit, per domain."""

    def __init__(self, user_agent: str, delay: float = DEFAULT_DELAY, obey_robots: bool = True):
        self.user_agent = user_agent
        self.delay = delay
        self.obey_robots = obey_robots
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._last_request = 0.0

    def _parser_for(self, url: str) -> RobotFileParser | None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._parsers:
            rp = RobotFileParser()
            rp.set_url(urljoin(origin, "/robots.txt"))
            try:
                rp.read()
                print(f"  [robots.txt] loaded for {origin}")
            except Exception as exc:
                print(f"  [robots.txt] unreadable for {origin} ({exc}) -- treating site as allowed")
                rp = None
            self._parsers[origin] = rp
            # Honour a site-declared crawl delay if it is stricter than ours.
            if rp is not None:
                try:
                    declared = rp.crawl_delay(self.user_agent)
                    if declared and float(declared) > self.delay:
                        self.delay = float(declared)
                        print(f"  [robots.txt] honouring declared Crawl-delay: {self.delay}s")
                except Exception:
                    pass
        return self._parsers[origin]

    def allowed(self, url: str) -> bool:
        if not self.obey_robots:
            return True
        rp = self._parser_for(url)
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request = time.monotonic()


def _next_file_index(metadata: list[dict]) -> int:
    highest = 0
    for entry in metadata:
        match = re.search(r"_(\d+)\.pdf$", entry.get("filename", ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def crawl_state(
    state: StateConfig,
    max_depth: int = 2,
    delay: float = DEFAULT_DELAY,
    obey_robots: bool = True,
    max_pages: int | None = None,
) -> int:
    """Crawl one state's portal. Returns the number of new PDFs downloaded."""
    paths.configure_stdout()
    user_agent = build_user_agent()
    headers = {"User-Agent": user_agent, "Accept": "text/html,application/pdf,*/*"}
    police = Politeness(user_agent, delay=delay, obey_robots=obey_robots)
    verify = verify_for(state)

    print(f"\n=== Crawling {state.name} ===")
    print(f"  start:      {state.start_url}")
    print(f"  user-agent: {user_agent}")
    print(f"  delay:      {delay}s   robots.txt: {'obeyed' if obey_robots else 'IGNORED'}")

    metadata: list[dict] = jsonio.read_json(state.crawl_metadata, default=[]) or []
    seen_pdf_urls = {m.get("pdf_url") for m in metadata if m.get("pdf_url")}
    file_index = _next_file_index(metadata)
    # URL-named states need to know which names are already spent, so a re-crawl
    # (or two URLs sharing a basename) cannot silently overwrite a stored PDF.
    taken_names: set[str] = set()
    if state.filename_from_url:
        taken_names = {m.get("filename") for m in metadata if m.get("filename")}
        if state.pdf_dir.exists():
            taken_names |= {p.name for p in state.pdf_dir.iterdir() if p.suffix.lower() == ".pdf"}
    print(f"  resuming:   {len(metadata)} PDFs already recorded")

    visited: set[str] = set()
    queue: list[tuple[str, int, str, str]] = [(state.start_url, 0, state.start_url, "")]
    new_downloads = 0
    pages_fetched = 0

    with jsonio.BatchedJsonWriter(state.crawl_metadata, existing=metadata, flush_every=5) as writer:
        def record(url: str, source: str, link_text: str, filename: str,
                   category: str, target: Path, table_meta: dict | None = None) -> None:
            table_meta = table_meta or {}
            entry = {
                "filename": filename,
                # Stored with forward slashes, relative to the repo root
                # (review finding #3) so it resolves on any OS.
                "file_path": paths.repo_relative(target),
                "pdf_url": url,
                "source_page": source,
                "state": state.name,
                "state_key": state.key,
                "category": category,
                "link_text": link_text,
                # The document's own issue date.  A date read from the portal's
                # listing table is authoritative and beats the best-effort read
                # of the PDF's text layer -- and unlike that read it works for
                # scanned documents that carry no text layer at all.
                "document_date": table_meta.get("table_date")
                                  or dates.extract_date_from_pdf(target),
                # When we scraped it -- distinct from the document date.
                "crawled_at": date.today().isoformat(),
            }
            # Only present for states whose portals publish a listing table
            # (currently Karnataka); the five older states record exactly the
            # keys they always did.
            entry.update(table_meta)
            writer.add(entry)

        while queue:
            current_url, depth, source_page, link_text = queue.pop(0)
            current_url, _ = urldefrag(current_url)
            if current_url in visited or depth > max_depth:
                continue
            if max_pages is not None and pages_fetched >= max_pages:
                print(f"\n  [limit] stopping after {max_pages} pages")
                break
            visited.add(current_url)

            if not police.allowed(current_url):
                print(f"  [robots.txt] disallowed, skipping: {current_url}")
                continue

            print(f"\n Scanning [{state.name}] [depth {depth}]: {current_url}")
            police.wait()
            try:
                response = requests.get(current_url, headers=headers, timeout=12,
                                        verify=verify)
                # Status first, then Content-Type (review finding #13): an error
                # page must never reach the parser downstream.
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "")

                if "text/html" not in content_type:
                    # A document served through a handler URL (UP's
                    # DownloadFile*.ashx?Id=...) has no .pdf extension for the
                    # link branch to match, so decide from the Content-Type and
                    # save it exactly like a direct *.pdf link.  Anything that
                    # is not a PDF is skipped, matching SKIP_EXTENSIONS.
                    if response_is_pdf(content_type, response.content[:16]) \
                            and current_url not in seen_pdf_urls:
                        seen_pdf_urls.add(current_url)
                        filename = f"{sanitize_filename(link_text or 'Document', file_index)}_{file_index}.pdf"
                        file_index += 1
                        state.pdf_dir.mkdir(parents=True, exist_ok=True)
                        target = state.pdf_dir / filename
                        category = determine_category(current_url, "", link_text)
                        print(f"  [PDF via handler] {category} | {filename}")
                        try:
                            with target.open("wb") as f:
                                f.write(response.content)
                        except Exception as exc:
                            print(f"  Failed saving {current_url}: {exc}")
                            target.unlink(missing_ok=True)
                            continue
                        record(current_url, source_page, link_text, filename, category, target)
                        new_downloads += 1
                    continue
            except Exception as exc:
                print(f" Skipping {current_url}: {exc}")
                continue

            pages_fetched += 1
            soup = BeautifulSoup(response.text, "html.parser")
            page_title = soup.title.string.strip() if soup.title and soup.title.string else ""

            for link in soup.find_all("a", href=True):
                href = (link.get("href") or "").strip()
                if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                    continue
                absolute_url, _ = urldefrag(urljoin(current_url, href))

                if href.lower().split("?")[0].endswith(".pdf"):
                    if absolute_url in seen_pdf_urls:
                        continue
                    if not police.allowed(absolute_url):
                        print(f"  [robots.txt] disallowed PDF, skipping: {absolute_url}")
                        continue
                    seen_pdf_urls.add(absolute_url)

                    anchor_text = link.text.strip() or "Untitled_Document"
                    # Read the listing row *before* naming the file: where a table
                    # exists its subject cell is what actually names the document,
                    # while the anchor text is usually just "Download".  The
                    # subject also gives category inference something to work with.
                    table_meta = parse_listing_row(link) if state.capture_table_metadata else {}
                    display_text = table_meta.get("subject") or anchor_text

                    if state.filename_from_url:
                        filename = filename_from_url(absolute_url, file_index, taken_names)
                        taken_names.add(filename)
                    else:
                        safe_name = sanitize_filename(display_text, file_index)
                        filename = f"{safe_name}_{file_index}.pdf"
                    file_index += 1
                    state.pdf_dir.mkdir(parents=True, exist_ok=True)
                    target = state.pdf_dir / filename
                    category = determine_category(current_url, page_title, display_text)

                    print(f"  [PDF] {category} | {filename}")
                    police.wait()
                    try:
                        pdf_res = requests.get(absolute_url, headers=headers, stream=True,
                                               timeout=30, verify=verify)
                        pdf_res.raise_for_status()
                        with target.open("wb") as f:
                            for chunk in pdf_res.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                    except Exception as exc:
                        print(f"  Failed download {absolute_url}: {exc}")
                        target.unlink(missing_ok=True)
                        continue

                    record(absolute_url, current_url, display_text, filename, category,
                           target, table_meta)
                    new_downloads += 1

                elif depth < max_depth:
                    parsed = urlparse(absolute_url)
                    if parsed.netloc == state.domain and parsed.scheme in ("http", "https"):
                        if not any(parsed.path.lower().endswith(ext) for ext in SKIP_EXTENSIONS):
                            if absolute_url not in visited:
                                queue.append((absolute_url, depth + 1,
                                              current_url, link.text.strip() or absolute_url))

    print(f"\n Crawl complete for {state.name}: {new_downloads} new PDFs, "
          f"{pages_fetched} pages fetched.")
    print(f" Metadata: {state.crawl_metadata}")
    return new_downloads


def fetch_missing_pdfs(state: StateConfig, delay: float = DEFAULT_DELAY) -> tuple[int, int]:
    """Re-download PDFs listed in ``crawl_metadata.json`` but absent on disk.

    Review finding #4: the app's download buttons are silently dead on a fresh
    clone because the PDF store is not (and should not be) in git.  The URLs are
    already sitting in ``crawl_metadata.json``, so a clone can reconstruct its
    own file store instead of shipping 24 MB of binaries.  Returns
    ``(fetched, failed)``.
    """
    paths.configure_stdout()
    user_agent = build_user_agent()
    headers = {"User-Agent": user_agent}
    police = Politeness(user_agent, delay=delay)
    # Built from the same config as the crawl, so a re-fetch of a portal with an
    # incomplete chain (Karnataka) does not fail where the crawl succeeded.
    verify = verify_for(state)

    metadata: list[dict] = jsonio.read_json(state.crawl_metadata, default=[]) or []
    if not metadata:
        print(f"[{state.name}] No crawl_metadata.json -- run `crawl` first.")
        return 0, 0

    missing = []
    for entry in metadata:
        target = state.pdf_dir / entry["filename"]
        if not target.exists():
            missing.append((entry, target))

    print(f"[{state.name}] {len(metadata)} PDFs recorded, {len(missing)} missing locally.")
    fetched = failed = 0
    for entry, target in missing:
        url = entry.get("pdf_url")
        if not url:
            failed += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"  fetching {entry['filename']}")
        police.wait()
        try:
            res = requests.get(url, headers=headers, stream=True, timeout=30, verify=verify)
            res.raise_for_status()
            with target.open("wb") as f:
                for chunk in res.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            fetched += 1
        except Exception as exc:
            print(f"    failed: {exc}")
            target.unlink(missing_ok=True)
            failed += 1

    print(f"[{state.name}] fetched {fetched}, failed {failed}.")
    return fetched, failed
