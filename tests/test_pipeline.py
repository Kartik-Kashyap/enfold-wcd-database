"""Tests for the automated guards that the review said this pipeline needed.

Review finding #1 closes with: "This failure mode is silent, so it needs an
automated guard, not just care."  A guard nobody tests is just more code that
might be silently wrong, so the guard has tests -- including tests built from
the actual fabricated output that shipped in ``cg/translated_docs.json``.

Run:  python -m pytest -q
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import crawler, dates, jsonio, quality
from pipeline.chunking import chunk_with_offsets
from pipeline.filters import build_where_filter
from pipeline.states import STATES, StateConfig
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

    def test_odia_text_layer_skips_ocr(self):
        """Odia-script (Oriya) Odisha PDFs have a usable text layer too."""
        sample = "ଓଡ଼ିଶା ରାଜ୍ୟ ମହିଳା ଓ ଶିଶୁ ବିକାଶ ବିଭାଗ ଦ୍ୱାରା ଜାରି ହୋଇଥିବା ପରିପତ୍ର । " * 3
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
# Handler-served PDFs (UP's DownloadFile*.ashx?Id=...) vs. extension-based links
# ---------------------------------------------------------------------------
class TestResponseIsPdf:
    def test_pdf_content_type(self):
        assert crawler.response_is_pdf("application/pdf")
        assert crawler.response_is_pdf("Application/PDF; charset=binary")
        assert crawler.response_is_pdf("application/pdf; name=go_1031.pdf")

    def test_octet_stream_needs_magic_bytes(self):
        assert crawler.response_is_pdf("application/octet-stream", b"%PDF-1.7 \x00\x01")
        assert not crawler.response_is_pdf("application/octet-stream", b"<html><body>hi")
        assert not crawler.response_is_pdf("binary/octet-stream", b"\x89PNG\r\n\x1a\n")

    def test_non_pdf_responses_are_not_saved(self):
        assert not crawler.response_is_pdf("text/html; charset=utf-8")
        assert not crawler.response_is_pdf("image/png")
        assert not crawler.response_is_pdf("application/msword")
        assert not crawler.response_is_pdf("application/rtf")
        assert not crawler.response_is_pdf("")
        assert not crawler.response_is_pdf(None)


# ---------------------------------------------------------------------------
# Per-state OCR language config (Odisha opts into 'ori'; the rest stay fast)
# ---------------------------------------------------------------------------
class TestStateOcrLangs:
    def test_odisha_opts_into_odia(self):
        assert STATES["odisha"].ocr_langs == "hin+eng+ori"

    def test_default_states_stay_fast(self):
        for key in ("cg", "bihar", "up", "delhi"):
            assert STATES[key].ocr_langs == "hin+eng"

    def test_dataclass_default_matches_module_default(self):
        """A hand-built StateConfig without ocr_langs must fall back to hin+eng."""
        from pipeline import ocr
        assert StateConfig(
            key="x", name="X", start_url="https://x.in/",
            data_dirname="x", pdf_dirname="pdfs",
        ).ocr_langs == ocr.OCR_LANGS == "hin+eng"


# ---------------------------------------------------------------------------
# Reading a large text layer without holding the whole document
# ---------------------------------------------------------------------------
class _FakePage:
    def __init__(self, number, log):
        self.number = number
        self._log = log

    def extract_text(self):
        return f"page {self.number}"

    def flush_cache(self):
        self._log.append(("flush", self.number))


class _FakePages:
    def __init__(self, total, log):
        self._total = total
        self._log = log

    def __len__(self):
        return self._total

    def __getitem__(self, key):
        if isinstance(key, slice):
            return [_FakePage(i + 1, self._log)
                    for i in range(*key.indices(self._total))]
        return _FakePage(key + 1, self._log)


class _FakePdf:
    """Stands in for pdfplumber's PDF: sliceable .pages, context manager."""

    def __init__(self, total, log):
        self.pages = _FakePages(total, log)
        self.log = log

    def __enter__(self):
        self.log.append(("open", None))
        return self

    def __exit__(self, *exc):
        self.log.append(("close", None))
        return False


