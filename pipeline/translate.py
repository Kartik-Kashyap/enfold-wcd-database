"""On-demand English translation, quality-gated and cached.

There is deliberately **no bulk translation stage** in this pipeline.

The old ``cg/translate_docs.py`` translated every document up front with
``Helsinki-NLP/opus-mt-hi-en`` and wrote the results to
``translated_docs.json``, which the indexer then embedded -- that is review
finding #1, the critical one.  Two things replace it:

1. The search index is built from Hindi (``pipeline/index.py``), so nothing
   downstream depends on a translation being correct.
2. English is produced **only when someone asks for a specific excerpt**, by
   the local llama3.2 already running under Ollama, and the result is cached to
   disk so the same excerpt is never paid for twice.  Nothing translates at
   import time and nothing translates a corpus in the background.

Every output passes ``pipeline.quality.check_translation`` before it is shown.
A failing translation is reported as failed -- never displayed as if it were
fluent, correct English, which is precisely how the original bug reached users.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import jsonio, paths, quality

DEFAULT_MODEL = "llama3.2"
MAX_CHUNK_CHARS = 900          # per Ollama request; keeps latency sane on a laptop
MAX_EXCERPT_CHARS = 4000       # refuse to translate more than this in one click


@dataclass
class TranslationResult:
    status: str                       # "ok" | "failed" | "unavailable"
    english: str = ""
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    cached: bool = False
    model: str = DEFAULT_MODEL

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def split_for_translation(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split on sentence boundaries, including the Devanagari danda.

    Kept from the original implementation, which correctly split on ``'। '``
    rather than assuming English punctuation.
    """
    if not text:
        return []
    chunks: list[str] = []
    for para in (p.strip() for p in text.split("\n")):
        if not para:
            continue
        while len(para) > max_chars:
            split_idx = -1
            for sep in ("। ", ". ", "।", "? ", "! ", "; ", ", ", " "):
                idx = para.rfind(sep, 1, max_chars)
                if idx > 0:
                    split_idx = idx + len(sep)
                    break
            if split_idx <= 0:
                split_idx = max_chars
            head = para[:split_idx].strip()
            if head:
                chunks.append(head)
            para = para[split_idx:].strip()
        if para:
            chunks.append(para)
    return chunks


