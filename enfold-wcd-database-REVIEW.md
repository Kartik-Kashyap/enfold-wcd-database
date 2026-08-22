# Code Review — Child Rights & Policy Document Pipeline

Hey Kartik,

Nice first commit — the pipeline shape is right, and a few of the engineering habits in here (resume
support, memory management in the OCR loop, keeping the Hindi original alongside every translation)
are things a lot of people don't get right on their first pass. That's why the one critical bug
below is worth taking seriously rather than personally: it's hiding *behind* code that otherwise
shows good instincts, which is exactly how these bugs survive.

This review goes through `bihar/scrap.py`, `cg/crawler.py`, `cg/ocr.py`, `cg/translate_docs.py`,
`cg/index_docs.py`, and `cg/app.py`, plus the committed data files. Every finding below was checked
directly against the code and data in this commit, with file and line references so you can jump
straight to them.

## Severity legend

- 🔴 **Critical** — data currently in the pipeline is wrong in a way that would mislead a user. Don't ship search results from this until it's fixed.
- 🟠 **High** — works, but defeats its own purpose (a "fast path" that never runs, etc.) or will hard-block scaling.
- 🟡 **Medium** — portability, reproducibility, or hygiene issues that will cost the next person (including future you) real time.
- 🔵 **Low** — correct-but-worth-knowing details, small bugs, minor inefficiencies.
- ✅ **Good** — deliberate, worth keeping, worth repeating in the next pipeline you write.

## Summary

| # | Severity | Finding | Where |
|---|---|---|---|
| 1 | 🔴 Critical | Translations are silently fabricated on hard input, and the entire search index is built from them | `cg/translate_docs.py`, `cg/index_docs.py` |
| 2 | 🟠 High | The Kruti Dev detector misfires on nearly all text, so every PDF takes the slow OCR path | `cg/ocr.py` |
| 3 | 🟡 Medium | Hardcoded Windows Tesseract path; stored paths use backslashes | `cg/ocr.py` |
| 4 | 🟡 Medium | Download buttons are dead on a fresh clone | `cg/app.py` |
| 5 | 🟡 Medium | 24 MB repo for ~31 KB of code; `.gitignore` doesn't cover any of it | `cg/chroma_db/`, `cg/*.json` |
| 6 | 🟡 Medium | No README, no `requirements.txt` | repo root |
| 7 | 🟡 Medium | `bihar/scrap.py` is a 4-line-diff copy of `cg/crawler.py` | `bihar/scrap.py` |
| 8 | 🟡 Medium | Crawler has no request delay and no `robots.txt` check | `cg/crawler.py` |
| 9 | 🔵 Low | Only 4 of 45 crawled PDFs have been processed | `cg/processed_docs.json` |
| 10 | 🔵 Low | The app's state filter lists states it has no data for | `cg/app.py` |
| 11 | 🔵 Low | Hindi/English excerpt offsets drift apart after translation | `cg/index_docs.py` |
| 12 | 🔵 Low | Full JSON output is rewritten after every single document (O(n²)) | `cg/ocr.py`, `cg/translate_docs.py` |
| 13 | 🔵 Low | `Content-Type` is checked before `raise_for_status()` | `cg/crawler.py` |

---

## 🔴 CRITICAL — English translations are fabricated, and the search index is built on them

**What's wrong.** `cg/translate_docs.py:67` loads `Helsinki-NLP/opus-mt-hi-en` (a small MarianMT
model) to translate every document's Hindi text to English. On hard input — dense bureaucratic
Hindi, OCR noise, repetition — it doesn't error out. It emits fluent, grammatically correct English
that has nothing to do with the source, most often fragments that read like Jehovah's Witnesses /
Watchtower literature. This is a known trap with small MT models, not a sign of carelessness: pushed
outside their comfort zone, they can fall back to memorized fragments of their training corpus
instead of failing loudly. OPUS-MT models are trained on the OPUS corpus, which has
Watchtower-published parallel text as one of its sources — this exact failure mode is a documented
quirk of that corpus, not a one-off fluke.

