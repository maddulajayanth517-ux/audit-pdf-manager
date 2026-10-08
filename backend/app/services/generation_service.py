"""Volume generation pipeline: split -> measure -> compress -> validate.

Runs inside a separate worker process (see job_manager.py) so that a very large PDF can never
freeze or crash the web server. Progress is written to ``status.json`` in the job folder; a
``cancel.flag`` file in the same folder requests cancellation.

Job folder layout (all names fixed or random - never user supplied):
    job.json          the job specification written by the server
    status.json       progress / results, read by the server
    fingerprints.json per-page fingerprints of the source used for validation
    out/              final volume PDFs
    work/             compression attempts kept as candidates
"""
from __future__ import annotations

import multiprocessing as mp
import os
import shutil
import threading
import time
import traceback
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typing import Callable, List, Optional

import pymupdf as fitz  # PyMuPDF

from ..utils.file_utils import ensure_dir, free_disk_bytes, read_json, remove_tree, write_json_atomic
from ..utils.size_utils import format_bytes_exact, format_size
from .compression_engine import (
    CancelledError,
    compress_volume,
    stronger_ladder,
    worker_fingerprints,
    worker_validate,
)
from .pdf_reader import PdfProcessingError
from .pdf_splitter import volume_toc
from .pdf_validator import page_fingerprints, validate_coverage, validate_volume

EXTREME_LEVEL = 5


class StatusWriter:
    def __init__(self, job_dir: Path):
        self.lock = threading.RLock()
        self.path = job_dir / "status.json"
        self.data = read_json(self.path, default=None) or {
            "state": "queued", "progress": 0.0, "events": [], "volumes": [], "summary": None, "error": None,
        }
        self._last_write = 0.0

    def save(self, force: bool = True) -> None:
        with self.lock:
            now = time.time()
            if force or now - self._last_write > 0.3:
                write_json_atomic(self.path, self.data)
                self._last_write = now

    def set(self, **kwargs) -> None:
        with self.lock:
            self.data.update(kwargs)
            self.save()

    def event(self, level: str, msg: str) -> None:
        with self.lock:
            self.data["events"].append({"t": time.time(), "level": level, "msg": msg})
            self.save(force=level != "info")

    def progress(self, value: float) -> None:
        with self.lock:
            self.data["progress"] = round(max(0.0, min(1.0, value)), 3)
            self.save(force=False)


def content_check_for(strategy_kind: Optional[str]) -> str:
    if strategy_kind == "raster":
        return "visual"
    if strategy_kind == "ghostscript":
        return "lenient"
    return "strict"


def _check_disk(job_dir: Path, needed: int) -> None:
    free = free_disk_bytes(job_dir)
    if free < needed:
        raise PdfProcessingError(
            f"Insufficient disk space: {format_size(free)} free, about {format_size(needed)} needed.",
            "disk_space",
        )


def _bake_form_fields(src: fitz.Document, job_dir: Path, spec: dict, status: "StatusWriter") -> fitz.Document:
    """Turn form fields (typically digital-signature stamps) into static page content.

    Copying pages between documents does not reliably carry form fields (signature fields with
    the same name in different parts of a merged report are dropped), which would silently remove
    visible signature stamps. A digital signature can never stay cryptographically valid once the
    PDF is split, so its *visible appearance* is what must be preserved - baking does exactly that.
    The prepared copy becomes the job's source, so validation compares against it.
    """
    count = sum(1 for page in src for _ in page.widgets())
    if not count:
        return src
    prepared = job_dir / "source_prepared.pdf"
    src.bake(annots=False, widgets=True)
    src.save(str(prepared), garbage=1)
    src.close()
    spec["source_path"] = str(prepared)
    write_json_atomic(job_dir / "job.json", spec)
    status.event("warn", f"{count} form/signature field(s) were converted to static page content so their "
                         f"visible appearance is kept in every volume. Digital signatures cannot remain "
                         f"cryptographically valid after a PDF is split.")
    return fitz.open(str(prepared))


