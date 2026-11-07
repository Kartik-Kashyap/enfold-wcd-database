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
    # Tesseract languages for OCR.  Hindi/English covers most states; Odisha
    # documents are in Odia script, which needs the 'ori' pack.  Adding 'ori'
    # to every state costs ~40% OCR time per page, so it is opt-in here.
    ocr_langs: str = "hin+eng"
    # ISO 639-1 code of the documents' own language, recorded in the vector
    # index so a Kannada hit is not mislabelled as Hindi (see pipeline/index.py).
    language_code: str = "hi"
    # Karnataka serves every PDF from `uploads/media_to_upload<id>.pdf` behind a
    # link whose text is the single word "Download", so the usual
    # link-text-derived name would make every file `Download_<n>.pdf`.  Naming
    # from the URL instead gives stable, unique, re-crawl-safe filenames.
    filename_from_url: bool = False
    # Karnataka lists each document in a table row that carries the subject,
    # order number, issue date, originating body and language.  Capturing the
    # row means those fields are known from the portal rather than guessed from
    # the PDF text later -- and the issue date survives even for scanned
    # documents that have no text layer to extract a date from.
    capture_table_metadata: bool = False
    # Some portals serve an incomplete TLS chain -- the leaf plus an *old*
    # intermediate that did not sign it -- and every ordinary client then rejects
    # the handshake (requests raises CERTIFICATE_VERIFY_FAILED, curl exits 60).
    # Naming a PEM from `pipeline/certs/` here appends that certificate to the
    # standard trust store for this state's requests: the chain is still verified
    # end to end, it just gets the intermediate the server forgot to send.  This
    # does NOT disable verification.  See `crawler.verify_for` for why.
    extra_ca: str | None = None

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
    "odisha": StateConfig(
        key="odisha",
        name="Odisha",
        start_url="https://wcd.odisha.gov.in/",
        data_dirname="odisha",
        pdf_dirname="odwcd_all_pdfs",
        ocr_langs="hin+eng+ori",
    ),
    "up": StateConfig(
        key="up",
        name="Uttar Pradesh",
        start_url="https://balvikasup.gov.in/",
        data_dirname="up",
        pdf_dirname="upwcd_all_pdfs",
    ),
    "delhi": StateConfig(
        key="delhi",
        name="Delhi",
        start_url="https://wcd.delhi.gov.in/",
        data_dirname="delhi",
        pdf_dirname="dwcd_all_pdfs",
    ),
    "karnataka": StateConfig(
        key="karnataka",
        name="Karnataka",
        start_url="https://wcd.karnataka.gov.in/",
        data_dirname="karnataka",
        pdf_dirname="kwcd_all_pdfs",
        # Kannada documents need the 'kan' pack, which is NOT installed on the
        # Oracle box (it has eng/hin/ori/osd only).  OCR for this state needs
        # `sudo apt install tesseract-ocr-kan` by whoever has root; until then
        # the crawl and its metadata still work, only text extraction is blocked.
        ocr_langs="kan+eng",
        language_code="kn",
        filename_from_url=True,
        capture_table_metadata=True,
        # This portal renewed its leaf onto GlobalSign's R46 intermediate but
        # still serves the previous one, so the chain it sends does not verify
        # on any client.  The certificate it forgot to send is vendored here.
        extra_ca="globalsign-gcc-r46-ov-tls-ca-2025.pem",
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
