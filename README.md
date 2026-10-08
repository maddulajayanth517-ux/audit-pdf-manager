---
title: Audit PDF Volume Manager
emoji: 📑
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 8000
pinned: false
short_description: Split audit PDFs into size-limited volumes without splitting Annexures
---

# Audit PDF Volume & Annexure Manager

A local web application that turns one large audit-report PDF into client-compliant **volumes**
(maximum file size and/or exact number of volumes) **without ever splitting an Annexure or any
other protected bookmarked section**. If a volume is too large, it is compressed step by step,
and the real file size is measured after each step, until it fits.

All PDF processing is deterministic Python code on your own computer or server (PyMuPDF, Pillow,
pypdf, optional Ghostscript). Document contents are never sent to an AI or any external service.

---

## Business rules (enforced in code)

| # | Rule | Where it is enforced |
|---|------|----------------------|
| 1 | A protected section is never split | `volume_planner.py` treats each protected section as one unit; `_assert_plan_integrity` and the validator re-check it |
| 2 | Original page order is never changed | Volumes are contiguous page ranges; partitioning never reorders units |
| 3 | Oversized volumes are compressed automatically | `compression_engine.compress_volume` |
| 4/5 | File size takes priority over visual quality | The compression ladder goes down to 72 DPI greyscale raster (and lower on request) |
| 6 | Actual file size is measured, not estimated | Every attempt is written to disk and `stat()`-ed |
| 7 | If the limit is impossible, report it and keep the section whole | Volume status `OVER_LIMIT`, user chooses what to do |
| 8 | No lost, duplicated or corrupted pages | Validator: page count, page sizes, text/visual fingerprints, two independent PDF parsers, coverage check across volumes |
| 9 | Bookmarks are preserved and renumbered per volume | `pdf_splitter.volume_toc` |
| 10 | Secure temporary handling | Random temp folders, automatic deletion, no document text in logs |

Decision priority used everywhere: **(1) never break protected sections → (2) never lose or reorder
pages → (3) required number of volumes → (4) maximum file size → (5) aggressive compression →
(6) quality.**

---

## Quick start

### Windows

1. Install **Python 3.10 or newer** from <https://www.python.org/downloads/> (tick *Add python.exe to PATH*).
2. Optional: install Ghostscript (see below) for an extra compression strategy.
3. Double-click **`run.bat`**. The first start creates a virtual environment and installs the
   dependencies; then your browser opens <http://127.0.0.1:8000>.

Manual alternative (PowerShell, in this folder):

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

### Linux / macOS

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
# or simply: ./run.sh
```

### Docker

```bash
docker compose up --build
# open http://127.0.0.1:8000
```

The image already contains Ghostscript. Temporary files are kept in a RAM-backed `tmpfs`, so
nothing is written to the host disk. Raise the `tmpfs` size in `docker-compose.yml` for very large PDFs.

---

## Deploying online for free (Hugging Face Spaces)

A free Hugging Face Space (Docker, CPU basic: 2 vCPU, 16 GB RAM) runs this project unchanged. The
`---` block at the top of this README tells Hugging Face that it is a Docker app on port 8000.

1. Create a free account at <https://huggingface.co/join>.
2. **New Space** (<https://huggingface.co/new-space>): name `audit-pdf-manager`, SDK **Docker → Blank**,
   hardware **CPU basic (free)**. *Public* is fine: the code holds no secrets, and the app itself is
   password-protected.
3. In the Space, open **Settings → Variables and secrets → New secret**, and add `APP_PASSWORD` with a
   strong password (optionally also `APP_USERNAME`; the default is `audit`). Do this **before**
   uploading, so the app never runs without a login.
4. Create a token with **write** access at <https://huggingface.co/settings/tokens>, then upload
   from this folder:
   ```powershell
   hf auth login
   hf upload spaces/<your-username>/audit-pdf-manager . . --repo-type space `
     --exclude ".venv/*" --exclude "**/__pycache__/*" --exclude ".pytest_cache/*" `
     --exclude ".env" --exclude "*.pdf" --exclude "*.zip"
   ```
5. The Space builds the image (a few minutes; progress is in the **Logs** tab). Then open the
   **direct link** `https://<your-username>-audit-pdf-manager.hf.space`, not the huggingface.co page:
   the login prompt does not work inside Hugging Face's embedded view. Share that link and the password.

To update the deployment later, run the same `hf upload` command again.

Free-tier notes: the Space sleeps after 48 hours without use, and the first visit after that takes
1–2 minutes to wake it. Storage is temporary (uploads are deleted anyway). 2 vCPU is slower than a
16-core desktop, roughly 2–4× for heavy compression.

## Ghostscript (optional)

