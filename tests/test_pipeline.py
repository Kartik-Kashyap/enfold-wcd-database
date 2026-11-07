"""Tests for the automated guards that the review said this pipeline needed.

Review finding #1 closes with: "This failure mode is silent, so it needs an
automated guard, not just care."  A guard nobody tests is just more code that
might be silently wrong, so the guard has tests -- including tests built from
the actual fabricated output that shipped in ``cg/translated_docs.json``.

Run:  python -m pytest -q
"""

from __future__ import annotations

import base64
import hashlib
import re
import ssl
import sys
from datetime import date
from pathlib import Path

import pytest
import requests.certs
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import crawler, dates, jsonio, paths, quality
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

    def test_pdftotext_timeout_while_sampling_falls_back(self, tmp_path, monkeypatch):
        """A hung pdftotext must fall back, not take the whole run down.

        ``TimeoutExpired`` is a sibling of ``CalledProcessError`` under
        ``SubprocessError`` -- it is not a subclass of it, nor of ``OSError``
        -- so an ``except`` clause listing those two misses it entirely and
        the timeout escapes ``extract_text``.  On a memory-starved box that
        is the difference between one slow document and a dead state run.
        """
        from pipeline import ocr
        import subprocess

        pdf_path = self._make_pdf(tmp_path, 12)

        def hang(p):
            raise subprocess.TimeoutExpired(cmd="pdfinfo", timeout=120)

        monkeypatch.setattr(ocr, "_pdftotext_page_count", hang)
        monkeypatch.setattr(ocr.pdfplumber, "open", lambda p: _FakePdf(3, []))
        monkeypatch.setattr(ocr.quality, "needs_ocr", lambda s: False)
        monkeypatch.setattr(ocr, "_read_text_layer", lambda path, total, **kw: "PDFPLUMBER")

        text, used_ocr = ocr.extract_text(pdf_path)
        assert (text, used_ocr) == ("PDFPLUMBER", False)

    def test_pdftotext_timeout_while_reading_falls_back(self, tmp_path, monkeypatch):
        """Sampling succeeds, then the full read exceeds the timeout."""
        from pipeline import ocr
        import subprocess

        pdf_path = self._make_pdf(tmp_path, 12)
        monkeypatch.setattr(ocr, "_pdftotext_page_count", lambda p: 5)
        monkeypatch.setattr(ocr, "_pdftotext_range", lambda p, f, l: ["sample text " * 30])
        monkeypatch.setattr(ocr.quality, "needs_ocr", lambda s: False)

        def hang(path, total, **kw):
            raise subprocess.TimeoutExpired(cmd="pdftotext", timeout=120)

        monkeypatch.setattr(ocr, "_read_text_layer_pdftotext", hang)
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


