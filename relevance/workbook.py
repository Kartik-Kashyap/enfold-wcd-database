"""Maintain the reviewers' workbook: one sheet per state, one row per document.

Two operations, both of which read a previous workbook and write a new one:

    # Add a state scored from crawl metadata only (no document text yet).
    python relevance/workbook.py title-only \
        --previous WCD-documents-6-states.xlsx --out WCD-documents-7-states.xlsx \
        --state Gujarat --metadata gujarat/crawl_metadata.json \
        --scores relevance/scores/gujarat.jsonl

    # Replace provisional rows with descriptions written from extracted text.
    python relevance/workbook.py summaries \
        --previous WCD-documents-7-states.xlsx --out WCD-documents-7-states.xlsx \
        --sheet "Karnataka (provisional)" --rename Karnataka \
        --scores relevance/scores/karnataka.json

    # Split crawl metadata into batches for the readers who assign the scores.
    python relevance/workbook.py batches --metadata gujarat/crawl_metadata.json --out-dir /tmp/gj

Score files are keyed by ``pdf_url``. See relevance/README.md for the scale and
the allowed values.
"""

from __future__ import annotations

import argparse
import collections
import copy
import json
import re
import sys
from pathlib import Path

import openpyxl
from openpyxl.worksheet.datavalidation import DataValidation

HEADER = [
    "Name of Document", "Language of the document", "Type of Document", "Date", "State",
    "Document Number", "Document Type", "2-3 line description of the document",
    "Link to document", "Relevancy Score", "Whether document is relevant (Manual Check)", "Notes",
]
WIDTHS = dict(zip("ABCDEFGHIJKL", (40, 14, 18, 12, 14, 24, 22, 70, 30, 10, 16, 45)))
LINK, SCORE, NOTES = 8, 9, 11
PROVISIONAL = "PROVISIONAL"

TYPES = {
    "Act", "Rules", "SOP / Guidelines", "Scheme Guidelines", "Government Order", "Notification",
    "Circular", "Office Order", "Letter", "Notice", "Report", "Form", "Recruitment / Transfer",
    "Tender / Procurement", "Other",
}
THEMES = {
    "Child Sexual Abuse / POCSO", "Child Marriage", "Juvenile Justice / CCIs",
    "Child Labour / Trafficking", "Child Protection", "Women's Safety / Violence",
    "Nutrition / ICDS / Anganwadi", "Education / ECCE", "Health", "Social Welfare Schemes",
    "Administration / HR", "Procurement / Finance", "Other",
}


def iso_date(value) -> str:
    """Normalise the crawlers' date formats to YYYY-MM-DD; anything else passes through."""
    s = str(value or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}"
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
    if m:
        return f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}"
    return s


def load_scores(path: Path) -> dict[str, dict]:
    """Read a .json array or .jsonl file of score records and validate every value."""
    text = path.read_text(encoding="utf-8")
    records = json.loads(text) if path.suffix == ".json" else [
        json.loads(line) for line in text.splitlines() if line.strip()
    ]
    scores: dict[str, dict] = {}
    for n, rec in enumerate(records, 1):
        problems = [
            f"{field} {rec.get(field)!r}"
            for field, allowed in (("type", TYPES), ("theme", THEMES), ("score", {1, 2, 3, 4, 5}))
            if rec.get(field) not in allowed
        ]
        if problems:
            sys.exit(f"{path}: record {n}: not an allowed value: " + ", ".join(problems))
        if rec["pdf_url"] in scores:
            sys.exit(f"{path}: record {n}: duplicate pdf_url {rec['pdf_url']}")
        scores[rec["pdf_url"]] = rec
    return scores


def data_rows(ws) -> list[list]:
    return [list(r[:12]) for r in ws.iter_rows(min_row=2, values_only=True) if r[0]]