The application works fully without Ghostscript (PyMuPDF + Pillow compression). When Ghostscript
is installed it is detected automatically and used as an extra text-preserving strategy
(`/ebook` at level 2 and `/screen` at level 4). Whether it was found is reported by
`GET /api/compression/info`.

| Platform | Installation |
|----------|--------------|
| **Windows** | Download the 64-bit installer from <https://ghostscript.com/releases/gsdnld.html> and install with the defaults. The app looks in `C:\Program Files\gs\gs*\bin\gswin64c.exe` and on `PATH`. Restart the app afterwards. To use another location, set `GHOSTSCRIPT_PATH` in `.env`. |
| **Debian/Ubuntu** | `sudo apt-get install ghostscript` |
| **RHEL/Fedora** | `sudo dnf install ghostscript` |
| **macOS** | `brew install ghostscript` |
| **Docker** | Already included (`apt-get install ghostscript` in the `Dockerfile`). Remove that line if you do not want it. |

---

## How to use

1. **Upload** the PDF (drag and drop). Password-protected PDFs ask for the password; the
   decrypted copy exists only in the temporary folder.
2. **Bookmark analysis.** The bookmark tree is shown and the level that most likely holds the
   Annexures is pre-selected. Every bookmark at that level (or above) starts a **protected
   section**. A level-1 Annexure without children still becomes its own section when level 2 is
   selected. Pages before the first bookmark become a protected *Front pages* section.
   - Change the level if needed.
   - Untick **Protected** for a section that may be split at a page boundary.
   - **Advanced: define protected sections manually** when bookmarks are missing or wrong.
     Pages not covered by any row become unprotected sections, so no page is ever dropped.
     Overlapping ranges are rejected.
3. **Client requirements.** Choose *Maximum Size*, *Number of Volumes* or *Both*, the size
   (KB/MB/GB), the number of volumes and the client name used for the file names.
4. **Plan volumes.** Review the proposed volumes and their estimated sizes. Volumes that will need
   compression are marked. To regroup, use **◀ Move first section to Vol N** or **Move last
   section to Vol N ▶** on a volume card. This moves a whole section to the neighbouring volume:
   only the boundary moves, so section order cannot change, and a volume can never start inside a
   protected section (the server rejects it). **Reset to automatic plan** undoes the changes.
   Moves are **checked against the required size**: for every section the app measures the smallest
   size it can reach at the strongest automatic compression, by really compressing up to 5 sample
   pages. If a move would create a volume that cannot reach the limit even then, it is refused and
   the plan stays unchanged. Each card shows “Smallest reachable (max. compression)”.
5. **Generate.** Each volume is built, measured, compressed if needed, measured again and
   validated. Progress is shown live and can be cancelled.
6. **Results.** Summary table, per-volume validation checklist, downloads per volume, and the ZIP of
   all volumes (volume PDFs only, ready to submit). The validation report is a separate, optional
   download (**Download validation report (optional)**) for your own records.
   If a protected section cannot be brought under the limit, you see:
   *“Annexure F cannot currently be reduced below 25 MB without splitting the protected section.”*
   and can choose to **try stronger compression**, **allow the volume to exceed the limit**
   (smallest version or original quality), **review the sections**, or **cancel**.
7. **Finish & delete files** removes the upload and all outputs immediately. Otherwise they are
   deleted automatically after `FILE_TTL_MINUTES` of inactivity, and on server start and stop.

### File-size units

**1 MB = 1,000,000 bytes** (decimal; KB = 1,000 and GB = 1,000,000,000) everywhere in the
application. Decimal is the stricter reading of a client limit: 25 MB = 25,000,000 bytes, which is
less than 25 MiB = 26,214,400 bytes. A volume that passes here therefore also passes a portal that
measures in binary units. (Windows Explorer shows binary sizes labelled “MB”, so it shows a
25,000,000-byte file as 23.8 MB.) Exact byte counts are shown next to sizes.

---

## How it works

### Volume planning (`services/volume_planner.py`)

Sections become **units**: each protected section is one unit; an unprotected section is broken
into one-page units. Volumes are always **contiguous runs of units in document order**.

* **Maximum size**: a greedy pass gives the minimum number of volumes (a unit larger than the limit
  becomes a volume of its own and is marked for compression). A binary search then finds the
  smallest possible largest volume for that count, and a dynamic programme picks, among all
  partitions within that bound, the one with the most even sizes.
* **Number of volumes**: the same min-max + balancing approach with exactly *N* volumes. If there
  are fewer indivisible units than *N*, fewer volumes are produced with a warning (never a split).
* **Both**: if *N* volumes can respect the limit, the plan respects both. Otherwise the number of
  volumes wins (priority 3 over 4), and oversized volumes are compressed.