# ---------------------------------------------------------------------------
# Karnataka listing-table metadata (wcd.karnataka.gov.in)
#
# Trimmed from the real portal: a header row labelling its columns in Kannada,
# and document rows whose download links read only "Download" -- so the subject,
# order number, issue date, originating body and language exist nowhere but the
# table row itself.
# ---------------------------------------------------------------------------
KARNATAKA_TABLE_HTML = """
<table class="table-striped table-bordered table">
 <tbody>
  <tr>
   <td><strong>ವಿಷಯ</strong></td>
   <td><strong>ಆದೇಶ ಸಂಖ್ಯೆ / ದಿನಾಂಕ</strong></td>
   <td><strong>ಮೂಲ</strong></td>
   <td><strong>ಮಾಧ್ಯಮ</strong></td>
   <td><strong>ಗಾತ್ರ</strong></td>
   <td><strong>ಡೌನ್‌ಲೋಡ್</strong></td>
  </tr>
  <tr>
   <td><p>ಸುಧಾರಣಾ ಸಂಸ್ಥೆಗಳ ಮಹಿಳಾ ನಿವಾಸಿಗಳ ವಿವಾಹ ಕಾರ್ಯಕ್ರಮದಡಿ ನೀಡಲಾಗುತ್ತಿರುವ ಆರ್ಥಿಕ ನೆರವನ್ನು ಹೆಚ್ಚಿಸುವ ಕುರಿತು.</p></td>
   <td><p>ಮಮಇ 293 ಮಮಅ 2005, 05.05.2007</p></td>
   <td>ಇಲಾಖೆ</td>
   <td>ಕನ್ನಡ</td>
   <td>2KB</td>
   <td><a href="https://wcd.karnataka.gov.in/uploads/media_to_upload1753699634.pdf" target="_blank">Download</a></td>
  </tr>
  <tr>
   <td><p>ಮಹಿಳಾ ಮತ್ತು ಮಕ್ಕಳ ಅಭಿವೃದ್ಧಿ ಇಲಾಖೆ ಯೋಜನೆಗಳನ್ನು ಮುಂದುವರೆಸುವ ಬಗ್ಗೆ.</p></td>
   <td><p>ಮಮಇ 41 ಸ್ವೀಮರ 2020,</p><p>22.06.2020</p></td>
   <td>&nbsp;ಇಲಾಖೆ</td>
   <td>&nbsp;ಕನ್ನಡ&nbsp;</td>
   <td>&nbsp;2KB</td>
   <td><a href="https://wcd.karnataka.gov.in/uploads/media_to_upload1759478376.pdf" target="_blank">DOWNLOAD</a></td>
  </tr>
  <tr>
   <td>ಅಂಗನವಾಡಿ ಕಾರ್ಯಕರ್ತೆಯರ ಆಯ್ಕೆ ಮಾರ್ಗಸೂಚಿ ಹಾಗೂ ಅರ್ಹತೆಯ ಮಾನದಂಡ</td>
   <td>ಮಮಇ 237 ಐಸಿಡಿ 2012, 15.06.2012</td>
   <td>ಇಲಾಖೆ</td>
   <td>ಕನ್ನಡ</td>
   <td>2KB</td>
   <td><a href="https://wcd.karnataka.gov.in/uploads/media_to_upload1753350170.pdf" target="_blank">Download</a></td>
  </tr>
 </tbody>
</table>
"""


def _karnataka_rows():
    soup = BeautifulSoup(KARNATAKA_TABLE_HTML, "html.parser")
    return soup.find_all("tr")[1:]  # [0] is the header row