def write_rows(ws, rows: list[list], template) -> None:
    """Replace the sheet's body with ``rows``, styled like ``template``'s header and first row."""
    if ws.max_row > 1:
        ws.delete_rows(2, ws.max_row)
    ws.data_validations.dataValidation.clear()
    for row in rows:
        ws.append(row)
    for col, width in WIDTHS.items():
        ws.column_dimensions[col].width = width
    for c in range(1, 13):
        ws.cell(1, c)._style = copy.copy(template.cell(1, c)._style)
        for r in range(2, len(rows) + 2):
            ws.cell(r, c)._style = copy.copy(template.cell(2, c)._style)
    for r in range(2, len(rows) + 2):
        ws.cell(r, LINK + 1).hyperlink = ws.cell(r, LINK + 1).value
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:L{len(rows) + 1}"
    checkbox = DataValidation(type="list", formula1='"TRUE,FALSE"', allow_blank=True)
    checkbox.add(f"K2:K{len(rows) + 1}")
    ws.add_data_validation(checkbox)


def template_sheet(wb, avoid=None):
    """Any populated state sheet, used as the source of cell styles."""
    for ws in wb:
        if ws.title != "Summary" and ws is not avoid and ws.max_row > 1:
            return ws
    sys.exit("The workbook has no populated state sheet to copy styles from.")


def summary_label(ws) -> str:
    rows = data_rows(ws)
    provisional = sum(1 for r in rows if str(r[NOTES] or "").startswith(PROVISIONAL))
    state = re.sub(r"\s*\(provisional\)\s*", "", ws.title).strip()
    if provisional == len(rows):
        return f"{state} (provisional, title only)"
    if provisional:
        return f"{state} ({len(rows) - provisional} summarised from text, {provisional} title only)"
    return state


def rebuild_summary(wb, notes: list[str]) -> None:
    """Recount every state sheet; keep the duplicate counts and the first note already there."""
    sm = wb["Summary"]
    old = [list(r) for r in sm.iter_rows(values_only=True)]
    duplicates = {
        re.split(r"\s*\(", str(r[0]))[0].strip(): r[2]
        for r in old[1:] if r[0] and isinstance(r[1], int)
    }
    old_notes = [r[0] for r in old[1:] if r[0] and r[1] is None]
    total_row = next(i for i, r in enumerate(old, 1) if r[0] == "Total")
    body_style = [copy.copy(sm.cell(2, c)._style) for c in range(1, 9)]
    total_style = [copy.copy(sm.cell(total_row, c)._style) for c in range(1, 9)]
    note_style = copy.copy(sm.cell(total_row + 1, 1)._style)

    for merged in list(sm.merged_cells.ranges):
        sm.unmerge_cells(str(merged))
    sm.delete_rows(2, sm.max_row)
    states = [ws for ws in wb if ws.title != "Summary"]
    for ws in states:
        counts = collections.Counter(r[SCORE] for r in data_rows(ws))
        label = summary_label(ws)
        sm.append([label, sum(counts.values()), duplicates.get(re.split(r"\s*\(", label)[0], 0)]
                  + [counts.get(s, 0) for s in (5, 4, 3, 2, 1)])
    last = len(states) + 1
    sm.append(["Total"] + [f"=SUM({col}2:{col}{last})" for col in "BCDEFGH"])
    all_notes = old_notes[:1] + notes
    for note in all_notes:
        sm.append([note])
    for r in range(2, last + 1):
        for c in range(1, 9):
            sm.cell(r, c)._style = copy.copy(body_style[c - 1])
    for c in range(1, 9):
        sm.cell(last + 1, c)._style = copy.copy(total_style[c - 1])
    for r in range(last + 2, last + 2 + len(all_notes)):
        sm.cell(r, 1)._style = copy.copy(note_style)
        sm.merge_cells(start_row=r, start_column=1, end_row=r, end_column=8)
        sm.row_dimensions[r].height = 75