class TestBatchedTextLayer:
    """A 47 MB Delhi PDF was OOM-killed mid-run; batching is the fix.

    pdfplumber retains a PDFPage per page touched, so reading a document in one
    pass keeps all of it resident.  These tests pin the batching contract --
    reopen per chunk, page order preserved, every page released.
    """

    def test_reopens_once_per_chunk(self, monkeypatch):
        from pipeline import ocr

        log = []
        opens = []

        def fake_open(path):
            opens.append(path)
            return _FakePdf(50, log)

        monkeypatch.setattr(ocr.pdfplumber, "open", fake_open)
        text = ocr._read_text_layer(Path("big.pdf"), 50, chunk_size=24)

        assert len(opens) == 3, "50 pages at 24/chunk is 24 + 24 + 2"
        assert [k for k, _ in log if k == "close"] == ["close"] * 3
        # The point of reopening: no handle outlives its batch.
        assert len([k for k, _ in log if k == "open"]) == 3

    def test_page_order_survives_the_reopens(self, monkeypatch):
        from pipeline import ocr

        monkeypatch.setattr(ocr.pdfplumber, "open", lambda p: _FakePdf(50, []))
        text = ocr._read_text_layer(Path("big.pdf"), 50, chunk_size=24)

        assert text.split("\n\n") == [f"page {i}" for i in range(1, 51)]

    def test_every_page_is_flushed(self, monkeypatch):
        from pipeline import ocr

        log = []
        monkeypatch.setattr(ocr.pdfplumber, "open", lambda p: _FakePdf(50, log))
        ocr._read_text_layer(Path("big.pdf"), 50, chunk_size=24)

        assert [n for k, n in log if k == "flush"] == list(range(1, 51))

    def test_document_smaller_than_one_chunk_opens_once(self, monkeypatch):
        from pipeline import ocr

        opens = []
        monkeypatch.setattr(ocr.pdfplumber, "open",
                            lambda p: (opens.append(p), _FakePdf(3, []))[1])
        text = ocr._read_text_layer(Path("small.pdf"), 3, chunk_size=24)

        assert len(opens) == 1
        assert text.split("\n\n") == ["page 1", "page 2", "page 3"]

    def test_extract_text_routes_a_usable_layer_through_the_batched_read(self, monkeypatch):
        """The fast path must not fall back to holding the document open."""
        from pipeline import ocr

        seen = []
        monkeypatch.setattr(ocr.pdfplumber, "open", lambda p: _FakePdf(3, []))
        monkeypatch.setattr(ocr.quality, "needs_ocr", lambda s: False)
        monkeypatch.setattr(ocr, "_read_text_layer",
                            lambda path, total, **kw: (seen.append(total), "BATCHED")[1])

        text, used_ocr = ocr.extract_text(Path("doc.pdf"))
        assert (text, used_ocr) == ("BATCHED", False)
        assert seen == [3]