class TestKarnatakaListingTable:
    def test_headers_map_to_canonical_fields(self):
        table = BeautifulSoup(KARNATAKA_TABLE_HTML, "html.parser").find("table")
        assert crawler.listing_headers(table) == [
            "subject", "order", "source_org", "medium", "size_reported", None,
        ]

    def test_row_captures_the_portal_published_fields(self):
        meta = crawler.parse_listing_row(_karnataka_rows()[0].find("a"))
        assert meta["order_no"] == "ಮಮಇ 293 ಮಮಅ 2005"
        assert meta["source_org"] == "ಇಲಾಖೆ"
        assert meta["medium"] == "ಕನ್ನಡ"
        assert meta["size_reported"] == "2KB"
        assert meta["subject"].startswith("ಸುಧಾರಣಾ ಸಂಸ್ಥೆಗಳ")

    def test_issue_date_comes_from_the_order_cell(self):
        meta = crawler.parse_listing_row(_karnataka_rows()[0].find("a"))
        assert meta["table_date"] == "2007-05-05"

    def test_date_split_across_paragraphs_still_parses(self):
        """Row 2 breaks the order cell over two <p> tags; row 3 is inline."""
        second = crawler.parse_listing_row(_karnataka_rows()[1].find("a"))
        assert second["table_date"] == "2020-06-22"
        assert second["order_no"] == "ಮಮಇ 41 ಸ್ವೀಮರ 2020"
        third = crawler.parse_listing_row(_karnataka_rows()[2].find("a"))
        assert third["table_date"] == "2012-06-15"

    def test_non_breaking_spaces_are_cleaned(self):
        meta = crawler.parse_listing_row(_karnataka_rows()[1].find("a"))
        assert meta["source_org"] == "ಇಲಾಖೆ"
        assert meta["medium"] == "ಕನ್ನಡ"

    def test_inline_date_label_is_stripped_from_the_order_number(self):
        """Live rows read "ಮಮಇ 81 ಮಮಅ 2025, ದಿನಾಂಕ:04.04.2025" (wcd /75/2025/kn)."""
        soup = BeautifulSoup(
            '<table>'
            '<tr><td><strong>ವಿಷಯ</strong></td>'
            '<td><strong>ಆದೇಶ ಸಂಖ್ಯೆ / ದಿನಾಂಕ</strong></td></tr>'
            '<tr><td>ಗೃಹಲಕ್ಷ್ಮಿ ಯೋಜನೆಯನ್ನು ಮುಂದುವರೆಸುವ ಬಗ್ಗೆ.</td>'
            '<td>ಮಮಇ 81 ಮಮಅ 2025, ದಿನಾಂಕ:04.04.2025</td>'
            '<td><a href="/uploads/media_to_upload1753436610.pdf">Download</a></td></tr>'
            '</table>',
            "html.parser",
        )
        meta = crawler.parse_listing_row(soup.find("a"))
        assert meta["table_date"] == "2025-04-04"
        assert meta["order_no"] == "ಮಮಇ 81 ಮಮಅ 2025"

    def test_parenthesised_order_number_survives(self):
        """Live row "ಮಮಇ 70 ಮಮಅ 2023 (ಭಾ-20), 23.02.2024" (wcd /76/2024/kn)."""
        soup = BeautifulSoup(
            '<table>'
            '<tr><td><strong>ವಿಷಯ</strong></td>'
            '<td><strong>ಆದೇಶ ಸಂಖ್ಯೆ / ದಿನಾಂಕ</strong></td></tr>'
            '<tr><td>ಗೃಹಲಕ್ಷ್ಮಿ ಯೋಜನೆಯ ಸೌಲಭ್ಯದ ಕುರಿತು.</td>'
            '<td>ಮಮಇ 70 ಮಮಅ 2023 (ಭಾ-20), 23.02.2024</td>'
            '<td><a href="/uploads/media_to_upload1753425960.pdf">Download</a></td></tr>'
            '</table>',
            "html.parser",
        )
        meta = crawler.parse_listing_row(soup.find("a"))
        assert meta["table_date"] == "2024-02-23"
        assert meta["order_no"] == "ಮಮಇ 70 ಮಮಅ 2023 (ಭಾ-20)"

    def test_a_link_wrapped_in_a_paragraph_inside_the_cell_still_matches(self):
        """Live rows wrap the anchor as a <td><p><a>; find_parent must cope."""
        soup = BeautifulSoup(
            '<table>'
            '<tr><td><strong>ವಿಷಯ</strong></td>'
            '<td><strong>ಆದೇಶ ಸಂಖ್ಯೆ / ದಿನಾಂಕ</strong></td></tr>'
            '<tr><td>ವಿಷಯ ಪಠ್ಯ</td><td>ಮಮಇ 1 ಮಮಅ 2024, 01.02.2024</td>'
            '<td><p><a href="/uploads/media_to_upload1.pdf">Download</a></p></td></tr>'
            '</table>',
            "html.parser",
        )
        meta = crawler.parse_listing_row(soup.find("a"))
        assert meta["table_date"] == "2024-02-01"

    def test_a_link_outside_a_table_captures_nothing(self):
        soup = BeautifulSoup('<p><a href="/a.pdf">Download</a></p>', "html.parser")
        assert crawler.parse_listing_row(soup.find("a")) == {}

    def test_a_table_without_a_recognisable_header_captures_nothing(self):
        """No Kannada header means no fields -- never a positional guess."""
        soup = BeautifulSoup(
            '<table><tr><td>a</td><td>b</td></tr>'
            '<tr><td>x</td><td><a href="/a.pdf">Download</a></td></tr></table>',
            "html.parser",
        )
        assert crawler.parse_listing_row(soup.find("a")) == {}

    def test_a_spanning_caption_row_is_not_a_header(self):
        """The real table at wcd /84/gradation-lists.../kn (found on a live crawl).

        It opens with one caption cell spanning all five columns, reading
        "...ವಿವಿಧ ವೃಂದಗಳ ದಿನಾಂಕ:01.01.2025ರಲ್ಲಿದ್ದಂತೆ ಜ್ಯೇಷ್ಠತಾ ಪಟ್ಟಿಗಳು." -- which
        contains the ದಿನಾಂಕ marker.  Accepting that caption as the header maps
        column 0 to "order", so every row's *serial number* (1, 2, 3...) was
        recorded as its government order number, with no subject and no date.
        """
        soup = BeautifulSoup(
            '<table>'
            '<tr><td colspan="5"><strong>ಮಹಿಳಾ ಮತ್ತು ಮಕ್ಕಳ ಅಭಿವೃದ್ದಿ ಹಾಗೂ '
            'ವಿಕಲಚೇತನರ ಮತ್ತು ಹಿರಿಯ ನಾಗರಿಕರ ಸಬಲೀಕರಣ ಇಲಾಖೆಯ ವಿವಿಧ ವೃಂದಗಳ '
            'ದಿನಾಂಕ:01.01.2025ರಲ್ಲಿದ್ದಂತೆ ಜ್ಯೇಷ್ಠತಾ ಪಟ್ಟಿಗಳು.</strong></td></tr>'
            '<tr><td>Sl No</td><td>Cadre</td><td>Government Notification</td>'
            '<td>Provisional list Date</td><td>Final list Date</td></tr>'
            '<tr><td>1</td><td>ಜಂಟಿ ನಿರ್ದೇಶಕರು</td>'
            '<td>ಮಮಇ 54 ಎಸ್‌ಜೆಡಿ 2025</td>'
            '<td><a href="/uploads/media_to_upload1763188850.pdf">04.03.2025</a></td>'
            '<td>17.09.2025</td></tr>'
            '</table>',
            "html.parser",
        )
        assert crawler.listing_headers(soup.find("table")) == []
        assert crawler.parse_listing_row(soup.find("a")) == {}

    def test_a_header_wider_than_the_data_row_is_rejected(self):
        """A positional mapping only holds when both rows have the same shape."""
        soup = BeautifulSoup(
            '<table>'
            '<tr><th>ವಿಷಯ</th><th>ದಿನಾಂಕ</th><th>ಮೂಲ</th></tr>'
            '<tr><td>ಒಂದು ದಾಖಲೆ</td><td><a href="/a.pdf">Download</a></td></tr>'
            '</table>',
            "html.parser",
        )
        assert crawler.parse_listing_row(soup.find("a")) == {}

    def test_the_real_table_still_maps_after_the_caption_guard(self):
        """The guard must not cost us the tables that were parsing correctly."""
        table = BeautifulSoup(KARNATAKA_TABLE_HTML, "html.parser").find("table")
        row = _karnataka_rows()[0]
        assert crawler.listing_headers(table, row) == [
            "subject", "order", "source_org", "medium", "size_reported", None,
        ]
        assert crawler.parse_listing_row(row.find("a"))["table_date"] == "2007-05-05"

    def test_subject_drives_category_inference(self):
        """The anchor text is only "Download"; the subject carries the signal."""
        meta = crawler.parse_listing_row(_karnataka_rows()[1].find("a"))
        assert crawler.determine_category("", "", meta["subject"]) == "Schemes & Programs"
        assert crawler.determine_category("", "", "Download") == "General / Uncategorized"

    def test_kannada_keywords_are_recognised(self):
        assert crawler.determine_category("", "", "ಅಧಿನಿಯಮ") == "Acts & Rules"
        assert crawler.determine_category("", "", "ಸುತ್ತೋಲೆ") == "Circulars & Orders"
        assert crawler.determine_category("", "", "ಮಾರ್ಗಸೂಚಿ") == "SOPs & Guidelines"
        assert crawler.determine_category("", "", "ವಾರ್ಷಿಕ ವರದಿ") == "Reports"


