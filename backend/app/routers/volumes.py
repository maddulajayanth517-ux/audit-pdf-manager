"""Volume planning, generation jobs, progress and conflict resolution."""
from __future__ import annotations

import copy
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..models.volume_models import GenerateRequest, PlanRequest, ResolveRequest, VolumePlan
from ..services.compression_engine import find_ghostscript
from ..services.generation_service import StatusWriter, _validate, summarize
from ..services.job_manager import manager
from ..services.pdf_reader import MUPDF_LOCK, open_pdf
from ..services.pdf_validator import validate_coverage
from ..services.section_analyzer import SectionError, measure_sections, normalize_sections, page_weights
from ..services.session_store import JobRecord, NotFound, store
from ..services.volume_planner import PlanningError, plan_volumes
from ..utils.file_utils import ensure_dir, free_disk_bytes, new_id, read_json, safe_name_part, write_json_atomic
from ..utils.size_utils import format_size, to_bytes
from .analysis import get_doc_or_404

router = APIRouter(prefix="/api", tags=["volumes"])


def get_job_or_404(job_id: str) -> JobRecord:
    try:
        return store.get_job(job_id)
    except NotFound:
        raise HTTPException(404, "Job not found. Files are deleted automatically after a period of inactivity.")


@router.post("/plan", response_model=VolumePlan)
def plan(req: PlanRequest):
    rec = get_doc_or_404(req.document_id)
    try:
        sections, warnings = normalize_sections(req.sections, rec.page_count)
    except SectionError as exc:
        raise HTTPException(422, str(exc))
    max_bytes = to_bytes(req.max_size, req.size_unit) if req.mode in ("max_size", "both") else None
    with MUPDF_LOCK:
        doc = open_pdf(rec.source_path)
        try:
            sections = measure_sections(doc, sections, rec.size_cache)
            if any(not s.protected and s.page_count > 1 for s in sections) and rec.weights is None:
                rec.weights = page_weights(doc)
        finally:
            doc.close()
    if req.compression is not None and not req.compression.reaches_floor():
        # The estimate assumes the strongest automatic compression; with these settings it would
        # promise sizes that cannot be guaranteed, so moves are not size-checked in advance.
        sections = [s.model_copy(update={"min_size_bytes": None}) for s in sections]
        warnings.append(
            "The selected compression option does not allow the strongest compression, so the smallest reachable "
            "size of each volume cannot be checked in advance. Real sizes are checked during generation."
        )
    try:
        result = plan_volumes(
            sections, req.mode, max_bytes, req.num_volumes if req.mode != "max_size" else None,
            weights=rec.weights, plan_id=new_id(), document_id=rec.id,
            volume_starts=req.volume_starts,
        )
    except PlanningError as exc:
        raise HTTPException(422, str(exc))
    result.warnings = warnings + result.warnings
    store.add_plan(rec, result.model_dump())
    return result


@router.post("/generate")
def generate(req: GenerateRequest):
    try:
        rec, plan_data = store.get_plan(req.plan_id)
    except NotFound:
        raise HTTPException(404, "Plan not found. Please plan the volumes again.")
    needed = rec.size_bytes * settings.disk_space_factor + 50_000_000
    if free_disk_bytes(settings.temp_root) < needed:
        raise HTTPException(507, f"Insufficient disk space: about {format_size(needed)} of free space is required.")

    client = safe_name_part(req.client_name or rec.filename, default="Client")
    width = 3 if len(plan_data["volumes"]) > 99 else 2
    volumes = [
        {
            "index": v["index"],
            "start_page": v["start_page"],
            "end_page": v["end_page"],
            "sections": v["sections"],
            "filename": f"{client}_Audit_Report_Volume_{v['index']:0{width}d}.pdf",
        }
        for v in plan_data["volumes"]
    ]
    gs_path, _ = find_ghostscript(settings.ghostscript_path)
    job_id = new_id()
    job_dir = ensure_dir(rec.dir / "jobs" / job_id)
    spec = {
        "job_id": job_id,
        "document_id": rec.id,
        "source_path": str(rec.source_path),
        "source_size": rec.size_bytes,
        "page_count": rec.page_count,
        "client": client,
        "zip_name": f"{client}_Audit_Report_Volumes.zip",
        "preserve_bookmarks": req.preserve_bookmarks,
        "max_bytes": plan_data["max_bytes"],
        "num_volumes_requested": plan_data["num_volumes_requested"],
        "mode": plan_data["mode"],
        "compression": req.compression.model_dump(),
        "gs_path": gs_path if req.compression.use_ghostscript else None,
        "gs_timeout": settings.ghostscript_timeout_s,
        "workers": settings.workers_per_job,
        "toc": [[e.level, e.title, e.page, e.parent] for e in rec.toc],
        "sections": [
            {"title": s["title"], "start_page": s["start_page"], "end_page": s["end_page"], "protected": s["protected"]}
            for s in plan_data["sections"]
        ],
        "volumes": volumes,
        "plan_warnings": plan_data["warnings"],
    }
    write_json_atomic(job_dir / "job.json", spec)
    write_json_atomic(job_dir / "status.json", {
        "state": "queued", "progress": 0.0, "events": [], "volumes": [], "summary": None, "error": None,
    })
    store.add_job(JobRecord(id=job_id, document_id=rec.id, dir=job_dir, client_name=client))
    manager.submit(job_id, job_dir, "generate")
    return {"job_id": job_id}