class TestPdftotextTextLayer:
    """Large PDFs skip pdfplumber's text-layer read entirely.

    A 46 MB Delhi booklet peaked at ~875 MB resident even with the batched
    pdfplumber read above, because the cost is pdfminer decoding embedded
    images per ``pdfplumber.open()``, not the number of pages held at once.
    Above ``TEXT_LAYER_PDFTOTEXT_MB`` the text layer comes from the
    ``pdftotext`` subprocess instead, which never loads image data into this
    process.
    """

    @staticmethod
    def _make_pdf(tmp_path, size_mb, name="doc.pdf"):
        p = tmp_path / name
        p.write_bytes(b"0" * int(size_mb * 1024 * 1024))
        return p

    def test_large_pdf_routes_to_pdftotext(self, tmp_path, monkeypatch):
        from pipeline import ocr

        pdf_path = self._make_pdf(tmp_path, 12)
        seen = []
        monkeypatch.setattr(ocr, "_pdftotext_page_count", lambda p: 5)
        monkeypatch.setattr(ocr, "_pdftotext_range", lambda p, f, l: ["sample text " * 30])
        monkeypatch.setattr(ocr.quality, "needs_ocr", lambda s: False)
        monkeypatch.setattr(ocr, "_read_text_layer_pdftotext",
                            lambda path, total, **kw: (seen.append(total), "PDFTOTEXT")[1])
        monkeypatch.setattr(ocr.pdfplumber, "open",
                            lambda p: (_ for _ in ()).throw(
                                AssertionError("pdfplumber must not open a large PDF")))

        text, used_ocr = ocr.extract_text(pdf_path)
        assert (text, used_ocr) == ("PDFTOTEXT", False)
        assert seen == [5]

    def test_small_pdf_keeps_pdfplumber(self, tmp_path, monkeypatch):
        from pipeline import ocr

        pdf_path = self._make_pdf(tmp_path, 1)
        calls = []
        monkeypatch.setattr(ocr, "_pdftotext_page_count", lambda p: calls.append("called") or 5)
        monkeypatch.setattr(ocr.pdfplumber, "open", lambda p: _FakePdf(3, []))
        monkeypatch.setattr(ocr.quality, "needs_ocr", lambda s: False)
        monkeypatch.setattr(ocr, "_read_text_layer", lambda path, total, **kw: "PDFPLUMBER")

        text, used_ocr = ocr.extract_text(pdf_path)
        assert (text, used_ocr) == ("PDFPLUMBER", False)
        assert calls == [], "pdftotext must not run below the threshold"

    def test_env_override_lowers_the_threshold(self, tmp_path, monkeypatch):
        from pipeline import ocr

        pdf_path = self._make_pdf(tmp_path, 1)  # below the default 10 MB
        monkeypatch.setenv("TEXT_LAYER_PDFTOTEXT_MB", "0.5")
        monkeypatch.setattr(ocr, "_pdftotext_page_count", lambda p: 2)
        monkeypatch.setattr(ocr, "_pdftotext_range", lambda p, f, l: ["text"])
        monkeypatch.setattr(ocr.quality, "needs_ocr", lambda s: False)
        monkeypatch.setattr(ocr, "_read_text_layer_pdftotext", lambda path, total, **kw: "PDFTOTEXT")
        monkeypatch.setattr(ocr.pdfplumber, "open",
                            lambda p: (_ for _ in ()).throw(
                                AssertionError("threshold override should skip pdfplumber")))

        text, used_ocr = ocr.extract_text(pdf_path)
        assert (text, used_ocr) == ("PDFTOTEXT", False)

    def test_missing_pdftotext_falls_back_to_pdfplumber(self, tmp_path, monkeypatch):
        """A missing/broken pdftotext must not lose the document."""
        from pipeline import ocr

        pdf_path = self._make_pdf(tmp_path, 12)

        def boom(p):
            raise FileNotFoundError("pdftotext not found")

        monkeypatch.setattr(ocr, "_pdftotext_page_count", boom)
        monkeypatch.setattr(ocr.pdfplumber, "open", lambda p: _FakePdf(3, []))
        monkeypatch.setattr(ocr.quality, "needs_ocr", lambda s: False)
        monkeypatch.setattr(ocr, "_read_text_layer", lambda path, total, **kw: "PDFPLUMBER")

        text, used_ocr = ocr.extract_text(pdf_path)
        assert (text, used_ocr) == ("PDFPLUMBER", False)

    def test_pdftotext_read_failure_falls_back_to_pdfplumber(self, tmp_path, monkeypatch):
        """pdftotext resolves the sample fine but the full read then fails."""
        from pipeline import ocr
        import subprocess

        pdf_path = self._make_pdf(tmp_path, 12)
        monkeypatch.setattr(ocr, "_pdftotext_page_count", lambda p: 5)
        monkeypatch.setattr(ocr, "_pdftotext_range", lambda p, f, l: ["sample text " * 30])
        monkeypatch.setattr(ocr.quality, "needs_ocr", lambda s: False)

        def boom(path, total, **kw):
            raise subprocess.CalledProcessError(1, "pdftotext")

        monkeypatch.setattr(ocr, "_read_text_layer_pdftotext", boom)
        monkeypatch.setattr(ocr.pdfplumber, "open", lambda p: _FakePdf(5, []))
        monkeypatch.setattr(ocr, "_read_text_layer", lambda path, total, **kw: "PDFPLUMBER")

        text, used_ocr = ocr.extract_text(pdf_path)
        assert (text, used_ocr) == ("PDFPLUMBER", False)

    def test_pdftotext_range_splits_on_form_feed(self):
        """Page-shape parity with pdfplumber: one string per page, no phantom
        trailing page from the form feed after the last one in the range."""
        from pipeline import ocr

        class FakeResult:
            stdout = "page one\x0cpage two\x0c".encode("utf-8")

        ocr_subprocess_run = ocr.subprocess.run
        try:
            ocr.subprocess.run = lambda *a, **kw: FakeResult()
            pages = ocr._pdftotext_range(Path("doc.pdf"), 1, 2)
        finally:
            ocr.subprocess.run = ocr_subprocess_run
        assert pages == ["page one", "page two"]

    def test_read_text_layer_pdftotext_chunks_like_the_pdfplumber_batching(self, monkeypatch):
        """Same page-range chunking contract as ``_read_text_layer``."""
        from pipeline import ocr

        calls = []

        def fake_range(path, first, last):
            calls.append((first, last))
            return [f"page {p}" for p in range(first, last + 1)]

        monkeypatch.setattr(ocr, "_pdftotext_range", fake_range)
        text = ocr._read_text_layer_pdftotext(Path("big.pdf"), 50, chunk_size=24)

        assert calls == [(1, 24), (25, 48), (49, 50)], "50 pages at 24/chunk is 24 + 24 + 2"
        assert text.split("\n\n") == [f"page {i}" for i in range(1, 51)]


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
# Document dates (pipeline/dates.py)
# ---------------------------------------------------------------------------
class TestDateExtraction:
    def test_issue_anchor_numeric(self):
        text = "क्रमांक 42 दिनांक 13/04/2026 को जारी किया गया परिपत्र।"
        assert dates.extract_date(text) == "2026-04-13"

    def test_di_abbreviation_with_dash(self):
        assert dates.extract_date("दि. 15-03-2024") == "2024-03-15"

    def test_anchor_with_colon_and_dots(self):
        assert dates.extract_date("दिनांक: 15.03.2024") == "2024-03-15"

    def test_hindi_digits(self):
        assert dates.extract_date("दिनांक १५/०३/२०२४") == "2024-03-15"

    def test_hindi_month_word(self):
        assert dates.extract_date("दिनांक 15 मार्च 2024") == "2024-03-15"

    def test_hindi_month_word_with_devanagari_year(self):
        assert dates.extract_date("दिनांक १५ मार्च २०२४") == "2024-03-15"

    def test_english_month_word(self):
        assert dates.extract_date("dated 12 March 2024") == "2024-03-12"

    def test_deadline_deprioritized(self):
        text = ("आवेदन की अंतिम तिथि 30/06/2026 है। "
                "यह परिपत्र दिनांक 15/03/2024 को जारी किया गया।")
        assert dates.extract_date(text) == "2024-03-15"

    def test_deadline_only_is_last_resort(self):
        assert dates.extract_date("आवेदन की अंतिम तिथि 30/06/2026 है।") == "2026-06-30"

    def test_issue_date_wins_over_earlier_bare_date(self):
        text = "संदर्भ 20/11/2025 के पत्र से। दिनांक 15/03/2024"
        assert dates.extract_date(text) == "2024-03-15"

    def test_iso_form(self):
        assert dates.extract_date("दिनांक 2024-03-15") == "2024-03-15"

    def test_no_date(self):
        assert dates.extract_date("कोई तिथि नहीं है इस दस्तावेज़ में।") is None

    def test_empty_input(self):
        assert dates.extract_date("") is None
        assert dates.extract_date(None) is None

    def test_garbage_is_not_a_date(self):
        assert dates.extract_date("abc/def/ghij कुछ भी") is None

    def test_invalid_calendar_date_skipped(self):
        assert dates.extract_date("दिनांक 31/02/2024") is None

    def test_invalid_month_skipped(self):
        assert dates.extract_date("दिनांक 12/13/2024") is None

    def test_out_of_range_year_skipped(self):
        assert dates.extract_date("दिनांक 15/03/1985") is None

    def test_date_epoch(self):
        # Day-count arithmetic, timezone-free: date objects have no .timestamp().
        assert dates.date_epoch("1970-01-02") == 86400
        assert dates.date_epoch("2024-03-15") > dates.date_epoch("2024-01-01")
        assert dates.date_epoch("2024-03-15") < dates.date_epoch("2024-12-31")

    def test_date_epoch_rejects_junk(self):
        assert dates.date_epoch("") is None
        assert dates.date_epoch(None) is None
        assert dates.date_epoch("not-a-date") is None
        assert dates.date_epoch("2024-13-40") is None

    def test_backfill_state_roundtrip(self, tmp_path):
        """Existing data gets dates without re-OCR; crawl metadata stays in sync."""
        state = StateConfig(
            key="test", name="Test", start_url="https://example.in/",
            data_dirname=str(tmp_path), pdf_dirname="pdfs",
        )
        jsonio.write_json_atomic(state.processed_docs, [
            {"id": "doc_1", "filename": "a_1.pdf", "text": "परिपत्र दिनांक 15/03/2024 को जारी।"},
            {"id": "doc_2", "filename": "b_2.pdf", "text": "कोई तिथि नहीं।"},
        ])
        jsonio.write_json_atomic(state.crawl_metadata, [
            {"filename": "a_1.pdf"}, {"filename": "b_2.pdf"},
        ])

        docs, dated = dates.backfill_state(state)
        assert (docs, dated) == (2, 1)

        docs = jsonio.read_json(state.processed_docs)
        assert docs[0]["document_date"] == "2024-03-15"
        assert "document_date" not in docs[1]

        meta = jsonio.read_json(state.crawl_metadata)
        assert meta[0]["document_date"] == "2024-03-15"
        assert "document_date" not in meta[1]


