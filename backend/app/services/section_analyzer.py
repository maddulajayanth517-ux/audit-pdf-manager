"""Turning bookmarks (or manual definitions) into contiguous, indivisible page sections."""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import pymupdf as fitz  # PyMuPDF

from ..models.pdf_models import Section
from .bookmark_analyzer import TocEntry, ancestors
from .pdf_reader import pdf_bytes


class SectionError(ValueError):
    pass


def sections_for_level(
    entries: List[TocEntry], page_count: int, level: int
) -> Tuple[List[Section], List[str]]:
    """Build sections whose boundaries are the start pages of all bookmarks at ``level`` or above.

    Using every bookmark with level <= the selected level (not only those *at* the level) means a
    level-1 Annexure without children still gets its own section when level 2 is selected.
    """
    warnings: List[str] = []
    by_id = {e.id: e for e in entries}
    chosen = [e for e in entries if e.level <= level and e.page is not None]
    if not chosen:
        return [], ["No usable bookmarks exist at the selected level."]

    pages_in_outline_order = [e.page for e in chosen]
    if pages_in_outline_order != sorted(pages_in_outline_order):
        warnings.append(
            "Some bookmarks are not in page order. Sections were ordered by page number; please review them."
        )

    # Group bookmarks that start on the same page; the deepest one names the section.
    by_page: Dict[int, List[TocEntry]] = {}
    for e in chosen:
        by_page.setdefault(e.page, []).append(e)

    starts = sorted(by_page)
    sections: List[Section] = []
    if starts[0] > 1:
        sections.append(
            Section(
                title="Front pages (before the first bookmark)",
                start_page=1,
                end_page=starts[0] - 1,
                protected=True,
                source="front-matter",
            )
        )
    for i, start in enumerate(starts):
        end = (starts[i + 1] - 1) if i + 1 < len(starts) else page_count
        group = by_page[start]
        deepest_level = max(e.level for e in group)
        named = [e for e in group if e.level == deepest_level]
        title = " / ".join(e.title for e in named)
        chain = [a.title for a in ancestors(by_id, named[0])]
        sections.append(
            Section(
                title=title,
                start_page=start,
                end_page=end,
                protected=True,
                path=" › ".join(chain),
                source="bookmark",
            )
        )
    return sections, warnings


def whole_document_section(page_count: int) -> Section:
    return Section(
        title="Entire document (no usable bookmarks)",
        start_page=1,
        end_page=page_count,
        protected=False,
        source="whole-document",
    )


def normalize_sections(sections: List[Section], page_count: int) -> Tuple[List[Section], List[str]]:
    """Validate client-supplied sections and make them cover every page exactly once.

    Overlaps and out-of-range pages are errors (they could duplicate or lose pages).
    Gaps are filled with *unprotected* sections so that no page is ever dropped.
    """
    warnings: List[str] = []
    for s in sections:
        if s.start_page > s.end_page:
            raise SectionError(f"Section '{s.title}': start page {s.start_page} is after end page {s.end_page}.")
        if s.end_page > page_count:
            raise SectionError(
                f"Section '{s.title}': end page {s.end_page} is beyond the last page ({page_count})."
            )
    ordered = sorted(sections, key=lambda s: (s.start_page, s.end_page))
    result: List[Section] = []
    expected = 1
    for s in ordered:
        if s.start_page < expected:
            prev = result[-1] if result else None
            raise SectionError(
                f"Sections overlap: '{s.title}' (pages {s.start_page}–{s.end_page}) overlaps "
                f"'{prev.title if prev else 'previous section'}'. Each page may belong to only one section."
            )
        if s.start_page > expected:
            result.append(_gap(expected, s.start_page - 1))
        result.append(s.model_copy(update={"title": (s.title or "").strip() or f"Pages {s.start_page}–{s.end_page}"}))
        expected = s.end_page + 1
    if expected <= page_count:
        result.append(_gap(expected, page_count))
    gaps = [s for s in result if s.source == "gap"]
    if gaps:
        warnings.append(
            f"{len(gaps)} page range(s) were not part of any defined section and were added as unprotected sections: "
            + ", ".join(f"{g.start_page}–{g.end_page}" for g in gaps)
        )
    return result, warnings


def _gap(start: int, end: int) -> Section:
    return Section(
        title=f"Pages {start}–{end} (not in a defined section)",
        start_page=start,
        end_page=end,
        protected=False,
        source="gap",
    )


def measure_range_size(doc: fitz.Document, start: int, end: int) -> int:
    """Actual size in bytes of the given page range when saved as its own PDF."""
    sub = fitz.open()
    try:
        sub.insert_pdf(doc, from_page=start - 1, to_page=end - 1)
        return len(pdf_bytes(sub))
    finally:
        sub.close()


MIN_SIZE_SAMPLES = 5


def estimate_min_size(doc: fitz.Document, start: int, end: int, original_size: int) -> int:
    """Estimate the smallest size the range can reach with the strongest automatic compression.

    Up to MIN_SIZE_SAMPLES evenly spaced pages are really compressed with the floor strategy and
    measured; the average per page is scaled to the whole range. The result is never larger
    than the original size (text pages may not shrink by rasterising, and the ladder keeps
    whatever is smallest).
    """
    from .compression_engine import FLOOR_STRATEGY, rasterize  # local import avoids a cycle

    n = end - start + 1
    k = min(MIN_SIZE_SAMPLES, n)
    pages = sorted({start - 1 + round(i * (n - 1) / max(k - 1, 1)) for i in range(k)})
    sample = fitz.open()
    try:
        for p in pages:
            sample.insert_pdf(doc, from_page=p, to_page=p)
        raster = rasterize(sample, FLOOR_STRATEGY.dpi, FLOOR_STRATEGY.quality, FLOOR_STRATEGY.grayscale)
        try:
            per_page = len(pdf_bytes(raster, optimize=True)) / len(pages)
        finally:
            raster.close()
    finally:
        sample.close()
    return min(original_size, int(per_page * n))


def measure_sections(doc: fitz.Document, sections: List[Section], cache: Dict[str, int]) -> List[Section]:
    out = []
    for s in sections:
        key = f"{s.start_page}-{s.end_page}"
        if key not in cache:
            cache[key] = measure_range_size(doc, s.start_page, s.end_page)
        min_key = "min:" + key
        if min_key not in cache:
            cache[min_key] = estimate_min_size(doc, s.start_page, s.end_page, cache[key])
        out.append(s.model_copy(update={"size_bytes": cache[key], "min_size_bytes": cache[min_key]}))
    return out


def page_weights(doc: fitz.Document) -> List[int]:
    """Rough relative weight of every page (content streams + images it uses).

    Only used to divide an *unprotected* section's measured size between its pages so that the
    planner may break such a section at a page boundary. Shared images count on every page that
    uses them, which is fine for a relative weighting.
    """
    weights: List[int] = []
    stream_len: Dict[int, int] = {}

    def length(xref: int) -> int:
        if xref not in stream_len:
            try:
                stream_len[xref] = len(doc.xref_stream_raw(xref) or b"")
            except Exception:  # noqa: BLE001
                stream_len[xref] = 0
        return stream_len[xref]

    for page in doc:
        w = 200  # page object overhead
        try:
            w += sum(length(x) for x in page.get_contents())
            w += sum(length(img[0]) for img in page.get_images(full=True))
        except Exception:  # noqa: BLE001
            pass
        weights.append(w)
    return weights


def find_section_for_page(sections: List[Section], page: int) -> Optional[Section]:
    for s in sections:
        if s.start_page <= page <= s.end_page:
            return s
    return None
