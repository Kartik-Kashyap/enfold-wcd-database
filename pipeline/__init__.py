"""Child Rights & Policy document pipeline.

Stages, in order:

    crawl  ->  ocr  ->  index  ->  app

Translation is deliberately *not* a stage.  English is generated on demand,
per excerpt, at display time (see ``pipeline.translate``).  The searchable
index is built from the Hindi source text, which is the ground truth.
"""

__all__ = ["paths", "states", "quality", "crawler", "ocr", "index", "translate"]