# ---------------------------------------------------------------------------
# The GUI date filter (found broken by an end-to-end run, not by these tests)
# ---------------------------------------------------------------------------
def _assert_chroma_legal(node, path="$"):
    """Assert every operator expression holds exactly ONE operator.

    This is the rule Chroma enforces at query time::

        ValueError: Expected operator expression to have exactly one operator

    An operator expression is a ``{field: {"$op": value}}`` mapping.  Walking
    the clause and checking it structurally catches the mistake without
    importing chromadb (and torch) into the suite.
    """
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if key == "$and":
            assert isinstance(value, list) and value, f"{path}.$and must be a non-empty list"
            for i, sub in enumerate(value):
                _assert_chroma_legal(sub, f"{path}.$and[{i}]")
        elif key.startswith("$"):
            raise AssertionError(f"{path}: operator {key} outside an expression")
        elif isinstance(value, dict):
            # An operator expression: {"field": {"$op": value}}.
            ops = [k for k in value if k.startswith("$")]
            assert len(ops) == 1, (
                f"{path}.{key} has {len(ops)} operators {ops}; Chroma allows exactly one. "
                "A range needs two conditions joined by $and."
            )
        # else: a plain equality condition ({"state": "Delhi"}) -- a scalar,
        # and legal as-is.  Only operator expressions carry the one-op rule.