Sizes used for planning are **measured** by extracting each section into its own PDF. The real
size of each volume is measured again during generation.

### Compression ladder (`services/compression_engine.py`)

Every attempt starts again from the original pages, so quality is never lost twice. The first
attempt whose **actual file size** is within the limit is accepted:

| Level | Strategy | Text searchable |
|------:|----------|:---:|
| 0 | Original pages, no compression | yes |
| 1 | Lossless optimisation: unused objects removed, streams and fonts deflated, object streams, font subsetting, metadata stripped | yes |
| 2 | Image recompression 200 DPI/q90 → 150/q80 → 150/q70, then Ghostscript `/ebook` | yes |
| 3 | Images 120 DPI/q60, then **rasterise** pages 150 DPI/q60 | yes, then no |
| 4 | Images 96 DPI/q50, Ghostscript `/screen`, rasterise 120 DPI/q45 | yes, then no |
| 5 | Images 72 DPI/q40 greyscale, rasterise 96 DPI/q35 greyscale | yes, then no |
| 6 | **Emergency**: rasterise 72 DPI/q25 then q15, greyscale | no |
| 7 | **Floor** (still automatic): rasterise 60 → 48 → 36 DPI greyscale, JPEG 12 → 5 | no |
| 8 | *Try stronger compression* (only on request): rasterise 30 / 24 DPI greyscale | no |

At each level the text-preserving variant is tried before rasterisation. When a result is still
more than twice the target, the gentler variants of that level are skipped. The last emergency
step is always tried before the attempt budget (default 15) runs out. The required file size has
priority over quality, so automatic mode goes all the way to the floor (level 7) without asking.
There are deliberately no quality settings in the interface: each one could only limit compression and
make it harder to meet the client's size. The only choice is the **Compression** option: *Automatic*
(default), *Lossless optimisation only* (never reduces quality, may miss the limit) or *Off*.
Levels 5 and above are flagged in the UI:
*“Extreme compression applied — document quality may be significantly reduced.”*

### Speed

Generation uses all CPU cores (setting `WORKERS_PER_JOB`; default: cores − 1, at most 8, fewer when free
memory is low):

* **All volumes are processed at the same time.**
* **Size prediction:** level 0 is tried first. If the volume is still too big, every level is tried on
  6 sample pages (a fraction of a second each), and levels predicted to stay far above the limit are
  skipped. Only the promising levels are built at full size, in parallel.
* **Early stop:** as soon as the gentlest level that meets the limit is known, the volume's other
  attempts are cancelled.
* Page fingerprints are read in the background, and all volumes are validated in parallel.

The result is the same file a one-by-one search would choose: the real size of every candidate is
measured, and the gentlest real success wins. On a 16-core PC, a 100 MB report into 3 × 4 MB volumes
went from about 40 s to about 12 s.

### Validation (`services/pdf_validator.py`)

For every volume:

* opens without repair (PyMuPDF) **and** is readable by pypdf, as an independent second parser
* exact page count
* page sizes match the original pages in order
* page content matches the original page at the same position: text fingerprints (exact for
  PyMuPDF output, warning-level for Ghostscript, which re-encodes text) or, for rasterised
  pages, visual thumbnail comparison
* sample pages render
* every bookmark points inside the volume, and the bookmark count is as expected
* all protected sections in the range are complete
* actual size compared with the limit (exact bytes)

Across volumes: no page missing, no page duplicated, original order, no protected section split.

### Bookmarks in volumes

Only bookmarks whose target page lies in the volume are kept, renumbered to the volume's page
numbers. If a kept bookmark's parent started in an earlier volume, the parent is recreated as
“*Title (continued)*” so the hierarchy stays valid.

---

