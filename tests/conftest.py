"""Shared fixtures: synthetic audit-report PDFs with controlled bookmarks and sizes."""
from __future__ import annotations

import io
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pymupdf as fitz  # PyMuPDF
from PIL import Image  # noqa: E402


def noise_jpeg(px: int, seed: int, quality: int = 92) -> bytes:
    """A random-noise image: practically incompressible, so it dominates the file size."""
    rnd = random.Random(seed)
    im = Image.frombytes("RGB", (px, px), rnd.randbytes(px * px * 3))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def make_pdf(path: Path, layout, image_px: int = 0, scanned: bool = False, bookmarks: bool = True) -> Path:
    """Create a PDF.

    ``layout`` is a list of (title, n_pages, level). A title of None adds pages without a bookmark;
    n_pages == 0 adds a bookmark pointing at the next page (a parent heading).
    ``image_px`` > 0 puts a noise image of that many pixels square on each page.
    ``scanned`` makes every page a single full-page image with no text layer.
    """
    doc = fitz.open()
    toc = []
    page_no = 0
    for title, n_pages, level in layout:
        if title is not None:
            toc.append([level, title, page_no + 1])
        for k in range(n_pages):
            page_no += 1
            page = doc.new_page(width=595, height=842)
            if scanned:
                page.insert_image(page.rect, stream=noise_jpeg(image_px or 600, seed=page_no))
                continue
            page.insert_text((72, 72), f"{title or 'Front'} - page {k + 1} (doc page {page_no})", fontsize=14)
            if image_px:
                page.insert_image(fitz.Rect(72, 100, 523, 551), stream=noise_jpeg(image_px, seed=page_no))
    if bookmarks:
        doc.set_toc(toc)
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def make(tmp_path):
    def _make(name="test.pdf", layout=None, **kw):
        return make_pdf(tmp_path / name, layout, **kw)

    return _make


@pytest.fixture
def ten_section_pdf(tmp_path):
    """Test 1: 100 pages, 10 bookmarked sections of 10 pages each."""
    layout = [("Main Report", 10, 1)] + [(f"Annexure {chr(65 + i)}", 10, 1) for i in range(9)]
    return make_pdf(tmp_path / "ten.pdf", layout, image_px=160)


@pytest.fixture
def nested_pdf(tmp_path):
    """Test 8: nested bookmarks (Audit Report > Main Report/Annexures > sub-items)."""
    layout = [
        (None, 2, 1),                 # cover pages without bookmark
        ("Audit Report", 0, 1),
        ("Main Report", 0, 2),
        ("Directors' Report", 3, 3),
        ("Financial Statements", 5, 3),
        ("Annexure A", 4, 2),
        ("Annexure B", 0, 2),
        ("Schedule B-1", 2, 3),
        ("Schedule B-2", 3, 3),
        ("Annexure C", 6, 2),
    ]
    return make_pdf(tmp_path / "nested.pdf", layout)
