"""State registry.

Review finding #7: ``bihar/scrap.py`` and ``cg/crawler.py`` differed in exactly
four lines, and all four were configuration -- the start URL, the state name,
and the download directory.  So the crawler lives in one place now
(``pipeline/crawler.py``) and the per-state differences live here.

Adding a state is adding an entry to ``STATES``.  Nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from . import paths


@dataclass(frozen=True)
class StateConfig:
    key: str            # CLI handle, e.g. "cg"
    name: str           # Display name, e.g. "Chhattisgarh"
    start_url: str
    data_dirname: str   # Directory under the repo root holding this state's data
    pdf_dirname: str    # PDF store, nested inside the data dir

    @property
    def domain(self) -> str:
        return urlparse(self.start_url).netloc

    @property
    def data_dir(self) -> Path:
        return paths.REPO_ROOT / self.data_dirname

    @property
    def pdf_dir(self) -> Path:
        return self.data_dir / self.pdf_dirname

    @property
    def crawl_metadata(self) -> Path:
        return self.data_dir / "crawl_metadata.json"

    @property
    def processed_docs(self) -> Path:
        return self.data_dir / "processed_docs.json"


STATES: dict[str, StateConfig] = {
    "cg": StateConfig(
        key="cg",
        name="Chhattisgarh",
        start_url="https://cgwcd.gov.in/",
        data_dirname="cg",
        pdf_dirname="cgwcd_all_pdfs",
    ),
    "bihar": StateConfig(
        key="bihar",
        name="Bihar",
        start_url="https://wcdc.bihar.gov.in/",
        data_dirname="bihar",
        pdf_dirname="bhwcd_all_pdfs",
    ),
}


def get(key: str) -> StateConfig:
    try:
        return STATES[key.lower()]
    except KeyError:
        known = ", ".join(sorted(STATES))
        raise SystemExit(f"Unknown state '{key}'. Known states: {known}") from None


def all_states() -> list[StateConfig]:
    return list(STATES.values())


def resolve(keys: list[str] | None, want_all: bool) -> list[StateConfig]:
    if want_all or not keys:
        return all_states()
    return [get(k) for k in keys]
