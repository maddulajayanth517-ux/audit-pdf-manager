"""Progressive PDF compression.

Every attempt builds a complete temporary PDF from the ORIGINAL pages (attempts are never
stacked on top of each other, so quality is not lost twice), the real file size is measured,
and the first attempt that meets the target is accepted.

Ladder (automatic mode):

  Level 0  original pages, no compression
  Level 1  lossless optimisation: unused objects removed, streams/fonts deflated, object
           streams, metadata stripped, fonts subset
  Level 2  image recompression, text kept: 200 DPI/q90, 150 DPI/q80, 150 DPI/q70,
           Ghostscript /ebook (if installed)
  Level 3  images 120 DPI/q60 (text kept), then rasterise pages at 150 DPI/q60
  Level 4  images 96 DPI/q50 (text kept), Ghostscript /screen, rasterise 120 DPI/q45
  Level 5  images 72 DPI/q40 grey (text kept), rasterise 96 DPI/q35 grey
  Level 6  emergency: rasterise 72 DPI/q25 grey, 72 DPI/q15 grey
  Level 7  floor (still automatic): rasterise 60/48/36 DPI grey
  Level 8  "try stronger compression" (user-triggered): rasterise 30/24 DPI grey

Speed: level 0 is tried first (it is instant and often enough). The remaining levels are tried in
batches - in parallel worker processes when a pool is given - and the GENTLEST attempt of a batch
that meets the target wins. That is exactly the file a one-by-one search would choose; only the
waiting time is shorter.

Text-preserving steps come before rasterisation at each level, following the priority order:
size limit first, then readable content, then searchable text, then visual quality.
"""
from __future__ import annotations

import glob
import io
import os
import shutil
import subprocess
from concurrent.futures import as_completed
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, List, Optional, Sequence

import pymupdf as fitz  # PyMuPDF
from PIL import Image

from ..utils.size_utils import format_size as _fmt
from .pdf_reader import PdfProcessingError, save_pdf
from .pdf_splitter import apply_toc, build_volume

MAX_RASTER_PIXELS = 40_000_000  # per page; very large pages get a lower effective DPI
MIN_IMAGE_BYTES = 8_000  # smaller images are not worth recompressing

LogFn = Callable[[str, str], None]


class CancelledError(Exception):
    pass


@dataclass(frozen=True)
class Strategy:
    level: int
    kind: str  # plain | optimize | images | ghostscript | raster
    dpi: Optional[int] = None
    quality: Optional[int] = None
    grayscale: bool = False
    gs_preset: Optional[str] = None

    @property
    def preserves_text(self) -> bool:
        return self.kind != "raster"

    @property
    def key(self) -> str:
        parts = [self.kind]
        if self.dpi:
            parts.append(f"{self.dpi}dpi")
        if self.quality:
            parts.append(f"q{self.quality}")
        if self.grayscale:
            parts.append("gray")
        if self.gs_preset:
            parts.append(self.gs_preset)
        return "_".join(parts)

    @property
    def label(self) -> str:
        if self.kind == "plain":
            return "No compression"
        if self.kind == "optimize":
            return "Lossless PDF optimisation"
        if self.kind == "ghostscript":
            return f"Ghostscript /{self.gs_preset}"
        tone = ", greyscale" if self.grayscale else ""
        if self.kind == "images":
            return f"Image recompression {self.dpi} DPI, JPEG {self.quality}{tone} (text kept)"
        return f"Page rasterisation {self.dpi} DPI, JPEG {self.quality}{tone} (text not searchable)"


# The strongest automatic step. The planner uses it to estimate the smallest size a volume can
# reach, so moves between volumes can be checked against the required size.
FLOOR_STRATEGY = Strategy(7, "raster", 36, 5, True)


# ----------------------------------------------------------------------------- ladder