class TestWhereFilter:
    def test_no_filters_is_none(self):
        assert build_where_filter() is None

    def test_single_condition_is_not_wrapped_in_and(self):
        where = build_where_filter(state="Delhi")
        assert where == {"state": "Delhi"}
        _assert_chroma_legal(where)

    def test_date_range_is_two_conditions_joined_by_and(self):
        """The regression: one dict with $gte AND $lte is rejected by Chroma."""
        where = build_where_filter(date_from="2025-01-01", date_to="2026-12-31")
        _assert_chroma_legal(where)

        lo = dates.date_epoch("2025-01-01")
        hi = dates.date_epoch("2026-12-31")
        assert where == {"$and": [
            {"document_date_epoch": {"$gte": lo}},
            {"document_date_epoch": {"$lte": hi}},
        ]}
        # The shape that actually broke it, spelled out.
        assert where != {"document_date_epoch": {"$gte": lo, "$lte": hi}}

    def test_date_range_with_state_and_category(self):
        where = build_where_filter(state="Delhi", category="Acts",
                                   date_from="2025-01-01", date_to="2026-12-31")
        _assert_chroma_legal(where)
        assert len(where["$and"]) == 4
        assert {"state": "Delhi"} in where["$and"]
        assert {"category": "Acts"} in where["$and"]

    def test_open_ended_range_is_a_single_operator(self):
        where = build_where_filter(date_from="2025-01-01")
        assert where == {"document_date_epoch": {"$gte": dates.date_epoch("2025-01-01")}}
        _assert_chroma_legal(where)

    def test_unparseable_dates_are_dropped_not_emitted_as_none(self):
        """Chroma rejects None inside a where clause."""
        assert build_where_filter(date_from="not-a-date") is None
        assert build_where_filter(date_from="", date_to=None) is None

        where = build_where_filter(state="Delhi", date_from="not-a-date")
        assert where == {"state": "Delhi"}

    def test_epochs_match_the_indexed_values(self):
        """The app's range must use the same arithmetic index.py stored with."""
        where = build_where_filter(date_from="2024-06-01")
        assert (where["document_date_epoch"]["$gte"]
                == dates.date_epoch("2024-06-01")
                == (date(2024, 6, 1) - date(1970, 1, 1)).days * 86400)


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
