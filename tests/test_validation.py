"""End-to-end generation and output validation (Test 10), bookmark remapping, API flow."""
import time

import pymupdf as fitz  # PyMuPDF
import pytest

from backend.app.models.volume_models import CompressionSettings
from backend.app.services.bookmark_analyzer import read_toc
from backend.app.services.generation_service import child_main
from backend.app.services.pdf_splitter import volume_toc
from backend.app.services.pdf_validator import page_fingerprints, validate_coverage, validate_volume
from backend.app.services.section_analyzer import measure_sections, sections_for_level
from backend.app.services.volume_planner import plan_volumes
from backend.app.utils.file_utils import read_json, write_json_atomic


def _job(pdf, job_dir, mode="max_size", max_bytes=None, num=None, level=1, compression=None):
    doc = fitz.open(str(pdf))
    entries, _ = read_toc(doc)
    sections, _ = sections_for_level(entries, doc.page_count, level)
    sections = measure_sections(doc, sections, {})
    plan = plan_volumes(sections, mode, max_bytes, num)
    spec = {
        "job_id": "x" * 32, "document_id": "y" * 32, "source_path": str(pdf),
        "source_size": pdf.stat().st_size, "page_count": doc.page_count, "client": "Client",
        "zip_name": "Client_Audit_Report_Volumes.zip", "preserve_bookmarks": True,
        "max_bytes": max_bytes, "num_volumes_requested": num, "mode": mode,
        "compression": (compression or CompressionSettings()).model_dump(), "gs_path": None, "gs_timeout": 60,
        "toc": [[e.level, e.title, e.page, e.parent] for e in entries],
        "sections": [{"title": s.title, "start_page": s.start_page, "end_page": s.end_page, "protected": s.protected}
                     for s in sections],
        "volumes": [{"index": v.index, "start_page": v.start_page, "end_page": v.end_page,
                     "sections": [s.model_dump() for s in v.sections],
                     "filename": f"Client_Audit_Report_Volume_{v.index:02d}.pdf"} for v in plan.volumes],
        "plan_warnings": plan.warnings,
    }
    doc.close()
    job_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(job_dir / "job.json", spec)
    child_main(str(job_dir), "generate")
    return spec, read_json(job_dir / "status.json")


def test_generated_volumes_are_valid(ten_section_pdf, tmp_path):
    """Test 1 + Test 10: grouped without splitting, every output opens and has exactly its pages."""
    size = ten_section_pdf.stat().st_size
    spec, status = _job(ten_section_pdf, tmp_path / "job", max_bytes=int(size / 3))
    assert status["state"] == "completed", status
    assert status["summary"]["validation_passed"]
    assert len(status["volumes"]) >= 3
    src = fitz.open(str(ten_section_pdf))
    for v in status["volumes"]:
        assert v["status"] == "PASS", v["validation"]
        out = fitz.open(str(tmp_path / "job" / "out" / v["filename"]))
        assert out.page_count == v["end_page"] - v["start_page"] + 1
        # Exactly the intended pages, in order: compare page text with the source.
        for i in range(out.page_count):
            assert out[i].get_text() == src[v["start_page"] - 1 + i].get_text()
        # Bookmarks point inside the volume and match the source titles.
        toc = out.get_toc()
        assert toc and all(1 <= t[2] <= out.page_count for t in toc)
        out.close()
    assert all(c["ok"] for c in status["summary"]["coverage"])
    src.close()


def test_compression_inside_generation_and_conflict(make, tmp_path):
    """One Annexure much larger than the limit: compressed, and if impossible reported - never split."""
    pdf = make("conf.pdf", [("Main Report", 2, 1), ("Annexure F", 4, 1)], image_px=500)
    spec, status = _job(pdf, tmp_path / "job", max_bytes=3_000)
    assert status["state"] == "completed"
    over = [v for v in status["volumes"] if v["status"] == "OVER_LIMIT"]
    assert over, status["volumes"]
    assert status["summary"]["ready"] is False
    assert status["summary"]["unresolved"]
    # Annexure F is still whole in a single volume
    holder = [v for v in status["volumes"] if any(s["title"] == "Annexure F" for s in v["sections"])]
    assert len(holder) == 1 and holder[0]["page_count"] == 4
    assert all(c["ok"] for c in status["summary"]["coverage"])


