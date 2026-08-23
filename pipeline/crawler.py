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
from pathlib import Path
from urllib.parse import urljoin, urldefrag, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from . import jsonio, paths
from .states import StateConfig

DEFAULT_DELAY = 1.0
SKIP_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".xlsx", ".xls",
                   ".doc", ".docx", ".ppt", ".pptx", ".mp4", ".mp3", ".rar", ".csv")

PROJECT_URL = "https://github.com/Kartik-Kashyap/Scraper"


def build_user_agent() -> str:
    """Identify the project, not a fake browser.

    Set ``CRAWLER_CONTACT`` to a real address you monitor -- a server admin who
    can reach you sends an email; one who cannot sends a firewall rule.
    """
    contact = os.environ.get("CRAWLER_CONTACT", "").strip()
    suffix = f"; contact: {contact}" if contact else "; contact: set CRAWLER_CONTACT env var"
    return f"EnfoldChildRightsCrawler/1.0 (+{PROJECT_URL}{suffix})"


def determine_category(url: str, page_title: str, link_text: str) -> str:
    """Infer document category from URL, page heading, or link text.

    Checks English and Devanagari keywords side by side -- kept as-is from the
    original, which got this right.
    """
    combined = f"{url} {page_title} {link_text}".lower()

    if any(k in combined for k in ["act", "अधिनियम", "rules", "नियम"]):
        return "Acts & Rules"
    if any(k in combined for k in ["circular", "परिपत्र", "notification", "अधिसूचना", "order", "आदेश"]):
        return "Circulars & Orders"
    if any(k in combined for k in ["scheme", "योजना", "program", "programme"]):
        return "Schemes & Programs"
    if any(k in combined for k in ["sop", "guideline", "दिशा-निर्देश", "disha-nirdesh"]):
        return "SOPs & Guidelines"
    if any(k in combined for k in ["report", "प्रतिवेदन", "annual"]):
        return "Reports"
    return "General / Uncategorized"


def sanitize_filename(text: str, fallback_index: int) -> str:
    clean = re.sub(r"[^\w\s-]", "", text or "").strip()
    ascii_only = re.sub(r"[^\x00-\x7F]+", "", clean).strip()
    ascii_only = re.sub(r"\s+", "_", ascii_only)
    return ascii_only[:50] if ascii_only else f"document_{fallback_index}"


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

    print(f"\n=== Crawling {state.name} ===")
    print(f"  start:      {state.start_url}")
    print(f"  user-agent: {user_agent}")
    print(f"  delay:      {delay}s   robots.txt: {'obeyed' if obey_robots else 'IGNORED'}")

    metadata: list[dict] = jsonio.read_json(state.crawl_metadata, default=[]) or []
    seen_pdf_urls = {m.get("pdf_url") for m in metadata if m.get("pdf_url")}
    file_index = _next_file_index(metadata)
    print(f"  resuming:   {len(metadata)} PDFs already recorded")

    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(state.start_url, 0)]
    new_downloads = 0
    pages_fetched = 0

    with jsonio.BatchedJsonWriter(state.crawl_metadata, existing=metadata, flush_every=5) as writer:
        while queue:
            current_url, depth = queue.pop(0)
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
                response = requests.get(current_url, headers=headers, timeout=12)
                # Status first, then Content-Type (review finding #13): an error
                # page must never reach the parser downstream.
                response.raise_for_status()
                if "text/html" not in response.headers.get("Content-Type", ""):
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

                    link_text = link.text.strip() or "Untitled_Document"
                    safe_name = sanitize_filename(link_text, file_index)
                    filename = f"{safe_name}_{file_index}.pdf"
                    file_index += 1
                    state.pdf_dir.mkdir(parents=True, exist_ok=True)
                    target = state.pdf_dir / filename
                    category = determine_category(current_url, page_title, link_text)

                    print(f"  [PDF] {category} | {filename}")
                    police.wait()
                    try:
                        pdf_res = requests.get(absolute_url, headers=headers, stream=True, timeout=30)
                        pdf_res.raise_for_status()
                        with target.open("wb") as f:
                            for chunk in pdf_res.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                    except Exception as exc:
                        print(f"  Failed download {absolute_url}: {exc}")
                        target.unlink(missing_ok=True)
                        continue

                    writer.add({
                        "filename": filename,
                        # Stored with forward slashes, relative to the repo root
                        # (review finding #3) so it resolves on any OS.
                        "file_path": paths.repo_relative(target),
                        "pdf_url": absolute_url,
                        "source_page": current_url,
                        "state": state.name,
                        "state_key": state.key,
                        "category": category,
                        "link_text": link_text,
                    })
                    new_downloads += 1

                elif depth < max_depth:
                    parsed = urlparse(absolute_url)
                    if parsed.netloc == state.domain and parsed.scheme in ("http", "https"):
                        if not any(parsed.path.lower().endswith(ext) for ext in SKIP_EXTENSIONS):
                            if absolute_url not in visited:
                                queue.append((absolute_url, depth + 1))

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
            res = requests.get(url, headers=headers, stream=True, timeout=30)
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
