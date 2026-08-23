"""Tests for the automated guards that the review said this pipeline needed.

Review finding #1 closes with: "This failure mode is silent, so it needs an
automated guard, not just care."  A guard nobody tests is just more code that
might be silently wrong, so the guard has tests -- including tests built from
the actual fabricated output that shipped in ``cg/translated_docs.json``.

Run:  python -m pytest -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import quality
from pipeline.chunking import chunk_with_offsets
from pipeline.translate import split_for_translation


# ---------------------------------------------------------------------------
# Finding #2 — the OCR gate
# ---------------------------------------------------------------------------
class TestNeedsOcr:
    def test_plain_english_no_longer_forces_ocr(self):
        """The exact regression from the review.

        `is_krutidev("This is a plain English sentence from a government
        circular.")` returned True, so every PDF took the slow OCR path.
        """
        sample = ("This is a plain English sentence from a government circular. " * 4)
        assert quality.needs_ocr(sample) is False

    def test_devanagari_text_layer_skips_ocr(self):
        sample = "छत्तीसगढ़ शासन महिला एवं बाल विकास विभाग द्वारा जारी परिपत्र क्रमांक ४२१ दिनांक १५ मार्च २०२४। " * 3
        assert quality.needs_ocr(sample) is False

    def test_scanned_page_with_no_text_layer_needs_ocr(self):
        assert quality.needs_ocr("") is True
        assert quality.needs_ocr("   \n  \n ") is True

    def test_short_garbage_text_layer_needs_ocr(self):
        assert quality.needs_ocr("Page 1") is True

    def test_krutidev_garble_needs_ocr(self):
        garbled = "NRRhlx<+ 'kklu vkS jgs fd; gksx =kk ç" * 6
        assert quality.needs_ocr(garbled) is True

    def test_english_only_document_does_not_need_ocr(self):
        """Guards against the review's suggested one-liner.

        `return not DEVANAGARI.search(text)` would rasterise a genuinely
        English circular for no reason -- a milder version of the same bug.
        """
        english = (
            "Government of India, Ministry of Women and Child Development. "
            "Standard operating procedure for District Child Protection Units. "
            "All officers shall submit compliance reports by the tenth of each month."
        )
        assert quality.needs_ocr(english) is False

    def test_readable_latin_detection(self):
        assert quality.looks_like_readable_latin(
            "Government of India Ministry of Women and Child Development circular"
        ) is True
        assert quality.looks_like_readable_latin("NRRhlx<+ 'kklu vkS jgs fd; gksx =kk" * 3) is False


class TestLegacyFontDetection:
    def test_plain_english_is_not_legacy_font(self):
        assert quality.looks_like_legacy_font(
            "This is a plain English sentence from a government circular."
        ) is False

    def test_real_devanagari_is_not_legacy_font(self):
        assert quality.looks_like_legacy_font("छत्तीसगढ़ शासन") is False

    def test_krutidev_signatures_detected(self):
        assert quality.looks_like_legacy_font("NRR 'kklu =kk vkS") is True


# ---------------------------------------------------------------------------
# Finding #1 — the translation guard
# ---------------------------------------------------------------------------
class TestTranslationGuard:
    def test_rejects_the_actual_shipped_title(self):
        """`छत्तीसगढ़ शासन` -> Watchtower text, from cg/translated_docs.json:5,12."""
        source = "छत्तीसगढ़ शासन"
        output = (
            "For example, in the United States, a number of young people have been "
            "forced to leave their homes and move to another country to serve where "
            "there is a greater need for Kingdom preachers."
        )
        check = quality.check_translation(source, output)
        assert not check.ok
        assert any("length ratio" in r for r in check.reasons)
        assert any("contamination" in r for r in check.reasons)

    def test_rejects_contamination_even_at_plausible_length(self):
        source = "यह एक सरकारी परिपत्र है जिसमें बाल कल्याण संबंधी निर्देश दिए गए हैं।" * 3
        output = ("The article was published by the Watchtower Bible and Tract Society "
                  "of New York, Inc. and discusses child welfare directives at length.") * 2
        check = quality.check_translation(source, output)
        assert not check.ok
        assert any("contamination" in r for r in check.reasons)

    @pytest.mark.parametrize("phrase", [
        "Jehovah's name is holy",
        "visit jw.org for more",
        "the Watchtower explains",
        "our Bible students meet weekly",
        "Awake! reported that",
        "God's Kingdom will rule",
    ])
    def test_contamination_vocabulary(self, phrase):
        assert quality.find_contamination(phrase)

    def test_accepts_a_faithful_translation(self):
        source = ("छत्तीसगढ़ शासन महिला एवं बाल विकास विभाग द्वारा जारी परिपत्र। "
                  "सभी जिला कार्यक्रम अधिकारियों को निर्देशित किया जाता है कि "
                  "बाल संरक्षण योजना के अंतर्गत मासिक प्रतिवेदन प्रस्तुत करें।")
        output = ("Circular issued by the Department of Women and Child Development, "
                  "Government of Chhattisgarh. All District Programme Officers are "
                  "directed to submit monthly reports under the Child Protection Scheme.")
        check = quality.check_translation(source, output)
        assert check.ok, check.reasons

    def test_rejects_untranslated_devanagari_passthrough(self):
        source = "बाल कल्याण समिति की बैठक प्रत्येक माह आयोजित की जाएगी।"
        check = quality.check_translation(source, source)
        assert not check.ok
        assert any("Devanagari" in r for r in check.reasons)

    def test_rejects_empty_output(self):
        check = quality.check_translation("कुछ पाठ यहाँ है और यह पर्याप्त लंबा है।", "")
        assert not check.ok
        assert "empty translation" in check.reasons

    def test_rejects_degenerate_repetition(self):
        source = "जिला कार्यक्रम अधिकारी को निर्देशित किया जाता है। " * 12
        output = "\n".join(["The District Programme Officer is directed to comply."] * 20)
        check = quality.check_translation(source, output)
        assert not check.ok
        assert any("degenerate" in r for r in check.reasons)

    def test_rejects_truncated_output(self):
        source = "बाल संरक्षण एवं कल्याण से संबंधित विस्तृत दिशानिर्देश। " * 20
        check = quality.check_translation(source, "Guidelines.")
        assert not check.ok
        assert any("length ratio" in r for r in check.reasons)

    def test_script_ratios(self):
        deva, latin = quality.script_ratios("abcd")
        assert (deva, latin) == (0.0, 1.0)
        deva, latin = quality.script_ratios("शासन")
        assert deva == 1.0 and latin == 0.0


# ---------------------------------------------------------------------------
# Model commentary leaking into translation output
# ---------------------------------------------------------------------------
class TestMetaCommentary:
    def test_rejects_the_observed_llama_refusal(self):
        """Observed verbatim from llama3.2 on a whitespace-only chunk."""
        leaked = (
            "I can't provide a translation of that text as it appears to be a "
            'single space character (" ") and does not contain any meaningful '
            "information. Can you please provide the actual Hindi text for me to translate?"
        )
        assert quality.find_meta_commentary(leaked)
        assert quality.is_refusal(leaked) is True

    @pytest.mark.parametrize("text", [
        "I cannot translate this text.",
        "As an AI, I am unable to help with that.",
        "Please provide the actual Hindi text.",
        "Here is the English translation of the passage:",
        "It seems like you have not provided any text.",
        "I'm ready to translate. Please provide the Hindi text.",
        "Waiting for the Hindi text.",
        "Go ahead and paste the passage.",
    ])
    def test_refusal_variants(self, text):
        assert quality.find_meta_commentary(text)

    def test_strips_appended_commentary_keeping_the_translation(self):
        """Observed: a valid translation followed by a trailing remark.

        Rejecting the whole chunk would throw away good output, so commentary is
        removed line by line.
        """
        mixed = (
            "Prevention of malnutrition and improvement of nutrition status.\n"
            "Functions and duties of institutions under the Act.\n"
            "I'm ready to translate. Please provide the Hindi text."
        )
        cleaned, removed = quality.strip_meta_commentary(mixed)
        assert removed == 1
        assert "ready to translate" not in cleaned
        assert "Prevention of malnutrition" in cleaned
        assert "Functions and duties" in cleaned

    def test_strip_leaves_clean_translation_untouched(self):
        good = ("Circular issued by the Department of Women and Child Development.\n"
                "All officers shall submit monthly reports.")
        cleaned, removed = quality.strip_meta_commentary(good)
        assert removed == 0
        assert cleaned == good

    def test_real_translation_is_not_a_refusal(self):
        good = ("Circular issued by the Department of Women and Child Development, "
                "Government of Chhattisgarh, directing all District Programme Officers "
                "to submit monthly reports.")
        assert quality.find_meta_commentary(good) == []
        assert quality.is_refusal(good) is False

    def test_guard_rejects_translation_containing_commentary(self):
        source = "महिला एवं बाल विकास विभाग का गठन किया गया है। " * 6
        output = ("The Department of Women and Child Development has been established. "
                  "I can't provide a translation of the remaining text as it appears "
                  "to be a single space character.")
        check = quality.check_translation(source, output)
        assert not check.ok
        assert any("commentary" in r for r in check.reasons)


class TestTranslatableContent:
    @pytest.mark.parametrize("junk", ["", "   ", "\n\n", ".", " . ", "-"])
    def test_rejects_untranslatable_fragments(self, junk):
        assert quality.has_translatable_content(junk) is False

    def test_accepts_devanagari(self):
        assert quality.has_translatable_content("बाल कल्याण") is True

    def test_accepts_real_words(self):
        assert quality.has_translatable_content("Child Protection Unit") is True


# ---------------------------------------------------------------------------
# Finding #11 — chunk offsets must be exact, not re-derived
# ---------------------------------------------------------------------------
class TestChunkOffsets:
    def test_offsets_slice_back_to_the_chunk(self):
        text = "".join(f"वाक्य संख्या {i} यहाँ समाप्त होता है। " for i in range(200))
        for chunk, start, end in chunk_with_offsets(text, chunk_size=800, overlap=100):
            assert text[start:end].strip() == chunk

    def test_offsets_are_exact_for_hindi_text(self):
        """The old code guessed `text_hi[i*700:(i+1)*700]` from the English chunk
        index, so Hindi and English drifted apart. Offsets are recorded now."""
        text = "क" * 2500
        chunks = chunk_with_offsets(text, chunk_size=800, overlap=100)
        assert chunks[0][1] == 0
        assert chunks[1][1] == 700  # step = chunk_size - overlap
        assert chunks[2][1] == 1400
        assert chunks[-1][2] == len(text)

    def test_full_coverage_no_gaps(self):
        text = "अ" * 5000
        chunks = chunk_with_offsets(text, chunk_size=800, overlap=100)
        assert chunks[0][1] == 0
        assert chunks[-1][2] == len(text)
        for (_, _, prev_end), (_, next_start, _) in zip(chunks, chunks[1:]):
            assert next_start < prev_end, "chunks must overlap, never gap"

    def test_empty_and_whitespace(self):
        assert chunk_with_offsets("") == []
        assert chunk_with_offsets("   ") == []

    def test_no_infinite_loop_on_full_overlap(self):
        chunks = chunk_with_offsets("x" * 100, chunk_size=10, overlap=10)
        assert len(chunks) < 1000


# ---------------------------------------------------------------------------
# Sentence splitting (kept from the original, which handled the danda correctly)
# ---------------------------------------------------------------------------
class TestSplitForTranslation:
    def test_splits_on_danda(self):
        text = "पहला वाक्य यहाँ है। " * 60
        chunks = split_for_translation(text, max_chars=200)
        assert all(len(c) <= 200 for c in chunks)
        assert "".join(chunks).replace(" ", "") == text.replace(" ", "")

    def test_handles_text_without_separators(self):
        chunks = split_for_translation("क" * 500, max_chars=100)
        assert all(len(c) <= 100 for c in chunks)
        assert sum(len(c) for c in chunks) == 500

    def test_empty(self):
        assert split_for_translation("") == []
