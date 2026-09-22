"""Build Chroma ``where`` clauses from the app's filter selections.

This lives in its own module for two reasons.

**It must be testable.**  The app is a Streamlit script: importing ``app.py``
executes it.  So filter construction that lives inline there can only be
exercised by driving a browser, and the date filter duly shipped broken --
it raised only when a user ticked "Enable date filter", so the suite stayed
green.

**It must be importable cheaply.**  ``pipeline/index.py`` owns the collection
and the metadata schema, which makes it the obvious home for this, but it
imports ``chromadb`` and ``sentence_transformers`` at module level.  Importing
it from the tests would add ~30 s of torch loading to a suite that currently
runs in about a second.  This module depends on ``dates`` alone.
"""

from __future__ import annotations

from . import dates

__all__ = ["build_where_filter"]


def build_where_filter(state: str | None = None, category: str | None = None,
                       date_from: str | None = None, date_to: str | None = None):
    """Build the Chroma ``where`` clause for the given filter selections.

    Chroma allows exactly ONE operator per expression, so a date range is two
    conditions joined by ``$and``.  Writing it as a single expression::

        {"document_date_epoch": {"$gte": lo, "$lte": hi}}

    is rejected at query time with::

        ValueError: Expected operator expression to have exactly one operator

    Returns ``None`` when nothing is selected, so callers can pass the result
    straight to ``collection.query(where=...)``.  A date bound that does not
    parse is dropped rather than emitted as ``None``: Chroma rejects ``None``
    inside a where clause, and ``{"document_date_epoch": None}`` would match
    nothing anyway.

    Epochs come from ``dates.date_epoch`` -- the same function ``index.py``
    stores metadata with, so the app's range and the indexed values cannot
    drift apart.
    """
    conditions: list[dict] = []
    if state:
        conditions.append({"state": state})
    if category:
        conditions.append({"category": category})

    for iso, op in ((date_from, "$gte"), (date_to, "$lte")):
        epoch = dates.date_epoch(iso)
        if epoch is not None:
            conditions.append({"document_date_epoch": {op: epoch}})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}
