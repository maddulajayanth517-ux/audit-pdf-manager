"""Bookmark reading, level suggestion and section detection (Tests 6 and 8)."""
import pymupdf as fitz  # PyMuPDF

from backend.app.models.pdf_models import Section
from backend.app.services.bookmark_analyzer import build_tree, level_stats, read_toc, suggest_level
from backend.app.services.section_analyzer import (
    SectionError,
    measure_sections,
    normalize_sections,
    sections_for_level,
)


def _toc(path):
    doc = fitz.open(str(path))
    entries, warnings = read_toc(doc)
    n = doc.page_count
    return doc, entries, warnings, n


def test_nested_hierarchy(nested_pdf):
    doc, entries, warnings, n = _toc(nested_pdf)
    assert n == 25
    tree = build_tree(entries)
    assert [t["title"] for t in tree] == ["Audit Report"]
    level2 = [c["title"] for c in tree[0]["children"]]
    assert level2 == ["Main Report", "Annexure A", "Annexure B", "Annexure C"]
    assert [c["title"] for c in tree[0]["children"][2]["children"]] == ["Schedule B-1", "Schedule B-2"]
    stats = {s["level"]: s for s in level_stats(entries)}
    assert stats[2]["keyword_hits"] == 3
    assert suggest_level(entries) == 2
    doc.close()


def test_sections_level2_never_split_annexures(nested_pdf):
    doc, entries, _, n = _toc(nested_pdf)
    sections, _ = sections_for_level(entries, n, 2)
    got = [(s.title, s.start_page, s.end_page) for s in sections]
    assert got == [
        ("Front pages (before the first bookmark)", 1, 2),
        ("Main Report", 3, 10),
        ("Annexure A", 11, 14),
        ("Annexure B", 15, 19),
        ("Annexure C", 20, 25),
    ]
    assert all(s.protected for s in sections)
    doc.close()


def test_sections_level3_uses_parents_without_children(nested_pdf):
    doc, entries, _, n = _toc(nested_pdf)
    sections, _ = sections_for_level(entries, n, 3)
    titles = [s.title for s in sections]
    # Annexure A / C have no level-3 children and must still be their own sections.
    assert titles == [
        "Front pages (before the first bookmark)", "Directors' Report", "Financial Statements",
        "Annexure A", "Schedule B-1", "Schedule B-2", "Annexure C",
    ]
    # Contiguous and complete
    assert sections[0].start_page == 1 and sections[-1].end_page == n
    for a, b in zip(sections, sections[1:]):
        assert b.start_page == a.end_page + 1
    doc.close()


def test_missing_destination_is_reported(tmp_path):
    doc = fitz.open()
    for _ in range(6):
        doc.new_page()
    doc.set_toc([[1, "Main", 1], [1, "Annexure A", 3], [1, "Annexure B", 5]])
    path = tmp_path / "x.pdf"
    doc.save(str(path))
    doc.close()
    doc = fitz.open(str(path))
    # Break one destination by pointing the outline item at nothing.
    xref = doc.get_outline_xrefs()[1]
    doc.xref_set_key(xref, "Dest", "null")
    doc.xref_set_key(xref, "A", "null")
    doc.saveIncr()
    entries, warnings = read_toc(doc)
    assert any("no valid page destination" in w for w in warnings)
    sections, _ = sections_for_level(entries, doc.page_count, 1)
    assert [(s.start_page, s.end_page) for s in sections] == [(1, 4), (5, 6)]
    doc.close()


def test_no_bookmarks(make):
    """Test 6: no bookmarks -> nothing pretends to know the Annexure boundaries."""
    path = make("nob.pdf", [("Main", 5, 1)], bookmarks=False)
    doc = fitz.open(str(path))
    entries, _ = read_toc(doc)
    assert entries == []
    assert suggest_level(entries) is None
    doc.close()


def test_manual_sections_fill_gaps_and_reject_overlap():
    secs = [Section(title="Annexure A", start_page=3, end_page=5), Section(title="Annexure B", start_page=8, end_page=10)]
    result, warnings = normalize_sections(secs, 12)
    assert [(s.start_page, s.end_page, s.protected) for s in result] == [
        (1, 2, False), (3, 5, True), (6, 7, False), (8, 10, True), (11, 12, False)]
    assert warnings
    try:
        normalize_sections([Section(title="A", start_page=1, end_page=5), Section(title="B", start_page=5, end_page=9)], 9)
        raise AssertionError("overlap not detected")
    except SectionError:
        pass
    try:
        normalize_sections([Section(title="A", start_page=1, end_page=50)], 9)
        raise AssertionError("out of range not detected")
    except SectionError:
        pass


def test_section_sizes_are_measured(ten_section_pdf):
    doc = fitz.open(str(ten_section_pdf))
    entries, _ = read_toc(doc)
    sections, _ = sections_for_level(entries, doc.page_count, 1)
    measured = measure_sections(doc, sections, {})
    assert len(measured) == 10
    assert all(s.size_bytes > 100_000 for s in measured)
    doc.close()
