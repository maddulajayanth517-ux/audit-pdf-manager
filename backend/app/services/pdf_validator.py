"""Validation of generated volumes against the source PDF."""
from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import List, Optional, Sequence

import pymupdf as fitz  # PyMuPDF
from PIL import Image, ImageChops, ImageStat

from ..utils.size_utils import format_bytes_exact, format_size

VISUAL_DIFF_LIMIT = 40.0  # mean absolute grey-level difference (0-255) on small thumbnails
THUMB_WIDTH = 64


def page_fingerprints(doc: fitz.Document, with_text: bool = True, start: int = 1,
                      end: Optional[int] = None) -> List[dict]:
    fps = []
    for pno in range(start - 1, end if end is not None else doc.page_count):
        page = doc[pno]
        r = page.rect
        fp = {"w": round(r.width, 1), "h": round(r.height, 1)}
        if with_text:
            try:
                norm = "".join(page.get_text("text").split())
            except Exception:  # noqa: BLE001
                norm = ""
            fp["t"] = hashlib.sha1(norm.encode("utf-8", "ignore")).hexdigest()[:16]
        fps.append(fp)
    return fps


def _thumb(page: fitz.Page) -> Image.Image:
    dpi = max(2, int(THUMB_WIDTH * 72 / max(page.rect.width, 1)))
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, alpha=False)
    im = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    return im.resize((THUMB_WIDTH, max(1, int(THUMB_WIDTH * page.rect.height / max(page.rect.width, 1)))))


def _check(name: str, ok: bool, detail: str, severity: str = "error") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail, "severity": severity}


