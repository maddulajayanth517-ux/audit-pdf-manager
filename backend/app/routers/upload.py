"""POST /api/upload - receive one PDF into a private temporary folder."""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from ..config import settings
from ..models.pdf_models import DocumentInfo
from ..services.bookmark_analyzer import read_toc
from ..services.pdf_reader import MUPDF_LOCK, PdfProcessingError, open_pdf, save_decrypted_copy
from ..services.session_store import DocumentRecord, store
from ..utils.file_utils import free_disk_bytes, looks_like_pdf, remove_tree
from ..utils.size_utils import format_size

router = APIRouter(prefix="/api", tags=["upload"])
log = logging.getLogger("audit_pdf.upload")
CHUNK = 1024 * 1024


def _inspect(upload_path, source_path, password: str):
    """Open the PDF (in a worker thread), read its outline and decrypt it if needed."""
    with MUPDF_LOCK:
        doc = open_pdf(upload_path, password or None)
        try:
            warnings = []
            if getattr(doc, "is_repaired", False):
                warnings.append("The PDF had structural errors and was repaired while opening. "
                                "Please check the generated volumes carefully.")
            was_encrypted = bool(doc.is_encrypted or password)
            toc, toc_warnings = read_toc(doc)
            if was_encrypted:
                save_decrypted_copy(doc, source_path)
            return warnings, was_encrypted, doc.page_count, toc, toc_warnings
        finally:
            doc.close()


@router.post("/upload")
async def upload_pdf(file: UploadFile = File(...), password: str = Form("")):
    original_name = os.path.basename((file.filename or "").replace("\\", "/"))[:200]
    if not original_name.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files (.pdf) can be uploaded.")

    if free_disk_bytes(settings.temp_root) < 200_000_000:
        raise HTTPException(507, "Insufficient disk space on the server to accept the upload.")

    doc_id, doc_dir = store.new_document_dir()
    upload_path = doc_dir / "upload.pdf"
    source_path = doc_dir / "source.pdf"
    written = 0
    try:
        with open(upload_path, "wb") as out:
            first = True
            while True:
                chunk = await file.read(CHUNK)
                if not chunk:
                    break
                if first:
                    if not looks_like_pdf(chunk):
                        raise HTTPException(400, "The file is not a valid PDF (missing PDF header).")
                    first = False
                written += len(chunk)
                if written > settings.max_upload_bytes:
                    raise HTTPException(
                        413, f"The file is larger than the maximum upload size of {format_size(settings.max_upload_bytes)}."
                    )
                out.write(chunk)
        if written == 0:
            raise HTTPException(400, "The uploaded file is empty.")

        try:
            warnings, was_encrypted, page_count, toc, toc_warnings = await run_in_threadpool(
                _inspect, upload_path, source_path, password
            )
        except PdfProcessingError as exc:
            status = 401 if exc.code in ("password_required", "password_incorrect") else 400
            raise HTTPException(status, {"code": exc.code, "message": exc.message})
        if was_encrypted:
            upload_path.unlink(missing_ok=True)
        else:
            upload_path.replace(source_path)
    except HTTPException:
        remove_tree(doc_dir)
        raise
    except MemoryError:
        remove_tree(doc_dir)
        raise HTTPException(507, "Not enough memory to open this PDF.")
    except Exception:  # noqa: BLE001
        remove_tree(doc_dir)
        log.exception("Upload failed for document %s", doc_id)
        raise HTTPException(500, "The PDF could not be processed. It may be damaged or use an unsupported structure.")
    finally:
        await file.close()

    rec = DocumentRecord(
        id=doc_id,
        dir=doc_dir,
        source_path=source_path,
        filename=original_name,
        size_bytes=written,
        page_count=page_count,
        was_encrypted=was_encrypted,
        toc=toc,
        toc_warnings=toc_warnings,
        open_warnings=warnings,
    )
    store.add_document(rec)
    log.info("Accepted upload %s: %d pages, %d bytes", doc_id, page_count, written)
    info = DocumentInfo(
        document_id=doc_id,
        filename=original_name,
        size_bytes=written,
        page_count=page_count,
        bookmark_count=len(toc),
        was_encrypted=was_encrypted,
    )
    return {"document": info, "warnings": warnings}