def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def build_ladder(cfg: dict, gs_available: bool) -> List[Strategy]:
    mode = cfg.get("mode", "automatic")
    ladder = [Strategy(0, "plain")]
    if mode == "off":
        return ladder
    ladder.append(Strategy(1, "optimize"))
    if mode == "lossless":
        return ladder

    gray_ok = bool(cfg.get("allow_grayscale", True))
    raster_ok = bool(cfg.get("allow_rasterization", True)) and cfg.get("preserve_text", "preferred") != "required"
    use_gs = gs_available and bool(cfg.get("use_ghostscript", True))
    min_dpi, max_dpi = int(cfg.get("min_dpi", 72)), int(cfg.get("max_dpi", 300))
    q_lo, q_hi = int(cfg.get("jpeg_quality_min", 10)), int(cfg.get("jpeg_quality_max", 95))

    raw = [
        (2, "images", 200, 90, False, None),
        (2, "images", 150, 80, False, None),
        (2, "images", 150, 70, False, None),
        (2, "ghostscript", None, None, False, "ebook"),
        (3, "images", 120, 60, False, None),
        (3, "raster", 150, 60, False, None),
        (4, "images", 96, 50, False, None),
        (4, "ghostscript", None, None, False, "screen"),
        (4, "raster", 120, 45, False, None),
        (5, "images", 72, 40, True, None),
        (5, "raster", 96, 35, True, None),
        (6, "raster", 72, 25, True, None),
        (6, "raster", 72, 15, True, None),
        # Size compliance beats quality: keep going automatically down to the floor strategy.
        (7, "raster", 60, 12, True, None),
        (7, "raster", 48, 8, True, None),
        (7, FLOOR_STRATEGY.kind, FLOOR_STRATEGY.dpi, FLOOR_STRATEGY.quality, True, None),
    ]
    seen = set()
    for level, kind, dpi, q, gray, preset in raw:
        if kind == "raster" and not raster_ok:
            continue
        if kind == "ghostscript" and not use_gs:
            continue
        if dpi is not None:
            dpi = _clamp(dpi, min_dpi, max_dpi)
        if q is not None:
            q = _clamp(q, q_lo, q_hi)
        st = Strategy(level, kind, dpi, q, gray and gray_ok, preset)
        sig = (st.kind, st.dpi, st.quality, st.grayscale, st.gs_preset)
        if sig in seen:
            continue
        seen.add(sig)
        ladder.append(st)
    return ladder


def stronger_ladder() -> List[Strategy]:
    """User explicitly asked for more than the automatic floor (text will be barely legible)."""
    return [
        Strategy(8, "raster", 30, 5, True),
        Strategy(8, "raster", 24, 5, True),
    ]


def describe_ladder(cfg: dict, gs_available: bool) -> List[dict]:
    return [{"level": s.level, "label": s.label, "preserves_text": s.preserves_text} for s in build_ladder(cfg, gs_available)]


# ----------------------------------------------------------------------------- ghostscript