**Evidence**, verified directly against the committed `cg/translated_docs.json` (4 documents total):

| Hindi title (`inferred_title`) | Gloss | English output (`title_english`) | Location |
|---|---|---|---|
| `छत्तीसगढ़ शासन` | "Government of Chhattisgarh" | *"For example, in the United States, a number of young people have been forced to leave their homes and move to another country to serve where there is a greater need for Kingdom preachers."* | `cg/translated_docs.json:5,12` (doc_1), identically repeated at `:44,51` (doc_4) |
| `छत्तीसगढ़ महिला कोष` | "Chhattisgarh Women's Fund" | *"The six - month - old lady Koss."* | `cg/translated_docs.json:31,38` (doc_3) |

The body text is worse. Doc_1's `text_english` (`cg/translated_docs.json:13`) — a document that,
from its content, is almost certainly the mandatory RTI Act 2005 §4(1)(b) disclosure list —
contains, verbatim:

```
Preparing the Scriptures to be readily available
...Article 206 -4 Rings According to the U.S.News & World Report 2024...
...For information on how to write, write to the address shown on page 5 of this magazine...
```

None of that is remotely in the source. I checked all four documents, not just the two titles quoted
above: **three of the four (doc_1, doc_3, doc_4) have completely unusable titles**, and **all four
contain the contamination tokens somewhere in the body** — doc_1 alone has 84 occurrences of
"Jehovah." Even doc_2, whose title survived intact (`सक्षम योजना` → "Enabled Scheme",
`cg/translated_docs.json:18,25`), drifts into the same failure mode further into its body: *"The
article was published by the Watchtower Bible and Tract Society of New York, Inc."* This is not a
2-of-4 problem — it's 4 of 4, at varying severity.

**Why it matters.** `cg/index_docs.py:63` — `primary_text = text_en if text_en.strip() else text_hi`
— prefers the English translation over the Hindi original whenever one exists. So **all 526 indexed
chunks** (verified: `select count(*) from embeddings` against `cg/chroma_db/chroma.sqlite3` returns
exactly `526`) are embedded from this unreliable text. `cg/app.py:91` then labels it "🇬🇧 Translated
English Preview" for the user, and `cg/app.py:169-181` (the `ollama.generate(model="llama3.2", ...)`
call at `:177`) feeds the same text to Llama 3.2 to produce "policy summaries." A staff member
searching this database would be shown invented government policy, presented with full formatting
and confidence.

**The data should not be used until this is fixed.**

**Fix, in this order:**

1. `cg/index_docs.py:19` and `cg/app.py:17` already load
   `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` for embeddings — it handles Hindi
   natively. Translation isn't needed for search at all. Index the Hindi directly.
2. If English is wanted for display, use IndicTrans2 (AI4Bharat), purpose-built for Indian
   languages, or the Llama 3.2 you're already running locally via Ollama.
3. Either way, add a sanity check before storing or indexing a translation — source/output length
   ratio, a language-ID check on the output — that refuses anything failing it. This failure mode
   is silent, so it needs an automated guard, not just care.

---

## 🟠 HIGH — the Kruti Dev heuristic misfires on virtually all input

**What's wrong.** `cg/ocr.py:10-14`:

```python
def is_krutidev(text):
    kruti_signatures = ['NRR', 'kklu', '<+', 'f', 'j', 'd', 's', '=kk', 'â', 'ã']
    matches = sum(1 for char in kruti_signatures if char in text)
    return matches >= 2
```

Four of the ten "signatures" are single lowercase letters (`f`, `j`, `d`, `s`). Any English sentence
of normal length contains at least two of those, so `matches >= 2` fires on essentially everything.
Confirmed directly:

```
>>> is_krutidev("This is a plain English sentence from a government circular.")
True
```

