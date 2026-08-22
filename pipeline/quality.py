"""Automated guardrails for machine-translation output.

Review finding #1 (critical).  ``Helsinki-NLP/opus-mt-hi-en`` did not fail
loudly on hard input -- dense bureaucratic Hindi, OCR noise, repetition.  It
emitted fluent, grammatical English with no relation to the source, most often
memorised fragments of Watchtower / Jehovah's Witnesses literature, which is a
documented artefact of the OPUS parallel corpus the model was trained on.

Three of four committed documents had unusable titles and all four carried the
contamination somewhere in the body (doc_1 alone: 84 occurrences of "Jehovah").
Because the failure is *silent*, care is not a control -- this module is.

Scope, stated honestly: these checks catch gross failures -- contamination,
script errors, length blowups, degenerate repetition.  They cannot catch a
subtle mistranslation.  ``छत्तीसगढ़ महिला कोष`` -> "The six-month-old lady
Koss." is wrong but passes every mechanical check here.  That is exactly why
the search index is built from the Hindi source and never from a translation;
this guard is the second line of defence, not the first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
LATIN_RE = re.compile(r"[A-Za-z]")
_WORD_RE = re.compile(r"\w+", re.UNICODE)

# Signature vocabulary of the OPUS/Watchtower contamination.  Deliberately
# specific: these phrases essentially cannot occur in an Indian state
# government child-welfare circular, so a hit is decisive rather than
# suggestive.  Matched case-insensitively on word boundaries where sensible.
CONTAMINATION_PATTERNS: tuple[str, ...] = (
    r"\bjehovah\b",
    r"\bwatchtower\b",
    r"\bwatch\s+tower\b",
    r"\bkingdom\s+(?:preacher|publisher|hall|proclaimer)s?\b",
    r"\bjw\.org\b",
    r"\bbible\s+and\s+tract\s+society\b",
    r"\bbible\s+students?\b",
    r"\bawake\s*!",
    r"\bthe\s+watchtower\b",
    r"\bfield\s+ministry\b",
    r"\bpioneer\s+service\b",
    r"\bcongregation\s+(?:elder|meeting)s?\b",
    r"\bspiritual\s+paradise\b",
    r"\bnew\s+world\s+translation\b",
    r"\bgod'?s\s+kingdom\b",
)
_CONTAMINATION_RE = re.compile("|".join(CONTAMINATION_PATTERNS), re.IGNORECASE)

# An instruction-tuned model asked to translate will sometimes answer *about* the
# request instead of performing it -- refusing an empty chunk, apologising, or
# adding a preamble.  Observed from llama3.2 on a whitespace-only chunk:
#   "I can't provide a translation of that text as it appears to be a single
#    space character (" ") and does not contain any meaningful information."
# Fluent English that is not a translation is the same failure class as the
# original bug, so it is caught the same way.
META_COMMENTARY_PATTERNS: tuple[str, ...] = (
    r"\bi\s+(?:can'?t|cannot|am\s+unable\s+to|won'?t)\b",
    r"\bi\s+(?:don'?t|do\s+not)\s+(?:see|have|understand)\b",
    r"\bi'?m\s+ready\s+to\s+translate\b",
    r"\bi'?(?:ll|d)\s+(?:be\s+happy\s+to|gladly)?\s*translate\b",
    r"\b(?:can|could)\s+you\s+please\s+(?:provide|supply|share|clarify)\b",
    r"\bplease\s+(?:provide|paste|share|supply)\b",
    r"\bgo\s+ahead\s+and\s+(?:provide|paste|share)\b",
    r"\bwaiting\s+for\s+(?:the|your)\b",
    r"\bas\s+an\s+ai\b",
    r"\bappears?\s+to\s+be\s+(?:a\s+)?(?:single\s+)?(?:space|blank|empty)\b",
    r"\bdoes\s+not\s+contain\s+any\s+(?:meaningful|readable|translatable)\b",
    r"\bno\s+(?:hindi\s+)?text\s+(?:was\s+)?(?:provided|given|supplied)\b",
    r"\bhere\s+is\s+(?:the|my)\s+(?:english\s+)?translation\b",
    r"\bthe\s+(?:hindi\s+)?text\s+(?:you\s+)?provided\s+is\b",
    r"\bit\s+seems\s+(?:like\s+)?(?:you|there)\b",
    r"\b(?:note|disclaimer)\s*:\s*(?:this|the)\s+translation\b",
)
_META_RE = re.compile("|".join(META_COMMENTARY_PATTERNS), re.IGNORECASE)

# Hindi -> English character expansion measured on the committed corpus was
# ~1.09-1.12.  These bands are wide enough not to fight real translations and
# tight enough to catch a 14-character Hindi title becoming 160 characters of
# invented English.
LONG_TEXT_THRESHOLD = 40
LEN_RATIO_LONG = (0.45, 2.60)
LEN_RATIO_SHORT = (0.20, 6.00)

MIN_LATIN_RATIO = 0.60          # output should be predominantly Latin script
MAX_DEVANAGARI_RATIO = 0.15     # ...and not still mostly Devanagari
MAX_LINE_REPEAT_RATIO = 0.50    # degenerate-loop detector
MIN_DISTINCT_WORD_RATIO = 0.12  # ditto, at word level


@dataclass
class TranslationCheck:
    """Result of validating one translation against its source."""

    ok: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return "ok" if self.ok else "failed"

    def summary(self) -> str:
        return "passed" if self.ok else "; ".join(self.reasons)


def script_ratios(text: str) -> tuple[float, float]:
    """Return ``(devanagari_ratio, latin_ratio)`` over letter characters."""
    deva = len(DEVANAGARI_RE.findall(text))
    latin = len(LATIN_RE.findall(text))
    total = deva + latin
    if total == 0:
        return 0.0, 0.0
    return deva / total, latin / total


def find_contamination(text: str) -> list[str]:
    """Return the distinct contamination phrases present in ``text``."""
    hits = {m.group(0).lower() for m in _CONTAMINATION_RE.finditer(text)}
    return sorted(hits)


def find_meta_commentary(text: str) -> list[str]:
    """Return model-talking-about-the-task phrases present in ``text``."""
    hits = {m.group(0).lower().strip() for m in _META_RE.finditer(text)}
    return sorted(hits)


def is_refusal(text: str) -> bool:
    """True when a chunk's output is commentary rather than a translation.

    Applied per chunk before joining, so one unusable chunk (typically a
    whitespace-only fragment the model chose to answer *about*) does not
    contaminate an otherwise good excerpt.
    """
    stripped = (text or "").strip()
    if not stripped:
        return True
    hits = find_meta_commentary(stripped)
    if not hits:
        return False
    # Short output that is mostly commentary -> refusal.  A long output with one
    # incidental match is handled by check_translation instead.
    return len(stripped) < 400 or len(hits) >= 2


def strip_meta_commentary(text: str) -> tuple[str, int]:
    """Remove individual lines that are model commentary, not translation.

    An instruction-tuned model can append a remark to otherwise good output --
    observed: a valid translation followed by "I'm ready to translate. Please
    provide the Hindi text."  Whole-chunk rejection would throw away the good
    part, so commentary is removed line by line.  Returns
    ``(cleaned_text, lines_removed)``.
    """
    if not text:
        return "", 0
    kept: list[str] = []
    removed = 0
    for line in text.splitlines():
        if line.strip() and find_meta_commentary(line):
            removed += 1
            continue
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    # Collapse blank runs left behind by removed lines.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned, removed


def has_translatable_content(text: str) -> bool:
    """True when a chunk is worth sending to a translation model at all.

    Sending whitespace or punctuation-only fragments is what provoked the
    refusal in the first place.
    """
    stripped = (text or "").strip()
    if len(stripped) < 3:
        return False
    if DEVANAGARI_RE.search(stripped):
        return True
    # No Devanagari: only worth translating if there are real words.
    return len(_WORD_RE.findall(stripped)) >= 2


def _repetition_metrics(text: str) -> tuple[float, float]:
    """Return ``(max_line_repeat_ratio, distinct_word_ratio)``."""
    lines = [ln.strip() for ln in text.splitlines() if len(ln.strip()) > 12]
    line_repeat = 0.0
    if len(lines) >= 4:
        counts: dict[str, int] = {}
        for ln in lines:
            counts[ln] = counts.get(ln, 0) + 1
        line_repeat = max(counts.values()) / len(lines)

    words = [w.lower() for w in _WORD_RE.findall(text)]
    distinct = len(set(words)) / len(words) if len(words) >= 30 else 1.0
    return line_repeat, distinct


def check_translation(source: str, output: str) -> TranslationCheck:
    """Validate one Hindi -> English translation.

    Refuses rather than trusts: anything that trips a check is reported as
    failed so the caller can store/display a failure instead of fluent
    nonsense.
    """
    reasons: list[str] = []
    source = source or ""
    output = output or ""
    src_len = len(source.strip())
    out_len = len(output.strip())

    if src_len == 0:
        return TranslationCheck(False, ["empty source"], {})
    if out_len == 0:
        return TranslationCheck(False, ["empty translation"], {"source_chars": src_len})

    ratio = out_len / src_len
    lo, hi = LEN_RATIO_LONG if src_len >= LONG_TEXT_THRESHOLD else LEN_RATIO_SHORT
    if not (lo <= ratio <= hi):
        reasons.append(f"length ratio {ratio:.2f} outside [{lo}, {hi}]")

    deva_ratio, latin_ratio = script_ratios(output)
    if latin_ratio < MIN_LATIN_RATIO:
        reasons.append(f"output only {latin_ratio:.0%} Latin script")
    if deva_ratio > MAX_DEVANAGARI_RATIO:
        reasons.append(f"output still {deva_ratio:.0%} Devanagari (untranslated)")

    hits = find_contamination(output)
    if hits:
        reasons.append("training-corpus contamination: " + ", ".join(hits[:5]))

    meta = find_meta_commentary(output)
    if meta:
        reasons.append("model commentary instead of translation: " + ", ".join(meta[:3]))

    line_repeat, distinct_words = _repetition_metrics(output)
    if line_repeat > MAX_LINE_REPEAT_RATIO:
        reasons.append(f"{line_repeat:.0%} of lines identical (degenerate output)")
    if distinct_words < MIN_DISTINCT_WORD_RATIO:
        reasons.append(f"only {distinct_words:.0%} distinct words (degenerate output)")

    metrics = {
        "source_chars": float(src_len),
        "output_chars": float(out_len),
        "length_ratio": round(ratio, 3),
        "latin_ratio": round(latin_ratio, 3),
        "devanagari_ratio": round(deva_ratio, 3),
        "line_repeat_ratio": round(line_repeat, 3),
        "distinct_word_ratio": round(distinct_words, 3),
    }
    return TranslationCheck(not reasons, reasons, metrics)


def looks_like_legacy_font(text: str) -> bool:
    """Detect Kruti Dev / legacy-encoding garble via multi-character signatures.

    The single-letter signatures that broke the original check (``f``, ``j``,
    ``d``, ``s``) are gone -- those are what made it fire on ordinary English.
    """
    if not text:
        return False
    if DEVANAGARI_RE.search(text):
        return False
    signatures = ("NRR", "kklu", "=kk", "<+", "'kk", "vkS", "jgs", "fd;", "gksx",
                  "ç", "â", "ã", "¸", "Ø", "¹", "»")
    return sum(1 for s in signatures if s in text) >= 2


def looks_like_readable_latin(text: str) -> bool:
    """True when Latin-script text reads like real language, not encoding garble.

    Legacy-font bytes rendered as Latin (``NRRhlx<+ 'kklu vkS jgs fd;``) are
    almost all consonants; real English runs ~35-40% vowels.  A cheap vowel-ratio
    test separates the two without a dictionary.
    """
    letters = [c for c in text if c.isalpha() and ord(c) < 128]
    if len(letters) < 50:
        return False
    vowels = sum(1 for c in letters if c.lower() in "aeiou")
    return vowels / len(letters) >= 0.22


def needs_ocr(sample_text: str) -> bool:
    """True when a pdfplumber text sample cannot be trusted and OCR should run.

    Review finding #2: the old ``is_krutidev`` heuristic listed the single
    letters ``f``, ``j``, ``d``, ``s`` among its ten "signatures" and fired on
    ``matches >= 2``, so it returned True for any ordinary English sentence and
    the cheap extraction path was dead code -- every PDF paid for full image
    OCR (all four committed records show ``"was_ocr_used": true``).

    Note this is deliberately *not* the review's suggested one-liner
    ``return not DEVANAGARI.search(sample_text)``.  That fixes the false
    positives on Hindi but keeps them on genuinely English documents -- an
    English-only circular has no Devanagari and would still be rasterised for
    nothing.  What actually matters is whether the embedded text layer is
    *usable*:

    * too little text          -> scanned image, OCR
    * contains Devanagari      -> already Unicode Hindi, no OCR
    * legacy-font garble       -> OCR (this is the Kruti Dev case)
    * readable Latin script    -> usable English text layer, no OCR
    * anything else            -> let OCR try
    """
    sample = (sample_text or "").strip()
    if len(sample) < 100:
        return True
    if DEVANAGARI_RE.search(sample):
        return False
    if looks_like_legacy_font(sample):
        return True
    return not looks_like_readable_latin(sample)