class TestKarnatakaStateConfig:
    def test_registered_with_kannada_and_table_capture(self):
        ks = STATES["karnataka"]
        assert ks.name == "Karnataka"
        assert ks.start_url == "https://wcd.karnataka.gov.in/"
        assert ks.ocr_langs == "kan+eng"
        assert ks.language_code == "kn"
        assert ks.capture_table_metadata is True
        assert ks.filename_from_url is True

    def test_existing_states_are_untouched_by_the_new_fields(self):
        """The five shipped states keep their OCR langs, their default
        (link-text) naming, and no table capture."""
        for key in ("cg", "bihar", "up", "delhi"):
            s = STATES[key]
            assert s.ocr_langs == "hin+eng"
            assert s.language_code == "hi"
            assert s.capture_table_metadata is False
            assert s.filename_from_url is False

    def test_new_fields_default_off(self):
        bare = StateConfig(key="x", name="X", start_url="https://x.in/",
                           data_dirname="x", pdf_dirname="pdfs")
        assert bare.language_code == "hi"
        assert bare.capture_table_metadata is False
        assert bare.filename_from_url is False


class TestFilenameFromUrl:
    def test_names_from_the_url_basename(self):
        """Karnataka's links all read "Download", so the URL is the only signal."""
        assert crawler.filename_from_url(
            "https://wcd.karnataka.gov.in/uploads/media_to_upload1753699634.pdf", 1
        ) == "media_to_upload1753699634.pdf"

    def test_query_string_and_fragment_are_dropped(self):
        assert crawler.filename_from_url(
            "https://x.in/uploads/doc42.pdf?v=2#page=1", 1) == "doc42.pdf"

    def test_unsafe_characters_are_replaced(self):
        assert crawler.filename_from_url("https://x.in/u/a%20b.pdf", 1) == "a_20b.pdf"

    def test_extension_is_added_when_the_path_has_none(self):
        assert crawler.filename_from_url(
            "https://x.in/u/DownloadFile.ashx", 1) == "DownloadFile.ashx.pdf"

    def test_falls_back_when_the_url_has_no_basename(self):
        assert crawler.filename_from_url("https://x.in/", 7) == "document_7.pdf"

    def test_a_name_already_taken_is_disambiguated(self):
        taken = {"doc42.pdf"}
        assert crawler.filename_from_url("https://x.in/u/doc42.pdf", 9, taken) == "doc42_9.pdf"

    def test_a_percent_encoded_basename_still_yields_a_usable_name(self):
        name = crawler.filename_from_url("https://x.in/u/%E0%B2%A6%E0%B2%B8.pdf", 1)
        assert name.endswith(".pdf") and name != "document_1.pdf"