**Why it matters.** `is_krutidev()` gates the fast path inside `extract_text_with_ocr_fallback`
(`cg/ocr.py:16-62`). It's run against a 3-page sample (`cg/ocr.py:24-29`); when it returns `True`,
`force_ocr` is set and the function skips past the cheap `pdfplumber` extraction — the `elif` branch
at `cg/ocr.py:32-38` that would otherwise return text directly — straight into the 150-DPI
`pdf2image` + Tesseract path (`cg/ocr.py:47-57`, `dpi=150` at `:49`). Since the heuristic fires on
nearly everything, that fast path is effectively dead code: every PDF pays for full image OCR
whether it needs it or not. The data confirms it — all 4 entries in `cg/processed_docs.json` show
`"was_ocr_used": true`. Tolerable at 45 PDFs; a hard blocker once this scales to other states.

**Fix.** Match on multi-character Kruti Dev signatures only (drop the single-letter entries), or
simpler — check whether the sample text already contains Devanagari and skip OCR if it does:

```python
import re
DEVANAGARI = re.compile(r'[\u0900-\u097F]')
def needs_ocr(sample_text):
    return not DEVANAGARI.search(sample_text)
```

---

## 🟡 MEDIUM — portability, reproducibility, hygiene

### Windows-only paths
`cg/ocr.py:8` hardcodes `pytesseract.pytesseract.tesseract_cmd = r'C:\Program
Files\Tesseract-OCR\tesseract.exe'`. Stored output paths use backslashes too — e.g.
`cg/processed_docs.json:17`, `"file_path": "cgwcd_all_pdfs\\-_37.pdf"` — which won't resolve on
macOS or Linux. Use `pathlib` / `os.path.join` throughout, and let `tesseract` resolve from `PATH`
with an environment-variable override for anyone who needs a specific binary.

### Download buttons are dead on a fresh clone
`cg/app.py:99-101` and `cg/app.py:165-167` both guard `st.download_button` with `if
os.path.exists(fpath):`. `fpath` points into `cg/cgwcd_all_pdfs/`, which is not in the repo —
verified absent (no directory matching `*pdfs*` exists anywhere in the tree). Clone this fresh and
run the app, and you get a working search UI with silently-missing download buttons on every single
result, and no error to explain why. Either commit a small sample of source PDFs for demo purposes,
or — better, given point 5 below — add a setup step that re-downloads the PDFs from the URLs already
sitting in `crawl_metadata.json`, so a fresh clone can reconstruct its own file store.

### 24 MB repo for ~31 KB of code
The working tree is 24 MB; the five pipeline scripts total ~31 KB combined. The rest is regenerable
build output, all committed:

- `cg/chroma_db/chroma.sqlite3` — 14.9 MB
- 4 orphaned collection UUID folders under `cg/chroma_db/` (`57e79382-…`, `6030f098-…`,
  `789e6e89-…`, `bba25d84-…`) — `cg/index_docs.py:24-26` deletes and recreates the named collection
  on every run but never cleans up the old on-disk segment directories Chroma leaves behind, so they
  accumulate
- `cg/translated_docs.json` — 1.19 MB
- `cg/processed_docs.json` — 797 KB

`.gitignore` (`.gitignore:1-2`) currently contains only `/planning` and `/archive`. None of the
above is covered, and a git repo isn't a good home for regenerable binary blobs anyway — every
clone, pull, and diff drags them along, and they'll only grow as you re-index more states. All of it
is regenerable from the source PDFs — ignore it, and if you want the database itself distributed,
ship it as a release artifact instead of a tracked file.

