# Child Rights & Policy Document Database

Crawls Indian state Women & Child Development portals for Acts, Rules, Circulars,
Schemes and SOPs; extracts their text (OCR where needed); indexes it for
semantic search; and serves it through a Streamlit UI with bilingual display.

**Hindi is the source of truth.** Search runs on the original Hindi text.
English is generated on demand, one excerpt at a time, and checked before it is
shown. See [Why there is no translation stage](#why-there-is-no-translation-stage).

---

## Quick start

```bash
pip install -r requirements.txt          # Python deps
# plus the system deps below (Tesseract + Hindi pack, Poppler)

python run.py status                     # what's actually in the database
python run.py fetch  --all               # download PDFs from tracked crawl metadata
python run.py ocr    --all               # extract text
python run.py index  --all               # embed the Hindi text
python run.py app                        # launch the UI
```

A fresh clone has the crawl metadata but no PDFs (they are regenerable and stay
out of git), so `fetch` is the first step. `run.py status` will tell you what is
missing at any point.

---

## System dependencies

Not installable via pip. All three are required for the stages that use them.

### Tesseract OCR + Hindi language pack

Needed by `run.py ocr`. The `hin` traineddata is required — without it Hindi OCR
output is unusable.

| OS | Install |
|---|---|
| Windows | [UB-Mannheim build](https://github.com/UB-Mannheim/tesseract/wiki) — tick "Hindi" under *Additional language data* |
| macOS | `brew install tesseract tesseract-lang` |
| Debian/Ubuntu | `sudo apt install tesseract-ocr tesseract-ocr-hin` |

The binary is located from `PATH`, or from `TESSERACT_CMD` if you need a specific
one — no hardcoded paths:

```bash
# only if tesseract isn't on PATH
export TESSERACT_CMD="/opt/homebrew/bin/tesseract"                    # macOS/Linux
setx TESSERACT_CMD "C:\Program Files\Tesseract-OCR\tesseract.exe"     # Windows
```

### Poppler

Needed by `pdf2image` to rasterise PDF pages.

| OS | Install |
|---|---|
| Windows | [poppler-windows releases](https://github.com/oschwartz10612/poppler-windows/releases) — add its `bin\` to `PATH` |
| macOS | `brew install poppler` |
| Debian/Ubuntu | `sudo apt install poppler-utils` |

### Ollama (optional)

Only for on-demand translation and summaries in the UI. Search works without it.

```bash
ollama pull llama3.2
ollama serve
```

---

## Pipeline stages

```
crawl  ──►  fetch  ──►  ocr  ──►  index  ──►  app
  │           │           │         │
  │           │           │         └─ chroma_db/            (Hindi embeddings)
  │           │           └─ <state>/processed_docs.json     (extracted text)
  │           └─ <state>/<state>_all_pdfs/                   (PDF store)
  └─ <state>/crawl_metadata.json                             (tracked in git)
```

Each stage is independently re-runnable and resumes where it left off.
`--state <key>` targets one state; `--all` targets every configured state.

| Command | Does |
|---|---|
| `run.py status` | Real coverage per state, index size, indexed language |
| `run.py crawl --state cg` | Polite crawl: obeys `robots.txt`, 1s delay, honest User-Agent |
| `run.py fetch --all` | Re-download PDFs listed in crawl metadata but absent locally |
| `run.py ocr --all` | Extract text; OCR only when the text layer is unusable |
| `run.py index --all` | Embed **Hindi** text into Chroma (per-state, incremental) |
| `run.py app` | Launch Streamlit |
| `run.py audit --file cg/translated_docs.json` | Run the quality guard over old MT output |

Useful flags: `crawl --depth 2 --delay 1.0 --max-pages 50`, `ocr --limit 5`,
`index --no-prune`, `app --port 8502`.

### Before crawling

Set a contact address. A server admin who can reach you sends an email; one who
cannot sends a firewall rule.

```bash
export CRAWLER_CONTACT="you@example.org"     # Windows: setx CRAWLER_CONTACT "you@example.org"
```

---

## Adding a state

Add one entry to `pipeline/states.py`:

```python
"mp": StateConfig(
    key="mp",
    name="Madhya Pradesh",
    start_url="https://mpwcdmis.nic.in/",
    data_dirname="mp",
    pdf_dirname="mpwcd_all_pdfs",
),
```

Then `python run.py crawl --state mp && python run.py ocr --state mp && python run.py index --state mp`.
There is no per-state script to copy — that was the bug this replaced.

---

## Why there is no translation stage

The first version of this pipeline translated every document with
`Helsinki-NLP/opus-mt-hi-en` and indexed the English output. On hard input —
dense bureaucratic Hindi, OCR noise, repetition — that model does not fail
loudly. It emits fluent, grammatical English unrelated to the source, most often
memorised fragments of Watchtower / Jehovah's Witnesses literature, which is
present in the OPUS corpus it was trained on.

The result: 3 of 4 documents had unusable titles, all 4 carried contamination in
the body (`doc_1` alone: 84 occurrences of "Jehovah"), and all 526 indexed chunks
were embedded from that text. `छत्तीसगढ़ शासन` ("Government of Chhattisgarh")
was displayed as *"For example, in the United States, a number of young people
have been forced to leave their homes…"*

Three changes, in the order that matters:

1. **Hindi is indexed, not English.** `paraphrase-multilingual-MiniLM-L12-v2`
   handles Hindi natively, so translation was never needed for search. The
   embedding model is multilingual, so an English query still matches Hindi text.
2. **No bulk translation.** English is generated per excerpt, only when someone
   clicks, by local llama3.2 — then cached to disk, so the same excerpt is never
   paid for twice. Opening the app translates nothing.
3. **Every translation is guarded.** `pipeline/quality.py` checks length ratio,
   script (must be Latin, must not still be Devanagari), degenerate repetition,
   a contamination blocklist, and **model commentary** — an instruction-tuned
   model asked to translate will sometimes answer *about* the request instead of
   performing it. Observed during this work, from llama3.2 on a whitespace-only
   fragment:

   > *"I can't provide a translation of that text as it appears to be a single
   > space character (" ") and does not contain any meaningful information."*

   Fluent English that is not a translation is the same failure class as the
   original bug, so it is caught the same way: unusable fragments are never sent,
   whole-chunk refusals are dropped, and appended remarks are stripped line by
   line so a good translation is not thrown away with them. Failures are shown as
   failures — a fluent wrong translation is worse than none.

See it working on the original bad data:

```
$ python run.py audit --file cg/translated_docs.json
  [FAIL] doc_1 title: length ratio 13.36 outside [0.2, 6.0]; training-corpus contamination: kingdom preachers
  [FAIL] doc_1 body: training-corpus contamination: awake!, bible and tract society, god's kingdom, jehovah, jw.org
  ...
  6 field(s) rejected by the guard.
```

**The guard's honest limit.** It catches gross failures, not subtle ones.
`छत्तीसगढ़ महिला कोष` → *"The six-month-old lady Koss."* is wrong and passes
every mechanical check. No automated check will catch that, which is the real
reason the index is built from Hindi: the correctness of search must not depend
on the correctness of a translation. Machine output in the UI is labelled as
unverified, and the source PDF is one click away.

`cg/translated_docs.json` holds the original contaminated output. It is
gitignored, so it exists only on machines that ran the old pipeline — the `audit`
example above will report a missing file on a fresh clone. Nothing in the
pipeline reads it. The same evidence is pinned in `tests/test_pipeline.py`
(`TestTranslationGuard`), which uses the real fabricated strings verbatim, so
`python -m pytest -q` reproduces the finding anywhere.

---

## Layout

```
run.py                     Single CLI entry point
pipeline/
  states.py                State registry — the only place states are configured
  paths.py                 Portable paths, Tesseract discovery
  crawler.py               Polite crawler + PDF re-fetch
  ocr.py                   Text extraction, OCR only when needed
  chunking.py              Chunking with exact source offsets (dependency-free)
  index.py                 Hindi embeddings into Chroma
  translate.py             On-demand translation, cached + guarded
  quality.py               needs_ocr + translation guard
  jsonio.py                Atomic, batched JSON writes
  app.py                   Streamlit UI
tests/test_pipeline.py     53 tests (python -m pytest -q)
cg/, bihar/                Per-state DATA only
chroma_db/                 Shared vector store (gitignored)
```

### What is and isn't in git

Tracked: code, `requirements.txt`, and `<state>/crawl_metadata.json` — small,
and the record that lets a clone rebuild everything else.

Ignored: `chroma_db/`, `*_all_pdfs/`, `processed_docs.json`,
`translated_docs.json`, `translation_cache.json`. These are regenerable build
output; the repo was 24 MB for ~31 KB of code before they were ignored. If you
want the vector store distributed, ship it as a release artifact, not a tracked
file.

---

## Tests

```bash
python -m pytest -q          # 53 tests, <1s
```

They cover the guards that have to work: the OCR gate (including the exact
plain-English regression that made the fast path unreachable) and the
translation guard (including the real fabricated strings that shipped, and the
real llama3.2 refusal observed while building this).

---

## Current coverage

Run `python run.py status` for live numbers. As of the last run: Chhattisgarh has
45 PDFs crawled and 4 with extracted text; Bihar has 90 crawled and 0 processed.
Two states crawled, one partially searchable — treat the current index as a
demo, not as coverage.
