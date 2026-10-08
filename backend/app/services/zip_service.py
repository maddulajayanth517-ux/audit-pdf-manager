"""ZIP packaging of generated volumes."""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Iterable, Optional, Tuple


def build_zip(files: Iterable[Tuple[Path, str]], out_path: Path, report_text: Optional[str] = None) -> Path:
    """Write ``files`` ((path, name-inside-zip) pairs) to ``out_path``.

    PDFs are already compressed, so they are stored without recompression (fast, same size).
    """
    tmp = out_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for path, arcname in files:
            zf.write(path, arcname=arcname)
        if report_text:
            zf.writestr("Validation_Report.txt", report_text, compress_type=zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(tmp) as zf:  # integrity check before publishing
        if zf.testzip() is not None:
            raise RuntimeError("ZIP integrity check failed.")
    tmp.replace(out_path)
    return out_path