## API

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/api/upload` | multipart `file` (+ optional `password`) → document info |
| POST | `/api/analyze` | `{document_id, level?}` → bookmark tree, level stats, sections with sizes |
| POST | `/api/sections/measure` | validate and measure manually defined sections |
| POST | `/api/plan` | `{document_id, sections, mode, max_size, size_unit, num_volumes}` → volume plan |
| POST | `/api/generate` | `{plan_id, client_name, preserve_bookmarks, compression}` → `job_id` |
| GET | `/api/status/{job_id}` | progress log, per-volume results, summary |
| POST | `/api/cancel/{job_id}` | cancel a running job |
| POST | `/api/jobs/{job_id}/volumes/{n}/resolve` | `{action: stronger \| accept_smallest \| accept_original}` |
| GET | `/api/download/{job_id}-{n}` | one volume |
| GET | `/api/download-zip/{job_id}` | all volumes (PDFs only) |
| GET | `/api/report/{job_id}` | validation report (text), optional separate download |
| GET | `/api/compression/info` | Ghostscript status, defaults, compression ladder |
| DELETE | `/api/documents/{document_id}` | delete the upload and every output now |

Interactive API documentation: <http://127.0.0.1:8000/docs>.

Generation runs in a **separate worker process** per job (`services/job_manager.py`), so large PDFs
never freeze the web server, and a crash from running out of memory only affects that job.

---

## Project structure

```
backend/app/
  main.py                FastAPI app, security headers, static frontend
  config.py              settings from environment / .env
  routers/               upload, analysis, volumes (plan/generate/status/cancel/resolve),
                         compression (info), downloads (volume/zip/report/delete)
  services/
    pdf_reader.py        safe opening, password handling, save helpers
    bookmark_analyzer.py outline reading, hierarchy repair, level suggestion
    section_analyzer.py  bookmark → protected sections, manual sections, size measurement
    volume_planner.py    contiguous min-max / balanced partitioning
    pdf_splitter.py      page-range extraction, bookmark remapping
    compression_engine.py progressive compression ladder, Ghostscript detection
    pdf_validator.py     per-volume and cross-volume validation
    generation_service.py worker-process pipeline (split → compress → validate)
    job_manager.py       worker processes, cancellation, crash detection
    session_store.py     temp-folder registry and automatic cleanup
    report_service.py    validation report text
    zip_service.py       ZIP packaging with integrity check
  models/                Pydantic request/response models
  utils/                 safe file names, temp files, size units
frontend/                index.html, styles.css, app.js (no build step)
tests/                   pytest suite (synthetic PDFs are generated on the fly)
```

The frontend is plain HTML, CSS and JavaScript served by FastAPI, so no Node.js toolchain is needed.
Because all logic sits behind the JSON API, a React or Next.js frontend can replace it later
without changing the backend.

---

## Security & privacy

* Uploads are written only to a random per-document folder under `APP_TEMP_DIR`. Nothing
  user-supplied is used as a path: uploaded file names are used only for display and, after
  sanitising, for output file names.
* Files are deleted on **Finish & delete files**, after `FILE_TTL_MINUTES` of inactivity, on
  cancellation or failure (work files), and when the server starts or stops.
* Uploaded files are never served back. Only generated volumes can be downloaded, through
  unguessable 128-bit job ids.
* Upload validation: `.pdf` extension, `%PDF` header, maximum size (`MAX_UPLOAD_MB`), free-disk check.
* Logs contain only ids, page counts and sizes, never document text. MuPDF console warnings
  are turned off. Error details keep only code locations, not exception messages.
* Security headers are set (CSP, `nosniff`, `no-store` for API responses, framing denied).
* By default the server listens on `127.0.0.1` only. To share it on an office network, bind to
  `0.0.0.0` behind a reverse proxy with HTTPS and authentication. The application has no user
  accounts of its own.

---

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The suite builds synthetic PDFs (noise images make sizes predictable) and covers the specified
scenarios: 100 pages and 10 sections grouped without splitting; an oversized Annexure compressed
to target; an impossible target reported without splitting; exact volume count, including when it
is impossible; both constraints, including when they conflict; no bookmarks; a scanned PDF; nested
bookmarks; multiple compression levels; output validation. It also covers brute-force optimality
of the partitioning, randomised plan invariants, bookmark remapping, detection of reordered or
missing pages, password-protected PDFs, path traversal, and a full API run from upload to ZIP.

---

## Troubleshooting

| Problem | Explanation / fix |
|---------|-------------------|
| “No usable bookmarks were detected…” | The PDF has no outline (or none with valid destinations). Define the protected ranges manually, or accept page-level splitting of the unprotected document. |
| A volume stays over the limit | One protected section is larger than the limit even after emergency compression. Choose *Try stronger compression*, *Allow exceed*, or review the sections (for example, unprotect a section that may be split). |
| Very large PDFs | Allow about 4× the PDF size in free disk space, plus enough memory for one page at a time during rasterisation. Increase `MAX_UPLOAD_MB` if needed. |
| Text no longer searchable | Rasterisation (level 3 or higher) was required to meet the limit. The size limit has priority over quality. Choose *Compression: Lossless optimisation only* to keep the original quality, at the cost of possibly not meeting the limit. |
| “form/signature field(s) were converted to static page content” | Digitally signed PDFs: a digital signature covers the whole original file, so it can never stay cryptographically valid once the PDF is split. Copying signature fields between documents is also unreliable (stamps can vanish), so the app turns them into ordinary page content before splitting. The visible stamp is pixel-identical; validation compares against this prepared copy. Keep the original signed PDF if the signature itself must be verified. |
| Ghostscript not detected | Install it (see above) and restart, or set `GHOSTSCRIPT_PATH`. |
