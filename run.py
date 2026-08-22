"""Single entry point for every pipeline stage.

    python run.py status                      # what's actually in the database
    python run.py crawl  --state cg           # polite crawl of one state
    python run.py fetch  --all                # re-download PDFs from crawl metadata
    python run.py ocr    --state bihar        # extract text (OCR only when needed)
    python run.py index  --all                # embed the HINDI text into Chroma
    python run.py app                         # launch the Streamlit UI
    python run.py audit  --file cg/translated_docs.json
                                              # run the quality guard over old MT output

Replaces the per-state script copies: one implementation, states configured in
``pipeline/states.py`` (review finding #7).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline import jsonio, paths, states as states_mod  # noqa: E402


def _add_state_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state", action="append", metavar="KEY",
                        help="State key (repeatable): " + ", ".join(sorted(states_mod.STATES)))
    parser.add_argument("--all", action="store_true", help="Apply to every configured state")


def cmd_status(args) -> int:
    paths.configure_stdout()
    print("=" * 66)
    print("  Child Rights & Policy Database — coverage")
    print("=" * 66)

    total_crawled = total_processed = 0
    for state in states_mod.all_states():
        crawled = jsonio.read_json(state.crawl_metadata, default=[]) or []
        processed = jsonio.read_json(state.processed_docs, default=[]) or []
        on_disk = len(list(state.pdf_dir.glob("*.pdf"))) if state.pdf_dir.exists() else 0
        ocr_used = sum(1 for d in processed if d.get("was_ocr_used"))
        total_crawled += len(crawled)
        total_processed += len(processed)

        pct = (len(processed) / len(crawled) * 100) if crawled else 0.0
        print(f"\n  {state.name}  [{state.key}]  {state.start_url}")
        print(f"    crawled (metadata) : {len(crawled)}")
        print(f"    PDFs on disk       : {on_disk}"
              + ("" if on_disk >= len(crawled) else
                 f"   -> {len(crawled) - on_disk} missing, run: python run.py fetch --state {state.key}"))
        print(f"    text extracted     : {len(processed)}  ({pct:.0f}% of crawl)"
              + (f"   [{ocr_used} needed OCR]" if processed else ""))

    print(f"\n  TOTAL: {total_processed} of {total_crawled} crawled PDFs have extracted text.")

    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(paths.CHROMA_DIR))
        collection = client.get_collection(paths.COLLECTION_NAME)
        count = collection.count()
        print(f"  Vector index: {count} chunks in {paths.CHROMA_DIR}")
        if count:
            sample = collection.get(limit=min(count, 500), include=["metadatas"])
            langs = {m.get("language", "?") for m in sample["metadatas"]}
            by_state: dict[str, int] = {}
            for m in sample["metadatas"]:
                by_state[m.get("state", "?")] = by_state.get(m.get("state", "?"), 0) + 1
            print(f"    indexed language(s): {', '.join(sorted(langs))} "
                  f"{'(Hindi source — correct)' if langs == {'hi'} else '(!! expected only hi)'}")
            print(f"    sample by state:     {by_state}")
    except Exception as exc:
        print(f"  Vector index: not built yet ({exc})")
        print("    Build it with: python run.py index --all")

    from pipeline import translate
    ok, msg = translate.is_available()
    cache = translate.TranslationCache()
    print(f"\n  On-demand translation: {'ready' if ok else msg}")
    print(f"    cached excerpts: {len(cache)}  ({paths.TRANSLATION_CACHE})")
    print()
    return 0


def cmd_crawl(args) -> int:
    from pipeline.crawler import crawl_state
    targets = states_mod.resolve(args.state, args.all)
    for state in targets:
        crawl_state(state, max_depth=args.depth, delay=args.delay,
                    obey_robots=not args.ignore_robots, max_pages=args.max_pages)
    return 0


def cmd_fetch(args) -> int:
    from pipeline.crawler import fetch_missing_pdfs
    for state in states_mod.resolve(args.state, args.all):
        fetch_missing_pdfs(state, delay=args.delay)
    return 0


def cmd_ocr(args) -> int:
    from pipeline.ocr import process_state, TesseractMissing
    try:
        for state in states_mod.resolve(args.state, args.all):
            process_state(state, limit=args.limit)
    except TesseractMissing as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_index(args) -> int:
    from pipeline.index import index_states
    index_states(states_mod.resolve(args.state, args.all), prune=not args.no_prune)
    return 0


def cmd_app(args) -> int:
    app_path = paths.REPO_ROOT / "pipeline" / "app.py"
    cmd = [sys.executable, "-m", "streamlit", "run", str(app_path)]
    if args.port:
        cmd += ["--server.port", str(args.port)]
    print("Launching:", " ".join(cmd))
    return subprocess.call(cmd)


def cmd_audit(args) -> int:
    """Run the translation quality guard over an existing MT output file.

    Demonstrates that the guard catches the failure described in review finding
    #1, on the very data that shipped with the bug.
    """
    paths.configure_stdout()
    from pipeline import quality

    target = Path(args.file)
    if not target.is_absolute():
        target = paths.REPO_ROOT / target
    docs = jsonio.read_json(target, default=None)
    if docs is None:
        print(f"Cannot read {target}", file=sys.stderr)
        return 1

    print(f"Auditing {len(docs)} record(s) in {target}\n")
    failures = 0
    for doc in docs:
        doc_id = doc.get("id", "?")
        for field_hi, field_en, label in (
            ("inferred_title", "title_english", "title"),
            ("text", "text_english", "body"),
        ):
            source = doc.get(field_hi, "") or ""
            output = doc.get(field_en, "") or ""
            if not source or not output:
                continue
            check = quality.check_translation(source, output)
            mark = "PASS" if check.ok else "FAIL"
            if not check.ok:
                failures += 1
            print(f"  [{mark}] {doc_id} {label}: {check.summary()}")
    print(f"\n{failures} field(s) rejected by the guard.")
    if failures:
        print("These are exactly the fabricated translations from review finding #1.")
        print("Nothing from this file is used by the pipeline any more — the index is built")
        print("from the Hindi source (pipeline/index.py) and English is generated on demand.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Child Rights & Policy document pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="Show real coverage and index state")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("crawl", help="Crawl a state portal for PDFs (polite)")
    _add_state_args(p)
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--delay", type=float, default=1.0, help="Seconds between requests (default 1.0)")
    p.add_argument("--max-pages", type=int, default=None)
    p.add_argument("--ignore-robots", action="store_true",
                   help="Do not honour robots.txt (not recommended)")
    p.set_defaults(func=cmd_crawl)

    p = sub.add_parser("fetch", help="Re-download PDFs listed in crawl_metadata.json")
    _add_state_args(p)
    p.add_argument("--delay", type=float, default=1.0)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("ocr", help="Extract text from PDFs (OCR only when needed)")
    _add_state_args(p)
    p.add_argument("--limit", type=int, default=None, help="Process at most N new PDFs")
    p.set_defaults(func=cmd_ocr)

    p = sub.add_parser("index", help="Embed Hindi text into the Chroma index")
    _add_state_args(p)
    p.add_argument("--no-prune", action="store_true", help="Keep orphaned segment dirs")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("app", help="Launch the Streamlit UI")
    p.add_argument("--port", type=int, default=None)
    p.set_defaults(func=cmd_app)

    p = sub.add_parser("audit", help="Run the translation quality guard over an MT output file")
    p.add_argument("--file", default="cg/translated_docs.json")
    p.set_defaults(func=cmd_audit)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
