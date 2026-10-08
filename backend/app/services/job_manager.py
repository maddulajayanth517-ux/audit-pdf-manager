"""Runs generation jobs in separate worker processes and supervises them.

Using processes (not threads) keeps the web server responsive, isolates PyMuPDF (which is not
thread-safe) and means an out-of-memory crash on a huge PDF only kills the worker.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from ..config import settings
from ..utils.file_utils import read_json, write_json_atomic
from .generation_service import child_main

log = logging.getLogger("audit_pdf.jobs")
CANCEL_GRACE_S = 20


@dataclass
class _Task:
    job_id: str
    job_dir: Path
    action: str
    volume_index: Optional[int]
    process: Optional[mp.Process] = None
    cancel_requested_at: Optional[float] = None


class JobManager:
    def __init__(self, max_concurrent: int):
        self.max_concurrent = max(1, max_concurrent)
        self.ctx = mp.get_context("spawn")
        self.lock = threading.Lock()
        self.queue: List[_Task] = []
        self.running: Dict[str, _Task] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.on_tick = None  # optional callback (cleanup)
        self._last_cleanup = 0.0

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="job-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self.lock:
            for task in self.running.values():
                if task.process and task.process.is_alive():
                    _terminate_tree(task.process)
            self.running.clear()
            self.queue.clear()

    def submit(self, job_id: str, job_dir: Path, action: str = "generate", volume_index: Optional[int] = None) -> None:
        with self.lock:
            if job_id in self.running or any(t.job_id == job_id for t in self.queue):
                raise RuntimeError("This job is already being processed.")
            self.queue.append(_Task(job_id, job_dir, action, volume_index))
        self._dispatch()

    def is_active(self, job_id: str) -> bool:
        with self.lock:
            return job_id in self.running or any(t.job_id == job_id for t in self.queue)

    def cancel(self, job_id: str) -> bool:
        with self.lock:
            for t in list(self.queue):
                if t.job_id == job_id:
                    self.queue.remove(t)
                    _mark(t.job_dir, "cancelled", "Processing cancelled before it started.", t.action)
                    return True
            task = self.running.get(job_id)
            if task is None:
                return False
            (task.job_dir / "cancel.flag").touch()
            task.cancel_requested_at = time.time()
            return True

    # ---- internals
    def _dispatch(self) -> None:
        with self.lock:
            while self.queue and len(self.running) < self.max_concurrent:
                task = self.queue.pop(0)
                (task.job_dir / "cancel.flag").unlink(missing_ok=True)
                proc = self.ctx.Process(
                    target=child_main, args=(str(task.job_dir), task.action, task.volume_index),
                    # Not a daemon: the job starts its own pool of worker processes (daemons may not).
                    # Jobs are still terminated explicitly in stop() when the server shuts down.
                    name=f"job-{task.job_id[:8]}", daemon=False,
                )
                proc.start()
                task.process = proc
                self.running[task.job_id] = task
                log.info("Started %s worker for job %s (pid %s)", task.action, task.job_id, proc.pid)

    def _reap(self) -> None:
        with self.lock:
            for job_id, task in list(self.running.items()):
                proc = task.process
                if proc is None:
                    continue
                if proc.is_alive():
                    if task.cancel_requested_at and time.time() - task.cancel_requested_at > CANCEL_GRACE_S:
                        _terminate_tree(proc)
                        _mark(task.job_dir, "cancelled", "Processing cancelled by the user.", task.action)
                    continue
                proc.join(timeout=0.1)
                status = read_json(task.job_dir / "status.json", default={}) or {}
                if status.get("state") in ("queued", "running"):
                    msg = ("The processing worker stopped unexpectedly (exit code "
                           f"{proc.exitcode}). The PDF may be too large for the available memory.")
                    _mark(task.job_dir, "failed", msg, task.action)
                log.info("Job %s worker finished (exit code %s)", job_id, proc.exitcode)
                del self.running[job_id]

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._reap()
                self._dispatch()
                if self.on_tick and time.time() - self._last_cleanup > 60:
                    self._last_cleanup = time.time()
                    self.on_tick()
            except Exception:  # noqa: BLE001
                log.exception("Job monitor error")
            self._stop.wait(0.5)


def _terminate_tree(proc) -> None:
    """Stop a job process AND the compression workers it started (otherwise they could linger)."""
    try:
        import psutil

        children = psutil.Process(proc.pid).children(recursive=True)
    except Exception:  # noqa: BLE001 - psutil missing or process already gone
        children = []
    for child in children:
        try:
            child.terminate()
        except Exception:  # noqa: BLE001
            pass
    proc.terminate()


def _mark(job_dir: Path, state: str, message: str, action: str = "generate") -> None:
    path = job_dir / "status.json"
    status = read_json(path, default={}) or {}
    if action == "stronger":
        # The job already had complete results before "try stronger compression" started: keep them.
        status.setdefault("events", []).append(
            {"t": time.time(), "level": "warn", "msg": f"Stronger compression stopped: {message} The previous result was kept."})
        status["state"] = "completed"
        write_json_atomic(path, status)
        return
    status.setdefault("events", []).append(
        {"t": time.time(), "level": "error" if state == "failed" else "warn", "msg": message}
    )
    status["state"] = state
    if state == "failed":
        status["error"] = message
    write_json_atomic(path, status)


manager = JobManager(settings.max_concurrent_jobs)
