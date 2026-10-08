"""POST /api/analyze - bookmark tree, level statistics and protected sections with sizes."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..models.pdf_models import AnalysisResponse, AnalyzeRequest, DocumentInfo, Section
from ..services.bookmark_analyzer import build_tree, level_stats, suggest_level
from ..services.pdf_reader import MUPDF_LOCK, open_pdf
from ..services.section_analyzer import (
    SectionError,
    measure_sections,
    normalize_sections,
    sections_for_level,
    whole_document_section,
)
from ..services.session_store import DocumentRecord, NotFound, store

router = APIRouter(prefix="/api", tags=["analysis"])

NO_BOOKMARKS = ("No usable bookmarks were detected. Automatic Annexure-safe splitting cannot be guaranteed. "
                "Define protected page ranges manually below if the document has Annexures.")


def get_doc_or_404(doc_id: str) -> DocumentRecord:
    try:
        return store.get_document(doc_id)
    except NotFound:
        raise HTTPException(404, "Document not found. Uploaded files are deleted automatically after a period "
                                 "of inactivity — please upload the PDF again.")


def doc_info(rec: DocumentRecord) -> DocumentInfo:
    return DocumentInfo(
        document_id=rec.id, filename=rec.filename, size_bytes=rec.size_bytes,
        page_count=rec.page_count, bookmark_count=len(rec.toc), was_encrypted=rec.was_encrypted,
    )


@router.post("/analyze", response_model=AnalysisResponse)
def analyze(req: AnalyzeRequest):
    rec = get_doc_or_404(req.document_id)
    warnings = list(rec.open_warnings) + list(rec.toc_warnings)
    usable = [e for e in rec.toc if e.page is not None]
    suggested = suggest_level(rec.toc)
    selected = req.level if req.level is not None else suggested

    sections: List[Section] = []
    if usable and selected is not None:
        sections, level_warnings = sections_for_level(rec.toc, rec.page_count, selected)
        warnings.extend(level_warnings)
    has_usable = bool(sections)
    if not has_usable:
        warnings.insert(0, NO_BOOKMARKS)
        sections = [whole_document_section(rec.page_count)]

    with MUPDF_LOCK:
        doc = open_pdf(rec.source_path)
        try:
            sections = measure_sections(doc, sections, rec.size_cache)
        finally:
            doc.close()

    return AnalysisResponse(
        document=doc_info(rec),
        bookmarks=build_tree(rec.toc),
        levels=level_stats(rec.toc),
        suggested_level=suggested,
        selected_level=selected if has_usable else None,
        sections=sections,
        warnings=warnings,
        has_usable_bookmarks=has_usable,
    )


class MeasureRequest(BaseModel):
    document_id: str
    sections: List[Section]


@router.post("/sections/measure")
def measure(req: MeasureRequest):
    """Validate manually defined sections (fill gaps, reject overlaps) and measure their sizes."""
    rec = get_doc_or_404(req.document_id)
    try:
        sections, warnings = normalize_sections(req.sections, rec.page_count)
    except SectionError as exc:
        raise HTTPException(422, str(exc))
    with MUPDF_LOCK:
        doc = open_pdf(rec.source_path)
        try:
            sections = measure_sections(doc, sections, rec.size_cache)
        finally:
            doc.close()
    return {"sections": sections, "warnings": warnings}
