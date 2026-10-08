"""Progressive compression (Tests 2, 3, 7, 9)."""
import pymupdf as fitz  # PyMuPDF

from backend.app.models.volume_models import CompressionSettings
from backend.app.services.compression_engine import build_ladder, compress_volume, stronger_ladder

DEFAULTS = CompressionSettings().model_dump()


def _run(path, tmp_path, target, cfg=None, start=1, end=None, toc=None, ladder=None):
    src = fitz.open(str(path))
    end = end or src.page_count
    logs = []
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    res = compress_volume(src, start, end, toc, target, cfg or DEFAULTS, work, "vol01",
                          lambda lvl, msg: logs.append((lvl, msg)), ladder=ladder)
    src.close()
    return res, logs


def test_no_compression_when_already_small(make, tmp_path):
    path = make("small.pdf", [("Main", 3, 1)])
    res, _ = _run(path, tmp_path, 10_000_000)
    assert res["achieved"]
    assert res["chosen"]["level"] == 0
    assert len(res["attempts"]) == 1


def test_oversized_annexure_is_compressed_to_target(make, tmp_path):
    """Test 2 / Test 9: a large section is compressed progressively until it fits."""
    path = make("big.pdf", [("Annexure F", 6, 1)], image_px=700)
    original = path.stat().st_size
    target = int(original * 0.45)
    res, logs = _run(path, tmp_path, target)
    assert res["achieved"], res["attempts"]
    assert res["chosen"]["size"] <= target
    assert len(res["attempts"]) >= 3  # several levels were tried
    sizes = [a["size"] for a in res["attempts"] if a["size"]]
    assert sizes[-1] <= target and all(s > target for s in sizes[:-1])
    doc = fitz.open(res["chosen"]["path"])
    assert doc.page_count == 6  # never split
    doc.close()
    assert any("target achieved" in m for _, m in logs)


def test_impossible_target_reports_instead_of_splitting(make, tmp_path):
    """Test 3: the target cannot be reached -> not achieved, smallest version kept, never split."""
    path = make("huge.pdf", [("Annexure F", 4, 1)], image_px=500)
    res, _ = _run(path, tmp_path, 5_000)
    assert not res["achieved"]
    assert res["smallest"]["size"] > 5_000
    assert res["lossless"] is not None
    levels = {a["level"] for a in res["attempts"]}
    assert 7 in levels  # the automatic floor level was reached
    doc = fitz.open(res["chosen"]["path"])
    assert doc.page_count == 4
    doc.close()


def test_scanned_pdf_is_compressed(make, tmp_path):
    """Test 7: image-only (scanned) pages are recompressed / rasterised."""
    path = make("scan.pdf", [("Annexure S", 5, 1)], image_px=1400, scanned=True)
    target = int(path.stat().st_size * 0.25)
    res, _ = _run(path, tmp_path, target)
    assert res["achieved"]
    assert res["chosen"]["size"] <= target
    assert res["chosen"]["level"] >= 2


def test_text_required_never_rasterises(make, tmp_path):
    cfg = {**DEFAULTS, "preserve_text": "required"}
    ladder = build_ladder(cfg, gs_available=False)
    assert all(s.kind != "raster" for s in ladder)
    path = make("t.pdf", [("A", 3, 1)], image_px=400)
    res, _ = _run(path, tmp_path, 3_000, cfg=cfg)
    assert not res["achieved"]
    assert res["chosen"]["preserves_text"]


def test_ladder_modes_and_limits():
    assert [s.kind for s in build_ladder({**DEFAULTS, "mode": "off"}, False)] == ["plain"]
    assert [s.kind for s in build_ladder({**DEFAULTS, "mode": "lossless"}, False)] == ["plain", "optimize"]
    auto = build_ladder(DEFAULTS, False)
    assert [s.level for s in auto] == sorted(s.level for s in auto)
    assert auto[-1].level == 7 and auto[-1].dpi == 36  # goes to the floor automatically
    assert all(s.kind != "ghostscript" for s in auto)
    with_gs = build_ladder(DEFAULTS, True)
    assert any(s.kind == "ghostscript" for s in with_gs)
    clamped = build_ladder({**DEFAULTS, "min_dpi": 120, "jpeg_quality_min": 50}, False)
    assert all((s.dpi or 999) >= 120 for s in clamped)
    assert all((s.quality or 99) >= 50 for s in clamped)
    no_gray = build_ladder({**DEFAULTS, "allow_grayscale": False}, False)
    assert not any(s.grayscale for s in no_gray)
    assert all(s.level == 8 and s.dpi < 36 for s in stronger_ladder())