class TranslationCache:
    """Disk-backed cache so a given excerpt is translated at most once."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else paths.TRANSLATION_CACHE
        self._lock = threading.Lock()
        self._data: dict[str, dict] = jsonio.read_json(self.path, default={}) or {}

    @staticmethod
    def key(text: str, model: str) -> str:
        digest = hashlib.sha256(f"{model}\x00{text}".encode("utf-8")).hexdigest()
        return digest[:32]

    def get(self, text: str, model: str) -> TranslationResult | None:
        entry = self._data.get(self.key(text, model))
        if not entry:
            return None
        return TranslationResult(
            status=entry.get("status", "failed"),
            english=entry.get("english", ""),
            reasons=list(entry.get("reasons", [])),
            metrics=dict(entry.get("metrics", {})),
            cached=True,
            model=model,
        )

    def put(self, text: str, model: str, result: TranslationResult) -> None:
        with self._lock:
            self._data[self.key(text, model)] = {
                "status": result.status,
                "english": result.english,
                "reasons": result.reasons,
                "metrics": result.metrics,
                "model": model,
                "source_chars": len(text),
            }
            jsonio.write_json_atomic(self.path, self._data)

    def __len__(self) -> int:
        return len(self._data)


def is_available(model: str = DEFAULT_MODEL) -> tuple[bool, str]:
    """Check that Ollama is reachable and the model is pulled."""
    try:
        import ollama
    except ImportError:
        return False, "the `ollama` python package is not installed"
    try:
        listed = ollama.list()
    except Exception as exc:
        return False, f"Ollama is not reachable ({exc}). Start it with `ollama serve`."

    names = []
    for entry in getattr(listed, "models", None) or listed.get("models", []):
        name = getattr(entry, "model", None) or (entry.get("model") if isinstance(entry, dict) else None)
        if name:
            names.append(name)
    if not any(n == model or n.split(":")[0] == model.split(":")[0] for n in names):
        return False, f"model '{model}' is not pulled. Run: ollama pull {model}"
    return True, "ready"


_PROMPT = (
    "You are translating an excerpt from an Indian state government document "
    "(child welfare / policy) from Hindi into English.\n"
    "Rules:\n"
    "- Translate faithfully and literally. Do not summarise, explain, or add anything.\n"
    "- Preserve numbers, dates, amounts, section references and proper nouns exactly.\n"
    "- The source is OCR output and may be garbled. Translate what is legible; "
    "omit what is not. Never invent content to fill a gap.\n"
    "- Output the English translation only, with no preamble or commentary.\n\n"
    "Hindi text:\n{text}\n\nEnglish translation:"
)


def _translate_chunk(text: str, model: str) -> str:
    import ollama

    response = ollama.generate(
        model=model,
        prompt=_PROMPT.format(text=text),
        options={
            "temperature": 0.1,   # translation, not composition
            "top_p": 0.9,
            "repeat_penalty": 1.1,
        },
    )
    raw = (response.get("response") if isinstance(response, dict) else response.response) or ""
    return raw.strip()


def translate_excerpt(
    text: str,
    model: str = DEFAULT_MODEL,
    cache: TranslationCache | None = None,
    force: bool = False,
    max_chars: int = MAX_EXCERPT_CHARS,
) -> TranslationResult:
    """Translate one excerpt on demand. Cached, guarded, and bounded in size.

    Returns a :class:`TranslationResult`; callers must check ``.ok`` before
    displaying ``.english``.  A guard failure is a result, not an exception --
    the point is that failures become visible instead of silent.
    """
    source = (text or "").strip()
    if not source:
        return TranslationResult("failed", reasons=["empty source"], model=model)

    if len(source) > max_chars:
        source = source[:max_chars]

    if cache is not None and not force:
        hit = cache.get(source, model)
        if hit is not None:
            return hit

    available, message = is_available(model)
    if not available:
        return TranslationResult("unavailable", reasons=[message], model=model)

    pieces: list[str] = []
    dropped = 0
    commentary_lines = 0
    try:
        for chunk in split_for_translation(source):
            # Don't ask the model about fragments there is nothing to translate
            # in -- a whitespace-only chunk is what provoked llama3.2 into
            # answering "I can't provide a translation of that text..." and
            # leaking that sentence into the output.
            if not quality.has_translatable_content(chunk):
                dropped += 1
                continue
            piece = _translate_chunk(chunk, model)
            if quality.is_refusal(piece):
                dropped += 1
                continue
            # A good translation can still carry an appended remark; drop those
            # lines rather than the whole chunk.
            piece, removed = quality.strip_meta_commentary(piece)
            commentary_lines += removed
            if piece:
                pieces.append(piece)
            else:
                dropped += 1
    except Exception as exc:
        return TranslationResult("unavailable", reasons=[f"Ollama error: {exc}"], model=model)

    english = "\n\n".join(p for p in pieces if p).strip()

    # Compare against only the source we actually translated, so dropped
    # fragments don't skew the length-ratio check.
    translated_source = "\n".join(
        c for c in split_for_translation(source) if quality.has_translatable_content(c)
    )
    check = quality.check_translation(translated_source or source, english)

    result = TranslationResult(
        status="ok" if check.ok else "failed",
        english=english if check.ok else "",
        reasons=check.reasons,
        metrics={**check.metrics,
                 "chunks_dropped": float(dropped),
                 "commentary_lines_removed": float(commentary_lines)},
        model=model,
    )
    # Cache failures too, so a known-bad excerpt does not re-burn the GPU.
    if cache is not None:
        cache.put(source, model, result)
    return result


def warm_cache(texts: list[str], model: str = DEFAULT_MODEL,
               cache: TranslationCache | None = None) -> dict[str, int]:
    """Optional: pre-translate a *specific, chosen* list of excerpts.

    Exists for the case where you want a handful of documents ready ahead of a
    demo.  It is never called automatically -- on-demand is the default so a
    laptop is not translating a corpus in the background.
    """
    cache = cache or TranslationCache()
    tally = {"ok": 0, "failed": 0, "unavailable": 0, "cached": 0}
    for i, text in enumerate(texts, start=1):
        result = translate_excerpt(text, model=model, cache=cache)
        tally["cached" if result.cached else result.status] += 1
        print(f"  [{i}/{len(texts)}] {result.status}"
              f"{' (cached)' if result.cached else ''}"
              f"{': ' + '; '.join(result.reasons) if result.reasons else ''}")
    return tally
