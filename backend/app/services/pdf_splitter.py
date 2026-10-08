"""Building a volume PDF from a page range and rebuilding its bookmarks."""
from __future__ import annotations

from typing import List, Optional, Sequence

import pymupdf as fitz  # PyMuPDF

# A TOC entry as passed between processes: [level, title, page_or_None, parent_index_or_None]
TocRow = Sequence


def volume_toc(toc: Sequence[TocRow], start: int, end: int) -> List[list]:
    """Bookmarks for a volume holding original pages start..end (1-based, inclusive).

    * Only bookmarks whose target page is inside the volume are kept.
    * Targets are renumbered relative to the volume (original page ``start`` becomes page 1).
    * If a kept bookmark's parent lies outside the volume, the parent is recreated (pointing at
      the first kept child, titled "... (continued)" when it started earlier) so that the
      hierarchy stays valid and no bookmark points to a page that does not exist.
    """
    result: List[list] = []
    emitted: dict[int, bool] = {}

    def chain(idx: int) -> List[int]:
        out = []
        parent = toc[idx][3]
        while parent is not None:
            out.append(parent)
            parent = toc[parent][3]
        return list(reversed(out))

    for idx, row in enumerate(toc):
        level, title, page = row[0], row[1], row[2]
        if page is None or not (start <= page <= end):
            continue
        for anc in chain(idx):
            if anc in emitted:
                continue
            a_level, a_title, a_page = toc[anc][0], toc[anc][1], toc[anc][2]
            suffix = " (continued)" if (a_page is not None and a_page < start) else ""
            result.append([a_level, f"{a_title}{suffix}", page - start + 1])
            emitted[anc] = True
        result.append([level, title, page - start + 1])
        emitted[idx] = True
    return result


def build_volume(
    src: fitz.Document,
    start: int,
    end: int,
    toc: Optional[Sequence[TocRow]] = None,
) -> fitz.Document:
    """Return a new in-memory document with original pages start..end (1-based) in order."""
    out = fitz.open()
    out.insert_pdf(src, from_page=start - 1, to_page=end - 1)
    if out.page_count != end - start + 1:
        raise RuntimeError(
            f"Page copy failed: expected {end - start + 1} pages, got {out.page_count}."
        )
    apply_toc(out, toc, start, end)
    return out


def apply_toc(doc: fitz.Document, toc: Optional[Sequence[TocRow]], start: int, end: int) -> int:
    """Set the volume bookmarks (or clear them when ``toc`` is None). Returns bookmark count."""
    rows = volume_toc(toc, start, end) if toc else []
    doc.set_toc(rows)
    return len(rows)