### No README, no requirements.txt
Both verified absent. Nothing pins the heavy, undeclared dependency list this pipeline actually
needs: `torch`, `transformers`, `sentence-transformers`, `chromadb`, `streamlit`, `ollama`,
`pytesseract`, `pdfplumber`, `pdf2image`, `beautifulsoup4`, `requests` — plus system-level Tesseract
with the Hindi language pack (`lang='hin+eng'`, `cg/ocr.py:52`) and Poppler (required by
`pdf2image.convert_from_path`, `cg/ocr.py:49`). Nobody else can run this today without
reverse-engineering the import list by hand, and there's no way to know which versions you actually
tested against. Add a `requirements.txt` (pin versions — `transformers`/`torch`/`chromadb` all break
each other across minor versions often enough that "whatever's latest" isn't safe), and a short
README covering the system-level installs and the order the five scripts need to run in.

### `bihar/scrap.py` is a near-exact copy of `cg/crawler.py`
Line-by-line, the two files differ in exactly 4 places — `cg/crawler.py:42,124,125,132`, at the same
line numbers in `bihar/scrap.py` — and all four are configuration: the `download_dir` default,
`START_URL`, `STATE`, and the `download_dir` keyword argument at the call site. That's 97% of lines
byte-identical (99.7% by character count). Every difference between them is already a variable, not
logic, so this is close to a free refactor: parameterize by state before it becomes twenty
near-identical copies where a bug fix has to be remembered and reapplied by hand in every one of
them.

### Crawler politeness
`cg/crawler.py` recurses into every same-domain link (the recursive call is `cg/crawler.py:121`)
with no delay between requests and no `robots.txt` check — confirmed by absence, there's no
`time.sleep` or `robotparser` anywhere in the file. The `User-Agent` (`cg/crawler.py:15-17`) spoofs
a real Chrome/Windows browser rather than identifying the project. Add a delay (~1s) between
requests, a descriptive `User-Agent` with a contact address, and respect `robots.txt`. Enfold's IP
getting blocked by a state government server would stop this project cold.

---

## 🔵 LOW — correctness details worth knowing

**Coverage is much smaller than it looks.** `cg/crawl_metadata.json` has 45 entries;
`cg/processed_docs.json` has 4. That's roughly 9% of a single state's crawl — worth stating plainly
so nobody mistakes the current output for coverage. It's actually a bit more lopsided than that:
`bihar/crawl_metadata.json` is also committed, with 90 entries — double Chhattisgarh's crawl —
meaning the Bihar crawler has already been run for real. But there's no `bihar/ocr.py`,
`bihar/translate_docs.py`, or `bihar/processed_docs.json` anywhere in the repo, so none of those 90
Bihar PDFs have been OCR'd, translated, or indexed. Two states have been crawled; one document, from
one state, is actually searchable today.

**The app promises states it doesn't have.** `cg/app.py:37`'s state filter lists `["All States",
"Chhattisgarh", "Andhra Pradesh", "Delhi", "Kerala", "Maharashtra", "Uttar Pradesh", "West Bengal"]`
— 7 real states plus the "All States" default — but only Chhattisgarh is indexed. (Bihar, the one
other state actually crawled, isn't even in this list.) A user who filters to "Kerala" gets a silent
zero-results screen that looks identical to "no matches for your query," not "we don't have this
state yet." Until coverage catches up with the list, either derive the filter options from `state`
values actually present in `raw_docs`/the collection metadata, or show a coverage indicator next to
each state.

**Hindi excerpts don't align with their English chunk.** `cg/index_docs.py:74` — `hi_excerpt =
text_hi[i * 700 : (i + 1) * 700] if text_hi else ""` — slices the *original* Hindi text at the same
character offsets as the *English* chunk index `i` (chunks built at `cg/index_docs.py:67` with
`chunk_size=800, overlap=100`, an effective 700-char step). Translation changes length, so the two
drift apart chunk over chunk, and the "corresponding" Hindi excerpt stops corresponding. Store each
chunk's source character offsets at chunk time instead of re-deriving them from the chunk index.

**O(n²) writes.** Both `cg/ocr.py:116-117` and `cg/translate_docs.py:132-134` rewrite their *entire*
JSON output after every single document, inside the per-document loop. Fine at 4 documents; slow at
thousands. Append-per-record (JSONL), or batch the writes every N documents.

**Ordering nit in the crawler.** `cg/crawler.py:50-53`:
```python
response = requests.get(current_url, headers=HEADERS, timeout=12)
if "text/html" not in response.headers.get("Content-Type", ""):
    return
response.raise_for_status()
```
The `Content-Type` check runs before `raise_for_status()`. Harmless today since both paths bail out
either way, but check status first so an error page never gets a chance to reach the parser
downstream.

---

## ✅ WHAT'S GENUINELY GOOD

The architecture instincts here are ahead of the execution, and for a first commit that's the right
way round to be wrong. Specifically:

- **Properly staged pipeline.** `cg/crawler.py` → `cg/ocr.py` → `cg/translate_docs.py` → `cg/index_docs.py` → `cg/app.py`. Each stage has its own `if __name__ == "__main__":` entry point and reads/writes its own JSON file, so each is independently runnable and re-runnable.
- **Resume capability**, in both `cg/ocr.py:75-81,87-88` and `cg/translate_docs.py:79-89,91` — both skip already-processed records on restart by diffing against what's already in the output file. Genuinely thoughtful for long-running jobs, and the kind of thing that's easy to skip on a first pass.
- **Memory discipline** in `cg/ocr.py:47-57` — PDFs are converted to images in page chunks (`chunk_size=5` pages) rather than all at once, with `del images` (`:56`) and `gc.collect()` (`:57`) after each chunk. That's the difference between this scaling to a 500-page PDF and not.
- **Correct batched inference** in `cg/translate_docs.py` — `torch.no_grad()` (`:58`), padding and truncation on the tokenizer call (`:53-54`), a sensible batch size with a comment explaining the hardware it was tuned for (`:114`), and a CUDA availability check (`:64`).
- **Keeping the Hindi original alongside the English**, throughout — `cg/index_docs.py:59-60` pulls both `text_en` and `text_hi` off every document, and stores a Hindi excerpt in the chunk metadata (`:74,85`) alongside the English chunk, which `cg/app.py` then surfaces in its bilingual tabs. For a legal corpus this is exactly right, and it's precisely what makes the fix for the critical finding above cheap — the ground truth was never thrown away.
- **Bilingual category inference.** `cg/crawler.py:19-33`'s `determine_category` checks both English and Devanagari keywords (`"act"` / `" अधिनियम"`, `"circular"` / `"परिपत्र"`, and so on) — a detail that's easy to skip if you're not thinking in both languages at once.
- **The Streamlit app's shape** — two search modes (`cg/app.py:28-31`), state/category filters (`:35-43`), bilingual excerpt tabs (`:91`, `:157`), and PDF access on every result (`:99-101`, `:165-167`) — is a genuinely useful tool once the data underneath it is trustworthy.
- **Sentence-boundary chunk-splitting**, including the Devanagari danda (`।`), in `cg/translate_docs.py:21` — splitting on `['। ', '. ', '।', ...]` rather than a hard character cutoff. Small detail, shows you were thinking about the script you were actually working with rather than assuming English punctuation.

---

## PRIORITISED NEXT STEPS

1. **Stop indexing translated text.** Embed the Hindi directly with the multilingual model you're already using (`paraphrase-multilingual-MiniLM-L12-v2`). Re-index.
2. **Fix `is_krutidev`** — a one-line change (drop the single-character signatures, or switch to a Devanagari-presence check) for a large speed win.
3. **Add `requirements.txt` and a short README** — setup including system dependencies (Tesseract + `hin` language pack, Poppler), and how to run each stage in order. Remove generated data and `chroma_db/` from git via `.gitignore`.
4. **Merge the two crawlers into one script parameterized by state.** Add a request delay and a `robots.txt` check while you're in there.
5. **Re-run all 45 (now 135, counting Bihar) documents, then eyeball a sample of the output against the source PDFs.** That single habit — actually reading a few translations next to their source — would have caught the critical bug on day one. It's the cheapest guardrail you have until the automated check in finding #1 is in place.

Good first commit. The critical fix is a day of work, not a rewrite — the rest of this is in good
enough shape that it's worth doing properly.