def _warn_low_memory(status: "StatusWriter") -> None:
    try:
        import psutil

        available = psutil.virtual_memory().available
    except Exception:  # noqa: BLE001 - psutil is optional
        return
    if available < 400_000_000:
        status.event("warn", f"Low available memory ({format_size(available)}). Processing may be slow or fail.")


def _validate(job_dir: Path, spec: dict, vol: dict, src: fitz.Document, fps: List[dict], kind: Optional[str]) -> dict:
    out_path = job_dir / "out" / vol["filename"]
    s, e = vol["start_page"], vol["end_page"]
    expected_bm = len(volume_toc(spec["toc"], s, e)) if spec["preserve_bookmarks"] else 0
    return validate_volume(
        out_path, src, s, e, fps[s - 1:e], content_check_for(kind), expected_bm,
        spec.get("max_bytes"), [x for x in spec["sections"] if x["protected"]],
    )


def _apply_result(job_dir: Path, spec: dict, vol: dict, res: dict, src, fps, validation: Optional[dict] = None) -> None:
    chosen = res["chosen"]
    out_path = job_dir / "out" / vol["filename"]
    kind = (chosen.get("strategy") or {}).get("kind", "plain")
    if validation is None:
        shutil.copyfile(chosen["path"], out_path)
        validation = _validate(job_dir, spec, vol, src, fps, kind)
    vol.update(
        size_bytes=out_path.stat().st_size,
        size_exact=format_bytes_exact(out_path.stat().st_size),
        original_bytes=(res.get("lossless") or chosen)["size"],
        compression_level=chosen["level"],
        compression_label=chosen["label"],
        strategy_kind=kind,
        text_preserved=chosen["preserves_text"],
        extreme=chosen["level"] >= EXTREME_LEVEL,
        achieved=res["achieved"],
        validation=validation,
        candidates={k: res[k] for k in ("smallest", "lossless") if res.get(k)},
    )
    vol.setdefault("attempts", [])
    vol["attempts"] = vol["attempts"] + res["attempts"]
    if not validation["passed"]:
        vol["status"] = "FAIL"
    elif spec.get("max_bytes") and vol["size_bytes"] > spec["max_bytes"]:
        vol["status"] = "OVER_LIMIT"
    else:
        vol["status"] = "PASS"


def summarize(spec: dict, volumes: List[dict], coverage: List[dict]) -> dict:
    total = sum(v.get("size_bytes") or 0 for v in volumes)
    statuses = [v.get("status") for v in volumes]
    all_valid = all(s in ("PASS", "OVER_LIMIT", "ACCEPTED_OVER_LIMIT") for s in statuses) and all(
        c["ok"] for c in coverage
    )
    unresolved = [v["index"] for v in volumes if v.get("status") == "OVER_LIMIT"]
    failed = [v["index"] for v in volumes if v.get("status") == "FAIL"]
    if failed:
        message = f"Validation failed for volume(s) {', '.join(map(str, failed))}. Do not use these files."
    elif unresolved:
        message = (f"{len(volumes)} volumes generated, but volume(s) {', '.join(map(str, unresolved))} could not be "
                   f"reduced below the size limit without splitting a protected section. Please choose an option.")
    else:
        message = f"{len(volumes)} volume{'s' if len(volumes) != 1 else ''} successfully generated."
    return {
        "original_bytes": spec["source_size"],
        "total_bytes": total,
        "volume_count": len(volumes),
        "coverage": coverage,
        "validation_passed": all_valid and not failed,
        "unresolved": unresolved,
        "ready": not failed and not unresolved,
        "message": message,
    }


