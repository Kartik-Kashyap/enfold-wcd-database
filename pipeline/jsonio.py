"""Atomic, batched JSON persistence.

Review finding #12: ``cg/ocr.py`` and ``cg/translate_docs.py`` both rewrote
their entire JSON output after every single document, inside the per-document
loop -- O(n^2) writes.  Fine at 4 documents, slow at thousands.

``BatchedJsonWriter`` keeps the same on-disk format (a JSON array, so existing
committed data stays readable) but flushes every N records instead of every
one, and writes via a temp file + ``os.replace`` so an interrupted run can
never leave a truncated file behind -- which matters, because the resume logic
in the OCR stage reads this file back.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def read_json(path: Path | str, default: Any = None) -> Any:
    """Read JSON, returning ``default`` on a missing or corrupt file."""
    p = Path(path)
    if not p.exists():
        return default
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def write_json_atomic(path: Path | str, payload: Any) -> None:
    """Write JSON to ``path`` atomically."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class BatchedJsonWriter:
    """Accumulate records in memory, flush to a JSON array every N adds."""

    def __init__(self, path: Path | str, existing: list | None = None, flush_every: int = 10):
        self.path = Path(path)
        self.records: list = list(existing or [])
        self.flush_every = max(1, flush_every)
        self._since_flush = 0

    def add(self, record: Any) -> None:
        self.records.append(record)
        self._since_flush += 1
        if self._since_flush >= self.flush_every:
            self.flush()

    def flush(self) -> None:
        if self._since_flush == 0 and self.path.exists():
            return
        write_json_atomic(self.path, self.records)
        self._since_flush = 0

    def __enter__(self) -> "BatchedJsonWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Flush even on failure: a partial run is worth keeping, and the resume
        # logic depends on the file reflecting what actually completed.
        self.flush()
