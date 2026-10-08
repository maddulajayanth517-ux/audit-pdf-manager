"""In-memory registry of uploaded documents, plans and jobs, with automatic cleanup.

Uploaded files live only in a random temporary folder per document. They are deleted when the
user clicks "Finish & delete", when they have not been used for FILE_TTL_MINUTES, and when the
server starts or stops.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..config import settings
from ..utils.file_utils import ensure_dir, is_valid_id, new_id, purge_dir_contents, remove_tree
from .bookmark_analyzer import TocEntry

log = logging.getLogger("audit_pdf.store")


@dataclass
class DocumentRecord:
    id: str
    dir: Path
    source_path: Path
    filename: str
    size_bytes: int
    page_count: int
    was_encrypted: bool
    toc: List[TocEntry]
    toc_warnings: List[str]
    open_warnings: List[str] = field(default_factory=list)
    weights: Optional[List[int]] = None
    size_cache: Dict[str, int] = field(default_factory=dict)
    plans: Dict[str, dict] = field(default_factory=dict)
    jobs: List[str] = field(default_factory=list)
    last_access: float = field(default_factory=time.time)


@dataclass
class JobRecord:
    id: str
    document_id: str
    dir: Path
    client_name: str
    created: float = field(default_factory=time.time)


class NotFound(KeyError):
    pass


class SessionStore:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()
        self.docs: Dict[str, DocumentRecord] = {}
        self.jobs: Dict[str, JobRecord] = {}
        self.plan_index: Dict[str, str] = {}  # plan_id -> document_id

    # ---- lifecycle
    def startup(self) -> None:
        ensure_dir(self.root)
        if settings.purge_on_startup:
            purge_dir_contents(self.root)

    def shutdown(self) -> None:
        with self.lock:
            for doc_id in list(self.docs):
                self._delete_locked(doc_id)
        purge_dir_contents(self.root)

    # ---- documents
    def new_document_dir(self) -> tuple[str, Path]:
        doc_id = new_id()
        return doc_id, ensure_dir(self.root / doc_id)

    def add_document(self, rec: DocumentRecord) -> None:
        with self.lock:
            self.docs[rec.id] = rec

    def get_document(self, doc_id: str) -> DocumentRecord:
        if not is_valid_id(doc_id):
            raise NotFound(doc_id)
        with self.lock:
            rec = self.docs.get(doc_id)
            if rec is None:
                raise NotFound(doc_id)
            rec.last_access = time.time()
            return rec

    def delete_document(self, doc_id: str) -> bool:
        with self.lock:
            return self._delete_locked(doc_id)

    def _delete_locked(self, doc_id: str) -> bool:
        rec = self.docs.pop(doc_id, None)
        if rec is None:
            return False
        for job_id in rec.jobs:
            self.jobs.pop(job_id, None)
        for plan_id in rec.plans:
            self.plan_index.pop(plan_id, None)
        remove_tree(rec.dir)
        log.info("Deleted temporary files for document %s", doc_id)
        return True

    # ---- plans
    def add_plan(self, doc: DocumentRecord, plan: dict) -> None:
        with self.lock:
            doc.plans[plan["plan_id"]] = plan
            self.plan_index[plan["plan_id"]] = doc.id

    def get_plan(self, plan_id: str) -> tuple[DocumentRecord, dict]:
        if not is_valid_id(plan_id):
            raise NotFound(plan_id)
        with self.lock:
            doc_id = self.plan_index.get(plan_id)
            if doc_id is None:
                raise NotFound(plan_id)
            doc = self.get_document(doc_id)
            return doc, doc.plans[plan_id]

    # ---- jobs
    def add_job(self, job: JobRecord) -> None:
        with self.lock:
            self.jobs[job.id] = job
            self.docs[job.document_id].jobs.append(job.id)

    def get_job(self, job_id: str) -> JobRecord:
        if not is_valid_id(job_id):
            raise NotFound(job_id)
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise NotFound(job_id)
            doc = self.docs.get(job.document_id)
            if doc:
                doc.last_access = time.time()
            return job

    # ---- cleanup
    def cleanup_expired(self, is_job_active) -> int:
        cutoff = time.time() - settings.file_ttl_minutes * 60
        removed = 0
        with self.lock:
            for doc_id, rec in list(self.docs.items()):
                if rec.last_access < cutoff and not any(is_job_active(j) for j in rec.jobs):
                    self._delete_locked(doc_id)
                    removed += 1
            # Folders left behind (e.g. a file was locked when its session was deleted).
            if self.root.exists():
                for child in self.root.iterdir():
                    if child.is_dir() and child.name not in self.docs and child.stat().st_mtime < cutoff:
                        remove_tree(child)
        return removed


store = SessionStore(settings.temp_root)
