"""Opening PDFs safely with PyMuPDF.

PyMuPDF must not be used by several threads at the same time, so all PDF work done inside
the web-server process is wrapped in ``MUPDF_LOCK``. Heavy generation work runs in separate
worker processes and does not need the lock.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pymupdf as fitz  # PyMuPDF

MUPDF_LOCK = threading.RLock()

# Keep MuPDF from printing warnings that could contain document text to the console/logs.
try:
    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)
except Exception:  # pragma: no cover - older PyMuPDF
    pass


class PdfProcessingError(Exception):
    """An error that is safe to show to the user."""

    def __init__(self, message: str, code: str = "pdf_error"):
        super().__init__(message)
        self.message = message
        self.code = code


def open_pdf(path: str | Path, password: str | None = None) -> fitz.Document:
    try:
        doc = fitz.open(str(path), filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - MuPDF raises several types
        raise PdfProcessingError(
            "The file could not be opened as a PDF. It may be corrupted or not a real PDF.",
            "corrupted",
        ) from exc
    if not doc.is_pdf:
        doc.close()
        raise PdfProcessingError("The uploaded file is not a PDF document.", "not_pdf")
    if doc.needs_pass:
        if not password:
            doc.close()
            raise PdfProcessingError(
                "This PDF is password-protected. Enter the document password to continue.",
                "password_required",
            )
        if not doc.authenticate(password):
            doc.close()
            raise PdfProcessingError("The password is incorrect for this PDF.", "password_incorrect")
    if doc.page_count == 0:
        doc.close()
        raise PdfProcessingError("The PDF contains no pages.", "empty")
    return doc


def save_decrypted_copy(doc: fitz.Document, target: Path) -> None:
    """Write an unencrypted copy so later steps do not need the password."""
    doc.save(str(target), encryption=fitz.PDF_ENCRYPT_NONE, garbage=1)


def save_kwargs(optimize: bool) -> dict:
    """Arguments for Document.save(); newer PyMuPDF options are added only when supported."""
    kwargs: dict = {"garbage": 4 if optimize else 3, "deflate": True}
    if optimize:
        kwargs.update(deflate_images=True, deflate_fonts=True, use_objstms=1)
    return kwargs


def save_pdf(doc: fitz.Document, path: str | Path, optimize: bool = False) -> None:
    kwargs = save_kwargs(optimize)
    try:
        doc.save(str(path), **kwargs)
    except TypeError:
        kwargs.pop("use_objstms", None)
        doc.save(str(path), **kwargs)


def pdf_bytes(doc: fitz.Document, optimize: bool = False) -> bytes:
    kwargs = save_kwargs(optimize)
    try:
        return doc.tobytes(**kwargs)
    except TypeError:
        kwargs.pop("use_objstms", None)
        return doc.tobytes(**kwargs)