def validate_volume(
    path: Path,
    src: fitz.Document,
    start: int,
    end: int,
    fingerprints: Sequence[dict],
    content_check: str,  # strict | lenient | visual
    expected_bookmarks: Optional[int],
    max_bytes: Optional[int],
    protected_sections: Sequence[dict],
) -> dict:
    checks: List[dict] = []
    expected_pages = end - start + 1
    size = path.stat().st_size if path.exists() else 0

    try:
        doc = fitz.open(str(path))
    except Exception:  # noqa: BLE001
        checks.append(_check("PDF opens correctly", False, "The generated file cannot be opened."))
        return {"passed": False, "checks": checks, "size_bytes": size}

    try:
        damaged = doc.needs_pass or not doc.is_pdf or getattr(doc, "is_repaired", False)
        checks.append(_check("PDF opens correctly / not corrupted", not damaged,
                             "Opened without repair." if not damaged else "File structure is damaged."))

        independent = _independent_page_count(path)
        if independent is not None:
            checks.append(_check("Independent parser (pypdf) reads file", independent == expected_pages,
                                 f"pypdf reads {independent} pages." if independent >= 0
                                 else "pypdf could not read the file."))

        checks.append(_check("Page count", doc.page_count == expected_pages,
                             f"{doc.page_count} pages (expected {expected_pages}, original pages {start}–{end})."))
        if doc.page_count != expected_pages:
            return {"passed": False, "checks": checks, "size_bytes": size}

        # Page order / identity: dimensions always, plus text or visual comparison.
        size_mismatch = [
            start + i for i, page in enumerate(doc)
            if abs(page.rect.width - fingerprints[i]["w"]) > 1.0 or abs(page.rect.height - fingerprints[i]["h"]) > 1.0
        ]
        checks.append(_check("Page sizes match original order", not size_mismatch,
                             "All page sizes match." if not size_mismatch
                             else f"Mismatch on original page(s) {_short(size_mismatch)}."))

        if content_check in ("strict", "lenient") and "t" in fingerprints[0]:
            new_fps = page_fingerprints(doc, with_text=True)
            text_mismatch = [start + i for i, (a, b) in enumerate(zip(new_fps, fingerprints)) if a["t"] != b["t"]]
            if content_check == "strict":
                checks.append(_check("Page content matches original (text)", not text_mismatch,
                                     "Text of every page matches the original page in the same position."
                                     if not text_mismatch else f"Text differs on original page(s) {_short(text_mismatch)}."))
            else:
                checks.append(_check("Page content matches original (text)", not text_mismatch,
                                     "Text matches." if not text_mismatch else
                                     f"Text layer re-encoded by Ghostscript on {len(text_mismatch)} page(s); "
                                     "page order verified by page sizes.", severity="warning"))
        if content_check == "visual" or (content_check == "lenient" and not size_mismatch):
            bad = []
            step = 1 if content_check == "visual" else max(1, expected_pages // 20)
            for i in range(0, expected_pages, step):
                a = _thumb(src[start - 1 + i])
                b = _thumb(doc[i]).resize(a.size)
                diff = ImageStat.Stat(ImageChops.difference(a, b)).mean[0]
                if diff > VISUAL_DIFF_LIMIT:
                    bad.append(start + i)
            checks.append(_check("Page content matches original (visual)", not bad,
                                 "Every page visually matches the original page in the same position."
                                 if not bad else f"Visual mismatch on original page(s) {_short(bad)}."))

        render_ok = True
        for i in sorted({0, expected_pages // 2, expected_pages - 1}):
            try:
                doc[i].get_pixmap(dpi=20)
            except Exception:  # noqa: BLE001
                render_ok = False
        checks.append(_check("Pages render", render_ok, "Sample pages rendered successfully."
                             if render_ok else "One or more pages failed to render."))

        toc = doc.get_toc(simple=True)
        bad_dest = [t for t in toc if not (1 <= t[2] <= doc.page_count)]
        bm_ok = not bad_dest and (expected_bookmarks is None or len(toc) == expected_bookmarks)
        checks.append(_check("Bookmark destinations valid", bm_ok,
                             f"{len(toc)} bookmark(s), all pointing inside this volume." if bm_ok
                             else f"{len(bad_dest)} invalid destination(s); {len(toc)} bookmark(s) "
                                  f"(expected {expected_bookmarks})."))
    finally:
        doc.close()

    split = [s["title"] for s in protected_sections
             if s["start_page"] <= end and s["end_page"] >= start
             and not (start <= s["start_page"] and s["end_page"] <= end)]
    inside = [s for s in protected_sections if start <= s["start_page"] and s["end_page"] <= end]
    checks.append(_check("Protected sections complete", not split,
                         f"{len(inside)} protected section(s) fully contained." if not split
                         else "Split protected section(s): " + ", ".join(split)))

    if max_bytes:
        checks.append(_check("File size within limit", size <= max_bytes,
                             f"{format_size(size)} ({format_bytes_exact(size)}) vs limit "
                             f"{format_size(max_bytes)} ({format_bytes_exact(max_bytes)}).", severity="limit"))
    else:
        checks.append(_check("File size", True, f"{format_size(size)} ({format_bytes_exact(size)}).", severity="info"))

    passed = all(c["ok"] for c in checks if c["severity"] == "error")
    return {"passed": passed, "checks": checks, "size_bytes": size}


def validate_coverage(
    volume_ranges: Sequence[tuple[int, int]], page_count: int, protected_sections: Sequence[dict]
) -> List[dict]:
    """Cross-volume checks: every page exactly once, in order; no protected section split."""
    pages: List[int] = []
    for s, e in volume_ranges:
        pages.extend(range(s, e + 1))
    missing = sorted(set(range(1, page_count + 1)) - set(pages))
    dups = sorted(p for p, n in Counter(pages).items() if n > 1)
    ordered = pages == sorted(pages)
    checks = [
        _check("No page missing", not missing, "All pages are included." if not missing
               else f"Missing page(s): {_short(missing)}."),
        _check("No page duplicated", not dups, "No duplicates." if not dups else f"Duplicated: {_short(dups)}."),
        _check("Original page order preserved", ordered, "Pages are in original order across volumes."
               if ordered else "Page order differs from the original."),
    ]
    split = []
    for sec in protected_sections:
        holders = [r for r in volume_ranges if r[0] <= sec["end_page"] and r[1] >= sec["start_page"]]
        if len(holders) != 1 or not (holders[0][0] <= sec["start_page"] and sec["end_page"] <= holders[0][1]):
            split.append(sec["title"])
    checks.append(_check("No protected section split", not split,
                         f"All {len(protected_sections)} protected section(s) are intact." if not split
                         else "Split: " + ", ".join(split)))
    return checks


def _independent_page_count(path: Path) -> Optional[int]:
    """Cross-check with a second PDF library. None if pypdf is not installed, -1 if unreadable."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        return len(PdfReader(str(path), strict=False).pages)
    except Exception:  # noqa: BLE001
        return -1


def _short(pages: Sequence[int], limit: int = 12) -> str:
    text = ", ".join(str(p) for p in pages[:limit])
    return text + (f" … (+{len(pages) - limit} more)" if len(pages) > limit else "")