def test_volume_toc_remaps_and_recreates_parents():
    toc = [
        [1, "Audit Report", 1, None],
        [2, "Main Report", 1, 0],
        [2, "Annexure A", 10, 0],
        [3, "A-1", 12, 2],
        [2, "Annexure B", 20, 0],
    ]
    assert volume_toc(toc, 10, 19) == [
        [1, "Audit Report (continued)", 1],
        [2, "Annexure A", 1],
        [3, "A-1", 3],
    ]
    assert volume_toc(toc, 20, 25) == [[1, "Audit Report (continued)", 1], [2, "Annexure B", 1]]
    assert volume_toc(toc, 1, 9) == [[1, "Audit Report", 1], [2, "Main Report", 1]]


def test_validator_detects_wrong_page_order(make, tmp_path):
    pdf = make("order.pdf", [("A", 4, 1)])
    src = fitz.open(str(pdf))
    fps = page_fingerprints(src)
    bad = fitz.open()
    bad.insert_pdf(src, from_page=1, to_page=1)
    bad.insert_pdf(src, from_page=0, to_page=0)
    bad.insert_pdf(src, from_page=2, to_page=3)
    out = tmp_path / "bad.pdf"
    bad.save(str(out))
    bad.close()
    res = validate_volume(out, src, 1, 4, fps, "strict", None, None, [])
    assert not res["passed"]
    res_visual = validate_volume(out, src, 1, 4, fps, "visual", None, None, [])
    # Text-only pages look alike as tiny thumbnails; the text check is what catches this case.
    assert any(c["name"].startswith("Page content") for c in res_visual["checks"])
    src.close()


def test_validator_detects_missing_page(make, tmp_path):
    pdf = make("miss.pdf", [("A", 4, 1)])
    src = fitz.open(str(pdf))
    short = fitz.open()
    short.insert_pdf(src, from_page=0, to_page=2)
    out = tmp_path / "short.pdf"
    short.save(str(out))
    short.close()
    res = validate_volume(out, src, 1, 4, page_fingerprints(src), "strict", None, None, [])
    assert not res["passed"]
    src.close()


def test_coverage_checks():
    prot = [{"title": "Annexure C", "start_page": 5, "end_page": 8, "protected": True}]
    ok = validate_coverage([(1, 4), (5, 10)], 10, prot)
    assert all(c["ok"] for c in ok)
    split = validate_coverage([(1, 6), (7, 10)], 10, prot)
    assert not next(c for c in split if c["name"] == "No protected section split")["ok"]
    missing = validate_coverage([(1, 4), (6, 10)], 10, [])
    assert not missing[0]["ok"]
    dup = validate_coverage([(1, 5), (5, 10)], 10, [])
    assert not dup[1]["ok"]


# ----------------------------------------------------------------------------- API flow

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.app import config
    from backend.app.services import session_store

    monkeypatch.setattr(session_store.store, "root", tmp_path / "store")
    with TestClient(__import__("backend.app.main", fromlist=["app"]).app) as c:
        yield c
    _ = config


