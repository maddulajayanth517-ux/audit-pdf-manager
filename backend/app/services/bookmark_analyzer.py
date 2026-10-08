"""Reading the PDF outline (bookmarks) and suggesting which level holds the Annexures."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import List, Optional

import pymupdf as fitz  # PyMuPDF

ANNEXURE_RE = re.compile(
    r"\b(annex(ure)?s?|appendi(x|ces)|schedules?|exhibits?|attachments?|enclosures?|encl\.?)\b",
    re.IGNORECASE,
)


@dataclass
class TocEntry:
    id: int
    level: int
    title: str
    page: Optional[int]  # 1-based, None if the destination is missing / invalid
    parent: Optional[int]  # id of the parent entry


def read_toc(doc: fitz.Document) -> tuple[List[TocEntry], List[str]]:
    """Return the outline as a flat list in document (outline) order, plus warnings."""
    warnings: List[str] = []
    try:
        raw = doc.get_toc(simple=False)
    except Exception:  # noqa: BLE001
        return [], ["The bookmark structure of this PDF could not be read."]

    entries: List[TocEntry] = []
    stack: List[TocEntry] = []  # current ancestor chain
    missing = 0
    fixed_levels = 0
    for idx, item in enumerate(raw):
        level, title, page = int(item[0]), str(item[1] or "").strip(), item[2]
        # Normalise invalid hierarchies (e.g. a jump from level 1 to level 3).
        max_allowed = (stack[-1].level + 1) if stack else 1
        if level > max_allowed:
            level = max_allowed
            fixed_levels += 1
        level = max(level, 1)
        while stack and stack[-1].level >= level:
            stack.pop()
        valid_page = page if isinstance(page, int) and 1 <= page <= doc.page_count else None
        if valid_page is None:
            missing += 1
        entry = TocEntry(
            id=idx,
            level=level,
            title=title or f"(untitled bookmark {idx + 1})",
            page=valid_page,
            parent=stack[-1].id if stack else None,
        )
        entries.append(entry)
        stack.append(entry)

    if missing:
        warnings.append(
            f"{missing} bookmark(s) have no valid page destination and were ignored for section detection."
        )
    if fixed_levels:
        warnings.append(f"{fixed_levels} bookmark(s) had an invalid nesting level and were corrected.")
    return entries, warnings


def build_tree(entries: List[TocEntry]) -> list[dict]:
    nodes = {e.id: {**asdict(e), "children": []} for e in entries}
    roots = []
    for e in entries:
        node = nodes[e.id]
        node.pop("parent", None)
        if e.parent is None:
            roots.append(node)
        else:
            nodes[e.parent]["children"].append(node)
    return roots


def level_stats(entries: List[TocEntry]) -> list[dict]:
    stats: dict[int, dict] = {}
    for e in entries:
        s = stats.setdefault(e.level, {"level": e.level, "count": 0, "valid": 0, "keyword_hits": 0})
        s["count"] += 1
        if e.page is not None:
            s["valid"] += 1
            if ANNEXURE_RE.search(e.title):
                s["keyword_hits"] += 1
    return [stats[k] for k in sorted(stats)]


def suggest_level(entries: List[TocEntry]) -> Optional[int]:
    """Pick the bookmark level that most likely represents major sections / Annexures.

    Preference: the level with the most Annexure-like titles (ties -> shallower level);
    otherwise the shallowest level that has at least two usable bookmarks.
    """
    stats = level_stats(entries)
    candidates = [s for s in stats if s["valid"] >= 2]
    if not candidates:
        usable = [s for s in stats if s["valid"] >= 1]
        return usable[0]["level"] if usable else None
    best_kw = max(candidates, key=lambda s: (s["keyword_hits"], -s["level"]))
    if best_kw["keyword_hits"] >= 2:
        return best_kw["level"]
    return candidates[0]["level"]


def ancestors(entries_by_id: dict[int, TocEntry], entry: TocEntry) -> List[TocEntry]:
    chain = []
    parent = entry.parent
    while parent is not None:
        p = entries_by_id[parent]
        chain.append(p)
        parent = p.parent
    return list(reversed(chain))