@lru_cache(maxsize=4)
def find_ghostscript(configured: str = "") -> tuple[Optional[str], Optional[str]]:
    candidates: List[str] = []
    if configured:
        candidates.append(configured)
    for name in ("gswin64c", "gswin32c", "gs"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    if os.name == "nt":
        for pattern in (
            r"C:\Program Files\gs\gs*\bin\gswin64c.exe",
            r"C:\Program Files (x86)\gs\gs*\bin\gswin32c.exe",
        ):
            candidates.extend(sorted(glob.glob(pattern), reverse=True))
    for cand in candidates:
        if not os.path.isfile(cand):
            continue
        try:
            res = subprocess.run(
                [cand, "--version"], capture_output=True, text=True, timeout=15, **_no_window()
            )
            if res.returncode == 0 and res.stdout.strip():
                return cand, res.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
    return None, None


def _no_window() -> dict:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def run_ghostscript(gs_path: str, src: Path, dst: Path, preset: str, timeout: int) -> None:
    cmd = [
        gs_path,
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.5",
        f"-dPDFSETTINGS=/{preset}",
        "-dNOPAUSE",
        "-dBATCH",
        "-dQUIET",
        "-dSAFER",
        "-dAutoRotatePages=/None",  # never change page orientation
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dSubsetFonts=true",
        f"-sOutputFile={dst}",
        str(src),
    ]
    res = subprocess.run(cmd, capture_output=True, timeout=timeout, **_no_window())
    if res.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError(f"Ghostscript failed (exit code {res.returncode}).")


# ----------------------------------------------------------------------------- strategies

def _optimize_structure(doc: fitz.Document) -> None:
    try:
        doc.set_metadata({})
    except Exception:  # noqa: BLE001
        pass
    try:
        doc.del_xml_metadata()
    except Exception:  # noqa: BLE001
        pass
    try:
        doc.subset_fonts()
    except Exception:  # noqa: BLE001 - optional dependency / unsupported font types
        pass


def recompress_images(
    doc: fitz.Document,
    target_dpi: int,
    quality: int,
    grayscale: bool,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> tuple[int, int]:
    """Downsample / re-encode embedded images in place. Returns (candidates, replaced)."""
    info: dict[int, dict] = {}
    for page in doc:
        page_long_side = max(page.rect.width, page.rect.height)
        for img in page.get_images(full=True):
            xref, w, h, bpc = img[0], img[2], img[3], img[4]
            rec = info.setdefault(xref, {"w": w, "h": h, "bpc": bpc, "disp": 0.0})
            try:
                rects = page.get_image_rects(xref)
            except Exception:  # noqa: BLE001
                rects = []
            disp = max((max(r.width, r.height) for r in rects), default=page_long_side)
            rec["disp"] = max(rec["disp"], disp)

    candidates = replaced = 0
    for xref, rec in info.items():
        if is_cancelled():
            raise CancelledError()
        if rec["bpc"] == 1 or rec["w"] < 32 or rec["h"] < 32:
            continue  # bilevel scans (CCITT/JBIG2) are already compact; tiny images are not worth it
        try:
            if doc.xref_get_key(xref, "ImageMask")[1] == "true":
                continue
            if doc.xref_get_key(xref, "Mask")[0] == "array":
                continue  # colour-key masking does not survive lossy compression
            raw_len = len(doc.xref_stream_raw(xref) or b"")
        except Exception:  # noqa: BLE001
            continue
        if raw_len < MIN_IMAGE_BYTES:
            continue
        candidates += 1
        try:
            new_data, nw, nh, cs = _encode_image(doc, xref, rec, target_dpi, quality, grayscale)
        except Exception:  # noqa: BLE001 - unusual colour spaces etc. are left untouched
            continue
        if new_data is None or len(new_data) >= raw_len * 0.95:
            continue
        doc.update_stream(xref, new_data, compress=False)
        doc.xref_set_key(xref, "Filter", "/DCTDecode")
        doc.xref_set_key(xref, "DecodeParms", "null")
        doc.xref_set_key(xref, "Decode", "null")
        doc.xref_set_key(xref, "Width", str(nw))
        doc.xref_set_key(xref, "Height", str(nh))
        doc.xref_set_key(xref, "BitsPerComponent", "8")
        doc.xref_set_key(xref, "ColorSpace", cs)
        doc.xref_set_key(xref, "SMaskInData", "null")  # JPEG2000-only flag; invalid for JPEG
        replaced += 1
    return candidates, replaced


def _encode_image(doc, xref, rec, target_dpi, quality, grayscale):
    pix = fitz.Pixmap(doc, xref)
    if pix.colorspace is None:
        return None, 0, 0, ""
    if pix.alpha:
        pix = fitz.Pixmap(pix, 0)
    if pix.n not in (1, 3):
        pix = fitz.Pixmap(fitz.csRGB, pix)
    mode = "L" if pix.n == 1 else "RGB"
    im = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
    pix = None
    if grayscale and mode != "L":
        im = im.convert("L")
    eff_dpi = max(im.width, im.height) / max(rec["disp"] / 72.0, 0.01)
    if eff_dpi > target_dpi * 1.05:
        scale = target_dpi / eff_dpi
        im = im.resize(
            (max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS
        )
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=int(quality), optimize=True)
    cs = "/DeviceGray" if im.mode == "L" else "/DeviceRGB"
    return buf.getvalue(), im.width, im.height, cs


def rasterize(
    base: fitz.Document,
    dpi: int,
    quality: int,
    grayscale: bool,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> fitz.Document:
    out = fitz.open()
    cs = fitz.csGRAY if grayscale else fitz.csRGB
    for page in base:
        if is_cancelled():
            out.close()
            raise CancelledError()
        rect = page.rect
        page_dpi = dpi
        pixels = (rect.width / 72 * dpi) * (rect.height / 72 * dpi)
        if pixels > MAX_RASTER_PIXELS:
            page_dpi = max(10, int(dpi * (MAX_RASTER_PIXELS / pixels) ** 0.5))
        pix = page.get_pixmap(dpi=page_dpi, colorspace=cs, alpha=False)
        mode = "L" if pix.n == 1 else "RGB"
        im = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        pix = None
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=int(quality), optimize=True)
        new_page = out.new_page(width=rect.width, height=rect.height)
        new_page.insert_image(new_page.rect, stream=buf.getvalue())
    return out


# ----------------------------------------------------------------------------- main loop

def _attempt(
    st: Strategy,
    src: fitz.Document,
    start: int,
    end: int,
    toc,
    out_path: Path,
    lossless_path: Optional[Path],
    gs_path: Optional[str],
    gs_timeout: int,
    is_cancelled,
) -> dict:
    extra: dict = {}
    if st.kind == "ghostscript":
        if not gs_path or not lossless_path:
            raise RuntimeError("Ghostscript is not available.")
        tmp = out_path.with_suffix(".gs.pdf")
        try:
            run_ghostscript(gs_path, lossless_path, tmp, st.gs_preset, gs_timeout)
            doc = fitz.open(str(tmp))
            try:
                if doc.page_count != end - start + 1:
                    raise RuntimeError("Ghostscript output has a different page count; discarded.")
                apply_toc(doc, toc, start, end)
                save_pdf(doc, out_path, optimize=True)
            finally:
                doc.close()
        finally:
            tmp.unlink(missing_ok=True)
        return extra

    base = build_volume(src, start, end, None)
    try:
        if st.kind == "plain":
            apply_toc(base, toc, start, end)
            save_pdf(base, out_path, optimize=False)
        elif st.kind == "optimize":
            _optimize_structure(base)
            apply_toc(base, toc, start, end)
            save_pdf(base, out_path, optimize=True)
        elif st.kind == "images":
            _optimize_structure(base)
            cands, replaced = recompress_images(base, st.dpi, st.quality, st.grayscale, is_cancelled)
            extra = {"image_candidates": cands, "images_replaced": replaced}
            apply_toc(base, toc, start, end)
            save_pdf(base, out_path, optimize=True)
        elif st.kind == "raster":
            out = rasterize(base, st.dpi, st.quality, st.grayscale, is_cancelled)
            try:
                apply_toc(out, toc, start, end)
                save_pdf(out, out_path, optimize=True)
            finally:
                out.close()
        else:
            raise ValueError(f"Unknown strategy {st.kind}")
    finally:
        base.close()
    return extra


def _run_and_measure(st, src, start, end, toc, out_path, lossless_path, gs_path, gs_timeout, is_cancelled) -> dict:
    """Run one attempt and measure the real file. Never raises except CancelledError."""
    try:
        extra = _attempt(st, src, start, end, toc, out_path, lossless_path, gs_path, gs_timeout, is_cancelled)
        size = out_path.stat().st_size
        check = fitz.open(str(out_path))
        pages_ok = check.page_count == end - start + 1
        check.close()
        if not pages_ok:
            raise RuntimeError("generated file has the wrong page count")
        return {"size": size, "extra": extra, "error": None}
    except CancelledError:
        out_path.unlink(missing_ok=True)
        raise
    except MemoryError:
        out_path.unlink(missing_ok=True)
        return {"size": None, "extra": {}, "error": "out of memory"}
    except Exception as exc:  # noqa: BLE001
        out_path.unlink(missing_ok=True)
        return {"size": None, "extra": {}, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


# --- worker-process side (parallel mode) -------------------------------------------------------

_WORKER_DOCS: dict = {}
SAMPLE_PAGES = 6          # pages used to predict the size of each level
MIN_PAGES_TO_PREDICT = 16  # smaller volumes are simply tried in full
PREDICT_MARGIN = 1.35     # also try levels predicted up to 35% above the target (predictions are estimates)


def _worker_source(source_path: str) -> fitz.Document:
    """The source PDF, opened once per worker process."""
    src = _WORKER_DOCS.get(source_path)
    if src is None:
        try:
            fitz.TOOLS.mupdf_display_errors(False)
            fitz.TOOLS.mupdf_display_warnings(False)
        except Exception:  # noqa: BLE001
            pass
        src = _WORKER_DOCS[source_path] = fitz.open(source_path)
    return src


def worker_sample(source_path: str, pages: List[int], strategy: dict, out_path: str,
                  gs_path: Optional[str], gs_timeout: int) -> Optional[int]:
    """Size of one level applied to a few sample pages only (used to predict the full size)."""
    src = _worker_source(source_path)
    base = Path(out_path)
    sample_src = base.with_suffix(".src.pdf")
    sdoc = fitz.open()
    try:
        for p in pages:
            sdoc.insert_pdf(src, from_page=p - 1, to_page=p - 1)
        save_pdf(sdoc, sample_src)
        res = _run_and_measure(Strategy(**strategy), sdoc, 1, len(pages), None, base, sample_src,
                               gs_path, gs_timeout, lambda: False)
        return res["size"]
    except Exception:  # noqa: BLE001 - a failed prediction only means "try it for real"
        return None
    finally:
        sdoc.close()
        sample_src.unlink(missing_ok=True)
        base.unlink(missing_ok=True)


def worker_fingerprints(source_path: str, start: int, end: int) -> List[dict]:
    from .pdf_validator import page_fingerprints
    return page_fingerprints(_worker_source(source_path), with_text=True, start=start, end=end)


def worker_validate(source_path: str, *args) -> dict:
    from .pdf_validator import validate_volume
    out_path, start, end, fps, content_check, expected_bm, max_bytes, protected = args
    return validate_volume(Path(out_path), _worker_source(source_path), start, end, fps, content_check,
                           expected_bm, max_bytes, protected)


def worker_attempt(source_path: str, start: int, end: int, toc, strategy: dict, out_path: str,
                   lossless_path: Optional[str], gs_path: Optional[str], gs_timeout: int,
                   stop_flags: Sequence[str] = ()) -> dict:
    """Runs inside a pool worker process. Stops early if any stop flag file appears."""
    src = _worker_source(source_path)
    flags = [Path(f) for f in stop_flags if f]
    try:
        return _run_and_measure(Strategy(**strategy), src, start, end, toc, Path(out_path),
                                Path(lossless_path) if lossless_path else None, gs_path, gs_timeout,
                                lambda: any(f.exists() for f in flags))
    except CancelledError:
        return {"size": None, "extra": {}, "error": "cancelled"}


# --- search ------------------------------------------------------------------------------------

def _prioritise_by_prediction(pending: List[Strategy], full_l0: int, target: int, start: int, end: int,
                              pool, source_path: str, workdir: Path, name: str, gs_path, gs_timeout,
                              log: LogFn) -> List[Strategy]:
    """Predict every level's full size from a few sample pages and drop hopeless gentle levels.

    Levels predicted to stay far above the target (more than PREDICT_MARGIN x target) are skipped,
    unless they come after the first plausible level. The order of the remaining levels is unchanged,
    so the gentlest REAL success still wins. If nothing looks plausible, the strongest levels are kept.
    """
    n = end - start + 1
    k = min(SAMPLE_PAGES, n)
    pages = sorted({start + round(i * (n - 1) / max(k - 1, 1)) for i in range(k)})
    plain = Strategy(0, "plain")
    jobs = [plain] + list(pending)
    futures = [pool.submit(worker_sample, source_path, pages, asdict(st),
                           str(workdir / f"{name}__sample{i}.pdf"), gs_path, gs_timeout)
               for i, st in enumerate(jobs)]
    sizes = [f.result() for f in futures]
    base = sizes[0]
    if not base:
        return pending
    predicted = {st: (None if s is None else full_l0 * s / base) for st, s in zip(jobs[1:], sizes[1:])}
    plausible = [i for i, st in enumerate(pending)
                 if predicted[st] is None or predicted[st] <= target * PREDICT_MARGIN]
    if not plausible:
        log("info", "Size estimate from sample pages: the limit looks very hard to reach; trying the strongest levels.")
        return pending[-4:]
    first = plausible[0]
    kept = [st for i, st in enumerate(pending) if i >= first and
            (predicted[st] is None or predicted[st] <= target * PREDICT_MARGIN or i > plausible[-1])]
    skipped = len(pending) - len(kept)
    if skipped:
        log("info", f"Size estimate from {k} sample pages: skipping {skipped} level(s) that cannot reach the limit.")
    return kept


def compress_volume(
    src: Optional[fitz.Document],
    start: int,
    end: int,
    toc,
    target_bytes: Optional[int],
    cfg: dict,
    workdir: Path,
    name: str,
    log: LogFn,
    is_cancelled: Callable[[], bool] = lambda: False,
    gs_path: Optional[str] = None,
    gs_timeout: int = 600,
    ladder: Optional[Sequence[Strategy]] = None,
    lossless_path: Optional[Path] = None,
    pool=None,
    source_path: Optional[str] = None,
    batch_size: int = 1,
    cancel_flag: Optional[Path] = None,
) -> dict:
    """Find the gentlest compression whose REAL file size meets ``target_bytes``.

    Sequential mode (``pool`` is None): ``src`` is used and levels are tried one by one.
    Parallel mode: ``pool`` (a ProcessPoolExecutor) and ``source_path`` are used; after level 0,
    levels are tried ``batch_size`` at a time in parallel and the gentlest success wins.

    Returns a dict with ``achieved``, ``chosen`` (the file to deliver), ``smallest`` and
    ``lossless`` candidates (kept for the user's "allow over limit" decision) and ``attempts``.
    """
    ladder = list(ladder or build_ladder(cfg, bool(gs_path)))
    attempts: List[dict] = []
    keep: dict = {}
    chosen: Optional[dict] = None
    lossless: Optional[dict] = None
    if lossless_path and lossless_path.exists():
        lossless = {"path": str(lossless_path), "size": lossless_path.stat().st_size, "level": 0,
                    "label": "Original quality", "preserves_text": True}
    smallest: Optional[dict] = None
    images_useless = False
    want_lossless_always = cfg.get("mode") == "lossless"
    decided_flag = workdir / f"{name}.decided"
    decided_flag.unlink(missing_ok=True)

    def run_batch(batch: List[Strategy]) -> List[dict]:
        lp = lossless["path"] if lossless else None
        paths = [workdir / f"{name}__L{st.level}_{st.key}.pdf" for st in batch]
        if pool is None:
            return [dict(_run_and_measure(st, src, start, end, toc, path, Path(lp) if lp else None,
                                          gs_path, gs_timeout, is_cancelled), path=path)
                    for st, path in zip(batch, paths)]
        flags = [str(cancel_flag) if cancel_flag else "", str(decided_flag)]
        futures = [pool.submit(worker_attempt, source_path, start, end, toc, asdict(st), str(path), lp,
                               gs_path, gs_timeout, flags)
                   for st, path in zip(batch, paths)]
        index = {f: i for i, f in enumerate(futures)}
        results: List[Optional[dict]] = [None] * len(futures)
        decided_at = None
        for f in as_completed(futures):
            r = f.result()
            results[index[f]] = dict(r, path=paths[index[f]])
            # Decided once some attempt meets the target and every gentler one has finished.
            for i, res in enumerate(results):
                if res is None:
                    break
                if res["size"] is not None and (target_bytes is None or res["size"] <= target_bytes) \
                        and not (want_lossless_always and batch[i].kind == "plain"):
                    decided_at = i
                    break
            if decided_at is not None:
                break
        if decided_at is not None:
            decided_flag.touch()  # running stronger attempts of this volume stop early
            for f in futures:
                f.cancel()        # attempts not started yet are dropped
            results = results[:decided_at + 1]
        if is_cancelled():
            for r in results:
                if r:
                    Path(r["path"]).unlink(missing_ok=True)
            raise CancelledError()
        return results

    def record(st: Strategy, res: dict) -> Optional[dict]:
        nonlocal smallest, lossless, images_useless
        if res["size"] is None:
            log("warn", f"Level {st.level} ({st.label}): failed, skipped")
            attempts.append({"level": st.level, "label": st.label, "size": None, "ok": False, "error": res["error"]})
            return None
        size, extra = res["size"], res["extra"]
        meets = target_bytes is None or size <= target_bytes
        rec = {"path": str(res["path"]), "size": size, "level": st.level, "label": st.label,
               "preserves_text": st.preserves_text, "strategy": asdict(st), "meets": meets}
        attempts.append({"level": st.level, "label": st.label, "size": size, "ok": meets, **extra})
        if target_bytes:
            log("ok" if meets else "info",
                f"Level {st.level} – {st.label}: {_fmt(size)} {'✓ target achieved' if meets else 'above target'}")
        else:
            log("info", f"Level {st.level} – {st.label}: {_fmt(size)}")
        keep[rec["path"]] = rec
        if smallest is None or size < smallest["size"]:
            smallest = rec
        if st.kind in ("plain", "optimize") and (lossless is None or size < lossless["size"]):
            lossless = rec
        if st.kind == "images" and extra.get("image_candidates", 0) == 0:
            images_useless = True
        return rec

    # Level 0 first: it is instant, and the volume often already fits.
    pending = list(ladder)
    first = pending.pop(0)
    if is_cancelled():
        raise CancelledError()
    rec = record(first, run_batch([first])[0])
    if rec and rec["meets"] and not want_lossless_always:
        chosen = rec
        pending = []

    n_pages = end - start + 1
    if chosen is None and pool is not None and target_bytes and rec and n_pages >= MIN_PAGES_TO_PREDICT:
        pending = _prioritise_by_prediction(pending, rec["size"], target_bytes, start, end, pool, source_path,
                                            workdir, name, gs_path, gs_timeout, log)

    while pending and chosen is None:
        if is_cancelled():
            raise CancelledError()
        batch: List[Strategy] = []
        while pending and len(batch) < max(1, batch_size):
            st = pending.pop(0)
            if st.kind == "images" and images_useless:
                continue
            if st.kind == "ghostscript" and not (lossless and Path(lossless["path"]).exists()):
                continue
            batch.append(st)
        if not batch:
            break
        for st, res in zip(batch, run_batch(batch)):
            if res is None:
                continue
            rec = record(st, res)
            # Results are in ladder order, so the first success in a batch is the gentlest one.
            if chosen is None and rec and rec["meets"] and not (want_lossless_always and st.kind == "plain"):
                chosen = rec

    if smallest is None and lossless is None:
        # Every attempt failed (e.g. out of memory): there is nothing safe to deliver.
        raise PdfProcessingError(
            f"Pages {start}–{end} could not be written to a volume: every attempt failed "
            f"({len(attempts)} tried). The PDF may be damaged or too large for the available memory.",
            "volume_failed",
        )
    achieved = chosen is not None
    if want_lossless_always and lossless and (chosen is None or lossless["size"] < chosen["size"]):
        chosen = lossless
        achieved = target_bytes is None or lossless["size"] <= target_bytes
    if chosen is None:
        chosen = smallest or lossless

    # Delete attempt files that are no longer needed.
    needed = {c["path"] for c in (chosen, smallest, lossless) if c}
    for path in list(keep):
        if path not in needed:
            Path(path).unlink(missing_ok=True)

    return {
        "achieved": achieved,
        "target_bytes": target_bytes,
        "chosen": chosen,
        "smallest": smallest,
        "lossless": lossless,
        "attempts": attempts,
    }
