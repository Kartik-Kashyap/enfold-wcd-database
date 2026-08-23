"""Text chunking with exact source offsets.

Kept dependency-free on purpose: the indexer needs chromadb and
sentence-transformers, but chunking is pure string logic and both the tests and
the UI should be able to import it without loading a 2 GB torch stack.
"""

from __future__ import annotations

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100


def chunk_with_offsets(text: str, chunk_size: int = CHUNK_SIZE,
                       overlap: int = CHUNK_OVERLAP) -> list[tuple[str, int, int]]:
    """Split ``text`` into ``(chunk, start, end)`` triples.

    Review finding #11.  The old indexer stored a Hindi excerpt as
    ``text_hi[i * 700 : (i + 1) * 700]`` -- reconstructing an offset into the
    *Hindi* source from the *English* chunk index.  Translation changes length,
    so the two drifted apart chunk over chunk and the "corresponding" Hindi
    excerpt stopped corresponding.

    Offsets are therefore recorded at chunk time.  ``text[start:end].strip()``
    is exactly the returned chunk, so a consumer can widen the window around a
    hit without guessing.
    """
    if not text:
        return []
    step = max(1, chunk_size - overlap)
    out: list[tuple[str, int, int]] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + chunk_size, length)
        raw = text[start:end]
        if raw.strip():
            out.append((raw.strip(), start, end))
        if end >= length:
            break
        start += step
    return out