def public_status(job: JobRecord) -> dict:
    status = read_json(job.dir / "status.json", default=None)
    if status is None:
        raise HTTPException(404, "Job status not available.")
    spec = read_json(job.dir / "job.json", default={}) or {}
    status = copy.deepcopy(status)
    status.pop("debug", None)
    for v in status.get("volumes", []):
        cands = v.pop("candidates", None) or {}
        v["smallest_bytes"] = (cands.get("smallest") or {}).get("size")
        v["original_quality_bytes"] = (cands.get("lossless") or {}).get("size")
        v["volume_id"] = f"{job.id}-{v['index']}"
    status["job_id"] = job.id
    status["active"] = manager.is_active(job.id)
    status["max_bytes"] = spec.get("max_bytes")
    status["zip_name"] = spec.get("zip_name")
    status["plan_warnings"] = spec.get("plan_warnings", [])
    return status


@router.get("/status/{job_id}")
def job_status(job_id: str):
    return public_status(get_job_or_404(job_id))


@router.post("/cancel/{job_id}")
def cancel(job_id: str):
    job = get_job_or_404(job_id)
    if not manager.cancel(job_id):
        raise HTTPException(409, "This job is not running.")
    return {"cancelled": True}


@router.post("/jobs/{job_id}/volumes/{index}/resolve")
def resolve(job_id: str, index: int, req: ResolveRequest):
    """User decision for a volume that is still over the limit after automatic compression."""
    job = get_job_or_404(job_id)
    if manager.is_active(job_id):
        raise HTTPException(409, "The job is still processing.")
    writer = StatusWriter(job.dir)
    vol = next((v for v in writer.data.get("volumes", []) if v["index"] == index), None)
    if vol is None:
        raise HTTPException(404, "Volume not found.")
    if vol.get("status") not in ("OVER_LIMIT", "ACCEPTED_OVER_LIMIT"):
        raise HTTPException(409, "This volume does not need a decision.")

    if req.action == "stronger":
        writer.set(state="queued")
        manager.submit(job_id, job.dir, "stronger", index)
        return public_status(job)

    key = "smallest" if req.action == "accept_smallest" else "lossless"
    cand = (vol.get("candidates") or {}).get(key)
    if not cand or not Path(cand["path"]).exists():
        raise HTTPException(409, "That version of the volume is no longer available. Please generate again.")
    spec = read_json(job.dir / "job.json")
    fps = read_json(job.dir / "fingerprints.json")
    shutil.copyfile(cand["path"], job.dir / "out" / vol["filename"])
    kind = (cand.get("strategy") or {}).get("kind", "plain")
    with MUPDF_LOCK:
        src = open_pdf(spec["source_path"])
        try:
            validation = _validate(job.dir, spec, vol, src, fps, kind)
        finally:
            src.close()
    size = (job.dir / "out" / vol["filename"]).stat().st_size
    vol.update(
        size_bytes=size, compression_level=cand["level"], compression_label=cand["label"],
        text_preserved=cand["preserves_text"], extreme=cand["level"] >= 5, strategy_kind=kind,
        validation=validation,
        status="ACCEPTED_OVER_LIMIT" if validation["passed"] else "FAIL",
    )
    label = "smallest achieved version" if key == "smallest" else "original-quality version"
    writer.event("warn", f"Volume {index}: user allowed the volume to exceed the limit ({label}, {format_size(size)}).")
    coverage = validate_coverage(
        [(v["start_page"], v["end_page"]) for v in writer.data["volumes"]], spec["page_count"],
        [x for x in spec["sections"] if x["protected"]],
    )
    writer.set(summary=summarize(spec, writer.data["volumes"], coverage))
    return public_status(job)