def test_bookmarks_survive_every_strategy(make, tmp_path):
    path = make("bm.pdf", [("Annexure A", 2, 1), ("Sub 1", 1, 2), ("Annexure B", 2, 1)], image_px=300)
    toc = [[1, "Annexure A", 1, None], [2, "Sub 1", 3, 0], [1, "Annexure B", 4, None]]
    for st in build_ladder(DEFAULTS, False):
        res, _ = _run(path, tmp_path, None, toc=toc, ladder=[st])
        doc = fitz.open(res["chosen"]["path"])
        assert doc.get_toc(simple=True) == [[1, "Annexure A", 1], [2, "Sub 1", 3], [1, "Annexure B", 4]], st
        assert doc.page_count == 5
        doc.close()


def test_large_reduction_reached_automatically(make, tmp_path):
    """Size beats quality: a 90% reduction is reached without any user action (floor level)."""
    path = make("deep.pdf", [("Annexure X", 8, 1)], image_px=700)
    target = int(path.stat().st_size * 0.10)
    res, _ = _run(path, tmp_path, target)
    assert res["achieved"], res["attempts"]
    assert res["chosen"]["size"] <= target
    assert res["chosen"]["level"] >= 5


def test_min_size_estimate_matches_real_floor(make, tmp_path):
    from backend.app.services.compression_engine import FLOOR_STRATEGY
    from backend.app.services.section_analyzer import estimate_min_size, measure_range_size
    path = make("est.pdf", [("A", 12, 1)], image_px=500)
    src = fitz.open(str(path))
    original = measure_range_size(src, 1, 12)
    est = estimate_min_size(src, 1, 12, original)
    src.close()
    res, _ = _run(path, tmp_path, None, ladder=[FLOOR_STRATEGY])
    real = res["chosen"]["size"]
    assert est < original
    assert 0.7 * real <= est <= 1.3 * real, (est, real)


def test_all_attempts_failing_gives_clear_error(make, tmp_path):
    """Nothing usable produced -> a clear, user-facing error instead of a crash."""
    import pytest
    from backend.app.services.compression_engine import Strategy
    from backend.app.services.pdf_reader import PdfProcessingError
    path = make("f.pdf", [("A", 2, 1)])
    with pytest.raises(PdfProcessingError, match="could not be written"):
        _run(path, tmp_path, 1_000, ladder=[Strategy(2, "ghostscript", gs_preset="ebook")])


def test_recompressed_jpx_flag_removed(tmp_path):
    from backend.app.services.compression_engine import recompress_images
    from conftest import noise_jpeg
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(page.rect, stream=noise_jpeg(900, 3))
    xref = page.get_images()[0][0]
    doc.xref_set_key(xref, "SMaskInData", "1")
    _, replaced = recompress_images(doc, 72, 40, True)
    assert replaced == 1
    assert doc.xref_get_key(xref, "SMaskInData")[0] == "null"
    assert doc.xref_get_key(xref, "Filter")[1] == "/DCTDecode"


def test_fine_tuning_uses_more_of_the_limit(make, tmp_path):
    """The result must fit the limit but use as much of it as possible (sharper output)."""
    path = make("tune.pdf", [("Annexure T", 8, 1)], image_px=700)
    target = int(path.stat().st_size * 0.30)
    res, logs = _run(path, tmp_path, target)
    assert res["achieved"] and res["chosen"]["size"] <= target
    coarse = [a for a in res["attempts"] if a["ok"]][0]["size"]  # first ladder step that fitted
    if coarse < 0.92 * target:
        assert any("Fine-tuning" in m for _, m in logs)
        assert res["chosen"]["size"] >= coarse
    assert res["chosen"]["size"] >= 0.80 * target, (res["chosen"]["size"], target)