def test_api_end_to_end(client, nested_pdf):
    with open(nested_pdf, "rb") as fh:
        r = client.post("/api/upload", files={"file": ("Client Report.pdf", fh, "application/pdf")})
    assert r.status_code == 200, r.text
    doc_id = r.json()["document"]["document_id"]

    r = client.post("/api/analyze", json={"document_id": doc_id})
    assert r.status_code == 200, r.text
    analysis = r.json()
    assert analysis["selected_level"] == 2
    assert [s["title"] for s in analysis["sections"]][1:] == ["Main Report", "Annexure A", "Annexure B", "Annexure C"]

    r = client.post("/api/plan", json={"document_id": doc_id, "sections": analysis["sections"],
                                       "mode": "num_volumes", "num_volumes": 3})
    assert r.status_code == 200, r.text
    plan = r.json()
    assert len(plan["volumes"]) == 3

    r = client.post("/api/generate", json={"plan_id": plan["plan_id"], "client_name": "Acme Ltd"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    deadline = time.time() + 120
    while time.time() < deadline:
        st = client.get(f"/api/status/{job_id}").json()
        if st["state"] in ("completed", "failed", "cancelled") and not st["active"]:
            break
        time.sleep(0.5)
    assert st["state"] == "completed", st
    assert st["summary"]["ready"]
    v1 = st["volumes"][0]
    assert v1["filename"] == "Acme_Ltd_Audit_Report_Volume_01.pdf"
    r = client.get(f"/api/download/{v1['volume_id']}")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    r = client.get(f"/api/download-zip/{job_id}")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert "Acme_Ltd_Audit_Report_Volumes.zip" in r.headers["content-disposition"]
    import io, zipfile
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert names == [v["filename"] for v in st["volumes"]]  # volume PDFs only, nothing else
    rep = client.get(f"/api/report/{job_id}")                 # report is a separate, optional download
    assert rep.status_code == 200 and "VALIDATION REPORT" in rep.text
    assert "Acme_Ltd_Validation_Report.txt" in rep.headers["content-disposition"]
    for v in st["volumes"]:                                    # bookmarks always preserved
        assert v["validation"]["passed"]

    r = client.delete(f"/api/documents/{doc_id}")
    assert r.json()["deleted"]
    assert client.get(f"/api/status/{job_id}").status_code == 404


def test_api_rejects_non_pdf_and_traversal(client, tmp_path):
    r = client.post("/api/upload", files={"file": ("../../evil.pdf", b"not a pdf at all", "application/pdf")})
    assert r.status_code == 400
    r = client.post("/api/upload", files={"file": ("x.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400
    assert client.get("/api/download/..%2F..%2Fsecret").status_code == 404
    assert client.get("/api/status/not-an-id").status_code == 404


def test_api_password_protected(client, tmp_path):
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "secret")
    path = tmp_path / "locked.pdf"
    doc.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="pw123", owner_pw="own")
    doc.close()
    data = path.read_bytes()
    r = client.post("/api/upload", files={"file": ("locked.pdf", data, "application/pdf")})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "password_required"
    r = client.post("/api/upload", files={"file": ("locked.pdf", data, "application/pdf")}, data={"password": "wrong"})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "password_incorrect"
    r = client.post("/api/upload", files={"file": ("locked.pdf", data, "application/pdf")}, data={"password": "pw123"})
    assert r.status_code == 200 and r.json()["document"]["was_encrypted"]


def test_api_no_bookmarks_warning(client, make):
    pdf = make("nob.pdf", [("Main", 4, 1)], bookmarks=False)
    with open(pdf, "rb") as fh:
        doc_id = client.post("/api/upload", files={"file": ("nob.pdf", fh, "application/pdf")}).json()["document"]["document_id"]
    a = client.post("/api/analyze", json={"document_id": doc_id}).json()
    assert not a["has_usable_bookmarks"]
    assert a["warnings"][0].startswith("No usable bookmarks were detected. Automatic Annexure-safe splitting cannot be guaranteed.")
    assert a["sections"][0]["protected"] is False
    r = client.post("/api/sections/measure", json={"document_id": doc_id, "sections": [
        {"title": "Main", "start_page": 1, "end_page": 2}, {"title": "Annexure A", "start_page": 3, "end_page": 4}]})
    assert r.status_code == 200 and len(r.json()["sections"]) == 2


def test_form_fields_keep_their_visible_appearance(tmp_path):
    """Real-world case: merged reports with signature/form fields sharing a name.

    Page copying can drop such fields; the pipeline bakes them into page content first so the
    visible stamp (and its text) is present in the output volumes.
    """
    merged = fitz.open()
    for i in range(2):
        part = fitz.open()
        page = part.new_page()
        page.insert_text((72, 72), f"Part {i} body")
        w = fitz.Widget()
        w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        w.field_name = "Signature1"  # same name in both parts, as in merged signed documents
        w.field_value = f"Signed by Partner {i}"
        w.rect = fitz.Rect(72, 100, 300, 130)
        page.add_widget(w)
        merged.insert_pdf(part)
        part.close()
    merged.set_toc([[1, "Main Report", 1], [1, "Annexure A", 2]])
    pdf = tmp_path / "signed.pdf"
    merged.save(str(pdf))
    merged.close()

    spec, status = _job(pdf, tmp_path / "job", mode="num_volumes", num=2)
    assert status["state"] == "completed", status
    assert any("form/signature field" in e["msg"] for e in status["events"])
    for i, v in enumerate(status["volumes"]):
        assert v["status"] == "PASS", v["validation"]
        out = fitz.open(str(tmp_path / "job" / "out" / v["filename"]))
        assert f"Signed by Partner {i}" in out[0].get_text()
        out.close()


def test_failed_stronger_run_keeps_completed_results(make, tmp_path):
    """A failed/cancelled 'try stronger compression' must not lose the finished volumes."""
    from backend.app.services.generation_service import StatusWriter, _finish_with_problem
    pdf = make("keep.pdf", [("Main Report", 2, 1), ("Annexure F", 3, 1)], image_px=400)
    spec, status = _job(pdf, tmp_path / "job", max_bytes=3_000)
    assert status["state"] == "completed"
    work_files = sorted((tmp_path / "job" / "work").iterdir())
    assert work_files  # saved "smallest" / "original quality" versions
    writer = StatusWriter(tmp_path / "job")
    writer.set(state="running")
    _finish_with_problem(tmp_path / "job", writer, "stronger", "failed", "Not enough memory.")
    after = read_json(tmp_path / "job" / "status.json")
    assert after["state"] == "completed"
    assert after["volumes"] == status["volumes"]
    assert sorted((tmp_path / "job" / "work").iterdir()) == work_files
    assert "previous result was kept" in after["events"][-1]["msg"]


def test_plan_size_check_respects_compression_settings(client, nested_pdf):
    with open(nested_pdf, "rb") as fh:
        doc_id = client.post("/api/upload", files={"file": ("n.pdf", fh, "application/pdf")}).json()["document"]["document_id"]
    a = client.post("/api/analyze", json={"document_id": doc_id}).json()
    base = {"document_id": doc_id, "sections": a["sections"], "mode": "both", "max_size": 1, "size_unit": "KB", "num_volumes": 2}
    strict = client.post("/api/plan", json={**base, "volume_starts": [1, 20]})
    assert strict.status_code == 422 and "not possible within the required size" in strict.json()["detail"]
    relaxed = client.post("/api/plan", json={**base, "volume_starts": [1, 20],
                                             "compression": {"preserve_text": "required"}})
    assert relaxed.status_code == 200
    assert any("cannot be checked in advance" in w for w in relaxed.json()["warnings"])


def test_password_protection(client, monkeypatch):
    import base64
    import dataclasses
    from backend.app import main
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, app_username="audit", app_password="s3cret"))
    assert client.get("/api/health").status_code == 200                       # open for platform health checks
    r = client.get("/")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    bad = base64.b64encode(b"audit:wrong").decode()
    assert client.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    good = base64.b64encode(b"audit:s3cret").decode()
    assert client.get("/", headers={"Authorization": f"Basic {good}"}).status_code == 200
    assert client.get("/api/compression/info", headers={"Authorization": f"Basic {good}"}).status_code == 200