def cmd_title_only(args) -> None:
    wb = openpyxl.load_workbook(args.previous)
    metadata = json.loads(Path(args.metadata).read_text(encoding="utf-8"))
    scores = load_scores(Path(args.scores))
    missing = [m["pdf_url"] for m in metadata if m["pdf_url"] not in scores]
    if missing:
        sys.exit(f"{len(missing)} documents have no score, e.g. {missing[0]}")

    rows = []
    for meta in metadata:
        s = scores[meta["pdf_url"]]
        title = meta.get("subject") or meta.get("link_text") or meta["filename"]
        detail = "; ".join(
            f"{label}: {meta[key]}" for label, key in (("Branch", "branch"), ("listed as", "document_type"))
            if meta.get(key)
        )
        description = f"Pending OCR. Original title: {title}." + (f" {detail}." if detail else "")
        rows.append([
            s["name"], s["language"], s["type"],
            iso_date(meta.get("table_date") or meta.get("document_date")), args.state,
            meta.get("order_no") or "", s["theme"], description, meta["pdf_url"], s["score"], False,
            f"{PROVISIONAL} — scored from title only (no OCR yet); Score reason: {s['reason']}",
        ])
    rows.sort(key=lambda r: (-r[SCORE], r[6], r[3]))

    ws = wb.create_sheet(f"{args.state} (provisional)")
    ws.append(HEADER)
    write_rows(ws, rows, template_sheet(wb, avoid=ws))
    rebuild_summary(wb, args.note)
    wb.save(args.out)
    print(f"{args.out}: {ws.title} — {len(rows)} rows")


def cmd_summaries(args) -> None:
    wb = openpyxl.load_workbook(args.previous)
    ws = wb[args.sheet]
    scores = load_scores(Path(args.scores))
    rows = data_rows(ws)
    by_url = {r[LINK]: r for r in rows}
    unknown = [url for url in scores if url not in by_url]
    if unknown:
        sys.exit(f"{len(unknown)} scored documents are not in sheet {args.sheet!r}, e.g. {unknown[0]}")

    summarised = []
    for url, s in scores.items():
        old = by_url[url]
        note = "; ".join(p for p in (f"Score reason: {s['reason']}", s.get("caveat"),
                                     "Summarised from the extracted text") if p)
        summarised.append([
            s["name"], s["language"], s["type"], s.get("date") or old[3], old[4],
            s.get("doc_number") or old[5], s["theme"], s["description"], url, s["score"], old[10], note,
        ])
    summarised.sort(key=lambda r: -r[SCORE])
    untouched = [r for r in rows if r[LINK] not in scores]
    write_rows(ws, summarised + untouched, template_sheet(wb, avoid=ws))
    if args.rename:
        ws.title = args.rename
    rebuild_summary(wb, args.note)
    wb.save(args.out)
    print(f"{args.out}: {ws.title} — {len(summarised)} summarised, {len(untouched)} unchanged")


def cmd_batches(args) -> None:
    metadata = json.loads(Path(args.metadata).read_text(encoding="utf-8"))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fields = ("pdf_url", "subject", "link_text", "order_no", "document_type", "branch", "category")
    for n, start in enumerate(range(0, len(metadata), args.size)):
        lines = [
            json.dumps({k: m[k] for k in fields if m.get(k)}, ensure_ascii=False)
            for m in metadata[start:start + args.size]
        ]
        (out_dir / f"batch_{n}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{out_dir}: {len(metadata)} documents in batches of {args.size}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def workbook_args(p):
        p.add_argument("--previous", required=True, help="Workbook to start from")
        p.add_argument("--out", required=True, help="Workbook to write")
        p.add_argument("--scores", required=True, help="Score records keyed by pdf_url (.json or .jsonl)")
        p.add_argument("--note", action="append", default=[],
                       help="Note for the Summary sheet (repeatable); replaces all but the first existing note")

    p = sub.add_parser("title-only", help="Add a state scored from crawl metadata")
    workbook_args(p)
    p.add_argument("--state", required=True, help="State name as it should appear, e.g. Gujarat")
    p.add_argument("--metadata", required=True, help="The state's crawl_metadata.json")
    p.set_defaults(func=cmd_title_only)

    p = sub.add_parser("summaries", help="Replace provisional rows with text-based descriptions")
    workbook_args(p)
    p.add_argument("--sheet", required=True, help="Existing sheet to update")
    p.add_argument("--rename", help="New title for the sheet")
    p.set_defaults(func=cmd_summaries)

    p = sub.add_parser("batches", help="Split crawl metadata into reader batches")
    p.add_argument("--metadata", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--size", type=int, default=200)
    p.set_defaults(func=cmd_batches)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
