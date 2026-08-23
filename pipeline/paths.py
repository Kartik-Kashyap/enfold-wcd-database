"""Portable path handling.

Review finding #3: paths were stored with Windows backslashes
(``"cgwcd_all_pdfs\\-_37.pdf"``) and the Tesseract binary was hardcoded to
``C:\\Program Files\\Tesseract-OCR\\tesseract.exe``.  Neither resolves on
macOS or Linux.  Everything here goes through ``pathlib`` and every path that
gets *written into JSON* is normalised to forward slashes, which both Windows
and POSIX accept on the way back in.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Repo root = parent of the `pipeline/` package.
REPO_ROOT = Path(__file__).resolve().parent.parent

# One shared vector store for every state, so the app's "All States" filter
# actually means something.  Kept out of git (see .gitignore).
CHROMA_DIR = REPO_ROOT / "chroma_db"
COLLECTION_NAME = "child_portal_docs"

# On-demand translation cache (see pipeline/translate.py).
TRANSLATION_CACHE = REPO_ROOT / "translation_cache.json"

EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def as_posix(path: Path | str) -> str:
    """Path -> string safe to store in JSON and re-read on any OS."""
    return Path(path).as_posix()


def repo_relative(path: Path | str) -> str:
    """Store paths relative to the repo root so the data survives a move."""
    p = Path(path)
    try:
        return as_posix(p.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return as_posix(p)


def resolve_stored(path_str: str) -> Path:
    """Inverse of :func:`repo_relative`, tolerant of legacy backslash paths."""
    if not path_str:
        return Path()
    normalised = str(path_str).replace("\\", "/")
    p = Path(normalised)
    if p.is_absolute():
        return p
    candidate = REPO_ROOT / p
    if candidate.exists():
        return candidate
    # Legacy records stored paths relative to the state dir, e.g.
    # "cgwcd_all_pdfs/-_37.pdf".  Try each state's data dir.
    for state_dir in REPO_ROOT.iterdir() if REPO_ROOT.exists() else []:
        if state_dir.is_dir():
            alt = state_dir / p
            if alt.exists():
                return alt
    return candidate


def tesseract_cmd() -> str | None:
    """Locate the Tesseract binary without hardcoding a Windows path.

    Order: ``TESSERACT_CMD`` env var -> whatever is on ``PATH``.  Returns
    ``None`` if neither resolves, so the caller can raise a useful error
    instead of failing deep inside pytesseract.
    """
    from shutil import which

    override = os.environ.get("TESSERACT_CMD")
    if override:
        return override
    return which("tesseract")


def configure_stdout() -> None:
    """Make Devanagari printable on a cp1252 Windows console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream and (stream.encoding or "").lower() != "utf-8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