class TestTableDatePrecedence:
    """A portal-published date outranks anything read out of the document."""

    def _state_and_stub(self, tmp_path, monkeypatch, crawl_entry, text):
        from pipeline import ocr, paths

        monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)
        state = StateConfig(key="k", name="K", start_url="https://k.in/",
                            data_dirname="k", pdf_dirname="pdfs")
        state.pdf_dir.mkdir(parents=True)
        (state.pdf_dir / "doc.pdf").write_bytes(b"%PDF-1.4\n")
        jsonio.write_json_atomic(state.crawl_metadata, [crawl_entry])
        monkeypatch.setattr(ocr, "configure_tesseract", lambda: "tesseract")
        monkeypatch.setattr(ocr, "check_hindi_langpack", lambda: True)
        monkeypatch.setattr(ocr, "missing_langpacks", lambda langs: [])
        monkeypatch.setattr(ocr, "extract_text", lambda p, lang=None: (text, False))
        return ocr, state

    def test_table_date_beats_ocr_text_and_the_pdf_read(self, tmp_path, monkeypatch):
        ocr, state = self._state_and_stub(
            tmp_path, monkeypatch,
            {"filename": "doc.pdf", "table_date": "2007-05-05",
             "document_date": "1999-01-01", "subject": "s"},
            "दिनांक 15/03/2024 को जारी",
        )
        ocr.process_state(state)
        docs = jsonio.read_json(state.processed_docs)
        assert docs[0]["document_date"] == "2007-05-05"

    def test_without_a_table_date_ocr_text_still_wins(self, tmp_path, monkeypatch):
        """The five existing states have no table_date, so nothing changes."""
        ocr, state = self._state_and_stub(
            tmp_path, monkeypatch,
            {"filename": "doc.pdf", "document_date": "1999-01-01"},
            "दिनांक 15/03/2024 को जारी",
        )
        ocr.process_state(state)
        docs = jsonio.read_json(state.processed_docs)
        assert docs[0]["document_date"] == "2024-03-15"

    def test_captured_fields_reach_processed_docs(self, tmp_path, monkeypatch):
        ocr, state = self._state_and_stub(
            tmp_path, monkeypatch,
            {"filename": "doc.pdf", "subject": "ವಿಷಯ", "order_no": "ಮಮಇ 293",
             "source_org": "ಇಲಾಖೆ", "medium": "ಕನ್ನಡ"},
            "some text",
        )
        ocr.process_state(state)
        doc = jsonio.read_json(state.processed_docs)[0]
        assert doc["subject"] == "ವಿಷಯ"
        assert doc["order_no"] == "ಮಮಇ 293"
        assert doc["source_org"] == "ಇಲಾಖೆ"
        assert doc["medium"] == "ಕನ್ನಡ"

    def test_states_without_the_fields_do_not_gain_empty_keys(self, tmp_path, monkeypatch):
        ocr, state = self._state_and_stub(
            tmp_path, monkeypatch, {"filename": "doc.pdf"}, "some text")
        ocr.process_state(state)
        doc = jsonio.read_json(state.processed_docs)[0]
        assert "subject" not in doc and "order_no" not in doc