def available_cpus() -> int:
    """CPUs this process may really use.

    Inside a container (Docker, Hugging Face Spaces, ...) os.cpu_count() reports the HOST's cores,
    e.g. 32, while the container is limited to 2. The cgroup limit and CPU affinity are checked first.
    """
    try:  # cgroup v2: "max 100000" (no limit) or "200000 100000" (= 2 CPUs)
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            return max(1, int(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    try:  # cgroup v1
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        if quota > 0:
            return max(1, quota // period)
    except (OSError, ValueError):
        pass
    if hasattr(os, "sched_getaffinity"):
        return max(1, len(os.sched_getaffinity(0)))
    return max(1, os.cpu_count() or 1)


def worker_count(spec: dict) -> int:
    """Parallel worker processes for one job: CPU cores (shared by concurrent jobs), less when memory is low."""
    configured = int(spec.get("workers") or 0)
    cpus = available_cpus()
    # Leave one core for the web server when there are enough; small containers use all they have.
    workers = configured or max(1, min(8, cpus - 1 if cpus > 2 else cpus))
    try:
        import psutil

        available = psutil.virtual_memory().available
        # Roughly one worker per 600 MB of free memory, plus room for large source files.
        per_worker = 600_000_000 + spec.get("source_size", 0)
        workers = min(workers, max(1, int(available // per_worker)))
    except Exception:  # noqa: BLE001 - psutil is optional
        pass
    return max(1, workers)


def run_generation(job_dir: Path, status: StatusWriter, is_cancelled: Callable[[], bool]) -> None:
    spec = read_json(job_dir / "job.json")
    ensure_dir(job_dir / "out")
    work_dir = ensure_dir(job_dir / "work")
    status.set(state="running", started_at=time.time())
    status.event("info", "Analyzing PDF...")
    _check_disk(job_dir, spec["source_size"] * 2 + 50_000_000)

    src = fitz.open(spec["source_path"])
    try:
        if src.page_count != spec["page_count"]:
            raise PdfProcessingError("The source PDF changed since it was analysed.", "changed")
        status.event("ok", f"PDF loaded ({src.page_count} pages, {format_size(spec['source_size'])})")
        status.event("ok", f"{len(spec['toc'])} bookmarks, {sum(1 for s in spec['sections'] if s['protected'])} "
                           f"protected section(s), {len(spec['volumes'])} volume(s) planned")
        src = _bake_form_fields(src, job_dir, spec, status)
        status.progress(0.05)

        toc = spec["toc"] if spec["preserve_bookmarks"] else None
        total_pages = sum(v["end_page"] - v["start_page"] + 1 for v in spec["volumes"])
        results: List[dict] = []
        for plan_vol in spec["volumes"]:
            s, e = plan_vol["start_page"], plan_vol["end_page"]
            results.append({
                "index": plan_vol["index"],
                "filename": plan_vol["filename"],
                "start_page": s,
                "end_page": e,
                "page_count": e - s + 1,
                "sections": plan_vol["sections"],
                "protected_count": sum(1 for x in plan_vol["sections"] if x["protected"]),
                "status": "PROCESSING",
            })
        status.data["volumes"] = results
        _check_disk(job_dir, spec["source_size"] * 2 + 20_000_000)
        _warn_low_memory(status)

        workers = worker_count(spec)
        status.event("info", f"Generating {len(results)} volume(s) in parallel ({workers} worker process(es))...")
        compressed: dict = {}
        done_pages = [0]
        cancel_flag = job_dir / "cancel.flag"

        def compress_one(vol: dict) -> dict:
            i, s, e = vol["index"], vol["start_page"], vol["end_page"]

            def log(level: str, msg: str) -> None:
                status.event(level, f"Volume {i}: {msg}")

            res = compress_volume(
                None, s, e, toc, spec.get("max_bytes"), spec["compression"], work_dir, f"vol{i:02d}", log,
                is_cancelled, spec.get("gs_path"), spec.get("gs_timeout", 600),
                pool=pool, source_path=spec["source_path"], batch_size=workers, cancel_flag=cancel_flag,
            )
            with status.lock:
                done_pages[0] += e - s + 1
                status.progress(0.05 + 0.75 * done_pages[0] / max(total_pages, 1))
            return res

        source = spec["source_path"]
        with ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn")) as pool, \
                ThreadPoolExecutor(max_workers=max(1, len(results))) as coordinators:
            # Page fingerprints (for validation) are read in the background while volumes compress.
            n = spec["page_count"]
            chunk = max(25, -(-n // workers))
            fp_futures = [pool.submit(worker_fingerprints, source, a, min(a + chunk - 1, n))
                          for a in range(1, n + 1, chunk)]
            futures = {vol["index"]: coordinators.submit(compress_one, vol) for vol in results}
            try:
                for idx, fut in futures.items():
                    compressed[idx] = fut.result()
                fps = [fp for f in fp_futures for fp in f.result()]
                write_json_atomic(job_dir / "fingerprints.json", fps)

                # Validate all volumes in parallel (each worker opens the source once).
                status.event("info", "Validating volumes...")
                val_futures = {}
                for vol in results:
                    res = compressed[vol["index"]]
                    out_path = job_dir / "out" / vol["filename"]
                    shutil.copyfile(res["chosen"]["path"], out_path)
                    kind = (res["chosen"].get("strategy") or {}).get("kind", "plain")
                    s, e = vol["start_page"], vol["end_page"]
                    expected_bm = len(volume_toc(spec["toc"], s, e)) if spec["preserve_bookmarks"] else 0
                    val_futures[vol["index"]] = pool.submit(
                        worker_validate, source, str(out_path), s, e, fps[s - 1:e], content_check_for(kind),
                        expected_bm, spec.get("max_bytes"), [x for x in spec["sections"] if x["protected"]])
                validations = {i: f.result() for i, f in val_futures.items()}
            except BaseException:
                cancel_flag.touch()  # stop the other volumes quickly
                for fut in futures.values():
                    fut.cancel()
                raise

        for n_done, vol in enumerate(results, start=1):
            if is_cancelled():
                raise CancelledError()
            i = vol["index"]
            _apply_result(job_dir, spec, vol, compressed[i], src, fps, validation=validations[i])
            if vol["status"] == "PASS":
                extra = " — extreme compression applied, quality may be significantly reduced" if vol["extreme"] else ""
                status.event("ok", f"Volume {i} complete: {format_size(vol['size_bytes'])}, "
                                   f"compression level {vol['compression_level']}, validation PASS{extra}")
            elif vol["status"] == "OVER_LIMIT":
                names = ", ".join(x["title"] for x in vol["sections"])
                status.event("warn", f"Volume {i} ({names}) cannot currently be reduced below "
                                     f"{format_size(spec['max_bytes'])} without splitting the protected section. "
                                     f"Smallest achieved: {format_size(vol['candidates']['smallest']['size'])}.")
            else:
                status.event("error", f"Volume {i} failed validation.")
            status.progress(0.8 + 0.18 * n_done / len(results))
        status.save()

        coverage = validate_coverage(
            [(v["start_page"], v["end_page"]) for v in results], spec["page_count"],
            [x for x in spec["sections"] if x["protected"]],
        )
        summary = summarize(spec, results, coverage)
        status.data["volumes"] = results
        status.event("ok" if summary["ready"] else "warn", summary["message"])
        status.set(state="completed", progress=1.0, summary=summary, finished_at=time.time())
    finally:
        src.close()


def run_stronger(job_dir: Path, status: StatusWriter, is_cancelled: Callable[[], bool], volume_index: int) -> None:
    spec = read_json(job_dir / "job.json")
    fps = read_json(job_dir / "fingerprints.json")
    vol = next(v for v in status.data["volumes"] if v["index"] == volume_index)
    status.set(state="running")
    status.event("info", f"Volume {volume_index}: trying stronger compression (beyond the automatic levels)...")
    src = fitz.open(spec["source_path"])
    try:
        lossless = (vol.get("candidates") or {}).get("lossless")
        toc = spec["toc"] if spec["preserve_bookmarks"] else None

        def log(level: str, msg: str) -> None:
            status.event(level, f"Volume {volume_index}: {msg}")

        res = compress_volume(
            src, vol["start_page"], vol["end_page"], toc, spec.get("max_bytes"),
            spec["compression"], ensure_dir(job_dir / "work"),
            f"vol{volume_index:02d}_strong", log, is_cancelled, spec.get("gs_path"), spec.get("gs_timeout", 600),
            ladder=stronger_ladder(), lossless_path=Path(lossless["path"]) if lossless else None,
        )
        previous_smallest = (vol.get("candidates") or {}).get("smallest")
        if not res["achieved"] and previous_smallest and (
                res["smallest"] is None or previous_smallest["size"] <= res["smallest"]["size"]):
            res["chosen"] = res["smallest"] = previous_smallest
        if lossless:
            res["lossless"] = lossless
        _apply_result(job_dir, spec, vol, res, src, fps)
        if vol["status"] == "PASS":
            status.event("ok", f"Volume {volume_index}: target achieved at {format_size(vol['size_bytes'])} "
                               f"(extreme compression — quality significantly reduced).")
        else:
            status.event("warn", f"Volume {volume_index}: still {format_size(vol['size_bytes'])}, above the limit.")
        coverage = validate_coverage(
            [(v["start_page"], v["end_page"]) for v in status.data["volumes"]], spec["page_count"],
            [x for x in spec["sections"] if x["protected"]],
        )
        summary = summarize(spec, status.data["volumes"], coverage)
        status.set(state="completed", progress=1.0, summary=summary)
    finally:
        src.close()


def child_main(job_dir_str: str, action: str = "generate", volume_index: Optional[int] = None) -> None:
    """Entry point of the worker process."""
    job_dir = Path(job_dir_str)
    status = StatusWriter(job_dir)
    cancel_flag = job_dir / "cancel.flag"

    def is_cancelled() -> bool:
        return cancel_flag.exists()

    try:
        if action == "generate":
            run_generation(job_dir, status, is_cancelled)
        elif action == "stronger":
            run_stronger(job_dir, status, is_cancelled, int(volume_index))
        else:
            raise ValueError(f"unknown action {action}")
    except CancelledError:
        _finish_with_problem(job_dir, status, action, "cancelled", "Processing cancelled by the user.")
    except PdfProcessingError as exc:
        _finish_with_problem(job_dir, status, action, "failed", exc.message)
    except MemoryError:
        _finish_with_problem(job_dir, status, action, "failed",
                             "Not enough memory to process this PDF. Try fewer pages per volume or close other programs.")
    except Exception as exc:  # noqa: BLE001 - report without document content
        # Only code locations are recorded - never exception text, which could quote document data.
        where = [f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in traceback.extract_tb(exc.__traceback__)[-4:]]
        status.data["debug"] = where
        _finish_with_problem(job_dir, status, action, "failed",
                             f"Unexpected processing error ({type(exc).__name__}). No files were produced for the failed step.")


def _finish_with_problem(job_dir: Path, status: StatusWriter, action: str, state: str, message: str) -> None:
    """Record a failure or cancellation.

    For a first generation, partial output is removed. For "try stronger compression" on an
    already completed job, the existing volumes and their saved versions stay untouched and the
    job returns to "completed", so the user keeps their results and decisions.
    """
    if action == "generate":
        status.event("error" if state == "failed" else "warn", message)
        status.set(state=state, error=message if state == "failed" else None)
        if state == "cancelled":
            remove_tree(job_dir / "out")
        remove_tree(job_dir / "work")
    else:
        status.event("warn", f"Stronger compression stopped: {message} The previous result was kept.")
        status.set(state="completed", progress=1.0)