class TestMissingLangpacks:
    def test_reports_a_pack_that_is_not_installed(self, monkeypatch):
        from pipeline import ocr
        monkeypatch.setattr(ocr.pytesseract, "get_languages",
                            lambda config="": ["eng", "hin", "ori", "osd"])
        assert ocr.missing_langpacks("kan+eng") == ["kan"]
        assert ocr.missing_langpacks("hin+eng") == []

    def test_no_warning_when_languages_cannot_be_listed(self, monkeypatch):
        from pipeline import ocr

        def boom(config=""):
            raise RuntimeError("no tesseract")

        monkeypatch.setattr(ocr.pytesseract, "get_languages", boom)
        assert ocr.missing_langpacks("kan+eng") == []


# ---------------------------------------------------------------------------
# Karnataka — the portal serves an incomplete TLS chain
# ---------------------------------------------------------------------------
class TestKarnatakaTlsChain:
    """wcd.karnataka.gov.in sends a chain that no client can verify.

    The leaf was renewed onto GlobalSign's R46 intermediate, but the handshake
    still carries the *previous* intermediate.  `openssl s_client` reports
    "Verify return code: 21 (unable to verify the first certificate)",
    `requests` raises CERTIFICATE_VERIFY_FAILED and curl exits 60 — on the
    laptop and on the Oracle box alike, so it is the server's fault, not ours.

    The fix appends the intermediate the server forgot (its own AIA extension
    points straight at it) to the standard trust store.  These tests pin both
    halves of that: the certificate is the right one, and nothing about
    verification was weakened to make the crawl work.
    """

    CERT = "globalsign-gcc-r46-ov-tls-ca-2025.pem"

    def test_karnataka_names_the_vendored_intermediate(self):
        assert STATES["karnataka"].extra_ca == self.CERT

    def test_the_vendored_file_is_in_the_repo(self):
        cert = paths.CERTS_DIR / self.CERT
        assert cert.is_file(), f"{cert} is missing -- the Karnataka crawl cannot verify"

    def test_no_other_state_needs_an_extra_certificate(self):
        """The five original states verified fine; only Karnataka is patched."""
        others = [s for s in STATES.values() if s.key != "karnataka"]
        assert others, "expected the original states to still be registered"
        assert all(s.extra_ca is None for s in others)

    def test_vendored_certificate_is_the_one_the_server_points_at(self):
        """Fingerprint, issuer and expiry, so a stale file cannot sit here silently.

        The value below is what `openssl x509 -fingerprint -sha256` printed for
        the certificate fetched from the portal's own AIA URI.
        """
        cert = paths.CERTS_DIR / self.CERT
        der = base64.b64decode(
            "".join(cert.read_text(encoding="utf-8")
                    .split("-----BEGIN CERTIFICATE-----")[1]
                    .split("-----END CERTIFICATE-----")[0].split()))
        assert len(der) == 1408
        fingerprint = ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())
        assert fingerprint == (
            "D1:60:E2:DE:4E:56:CB:10:B6:6C:1C:B0:AD:CB:79:CF:"
            "93:C7:8D:CD:9D:B3:0C:20:18:22:02:62:C0:40:63:F9")

        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cafile=str(cert))
        ours = [c for c in ctx.get_ca_certs()
                if "GCC R46 OV TLS CA 2025" in str(c["subject"])]
        assert ours, "the vendored file does not contain the expected intermediate"
        # Its issuer is a root certifi already trusts, which is exactly why
        # appending it restores verification instead of bypassing it.
        assert "GlobalSign Root R46" in str(ours[0]["issuer"])

    def test_states_without_an_extra_ca_use_the_default_store(self):
        for state in STATES.values():
            if state.extra_ca is None:
                assert crawler.verify_for(state) is True

    def test_missing_file_falls_back_to_the_default_store(self, monkeypatch, tmp_path):
        """A deleted certificate must degrade to the normal store, not to no checks."""
        monkeypatch.setattr(paths, "CERTS_DIR", tmp_path / "empty")
        monkeypatch.setattr(paths, "CA_BUNDLE_CACHE", tmp_path / "ca")
        assert crawler.verify_for(STATES["karnataka"]) is True

    def test_combined_bundle_keeps_every_root_and_adds_exactly_one(self,
                                                                  monkeypatch, tmp_path):
        """The safety property: a bundle that dropped roots would be a downgrade."""
        monkeypatch.setattr(paths, "CA_BUNDLE_CACHE", tmp_path / "ca")
        bundle = Path(crawler.verify_for(STATES["karnataka"]))
        assert bundle.is_file()

        base = ssl.create_default_context(cafile=requests.certs.where())
        combined = ssl.create_default_context(cafile=str(bundle))
        names = [str(c["subject"]) for c in combined.get_ca_certs()]
        assert any("GCC R46 OV TLS CA 2025" in n for n in names)
        # Same roots as the standard store, plus the one intermediate.
        assert len(combined.get_ca_certs()) == len(base.get_ca_certs()) + 1

    def test_bundle_is_standard_roots_plus_the_extra_certificate(self,
                                                                 monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "CA_BUNDLE_CACHE", tmp_path / "ca")
        bundle = Path(crawler.verify_for(STATES["karnataka"]))
        payload = bundle.read_bytes()
        assert payload.startswith(Path(requests.certs.where()).read_bytes().rstrip())
        assert payload.rstrip().endswith(b"-----END CERTIFICATE-----")
        assert (paths.CERTS_DIR / self.CERT).read_bytes().strip() in payload

    def test_bundle_is_reused_rather_than_rebuilt(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "CA_BUNDLE_CACHE", tmp_path / "ca")
        first = Path(crawler.verify_for(STATES["karnataka"]))
        stamp = first.stat().st_mtime_ns
        assert Path(crawler.verify_for(STATES["karnataka"])) == first
        assert first.stat().st_mtime_ns == stamp

    def test_crawler_never_disables_verification(self):
        """`verify=False` anywhere in a request would make the corpus forgeable.

        The vendored-certificate route exists precisely so nobody is tempted to
        reach for it; this guards the invariant against a future edit.
        """
        source = re.sub(r"\s+", " ", Path(crawler.__file__).read_text(encoding="utf-8"))
        calls = re.findall(r"requests\.get\((.{0,160}?)\)", source)
        assert len(calls) == 3, "expected the three download call sites"
        for call in calls:
            assert "verify=verify" in call, f"a request lost its verify argument: {call}"
            assert "verify=False" not in call
