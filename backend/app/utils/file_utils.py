"""Safe temporary-file handling.

Nothing user-supplied is ever used as a path component: every folder/file name on disk is
either a fixed name or a random hex id generated here.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path

ID_RE = re.compile(r"^[a-f0-9]{32}$")
PDF_MAGIC = b"%PDF-"


def new_id() -> str:
    return uuid.uuid4().hex


def is_valid_id(value: str) -> bool:
    return bool(value) and bool(ID_RE.match(value))


def safe_name_part(value: str, default: str = "Document", max_len: int = 60) -> str:
    """Turn arbitrary text into a safe file-name fragment (letters, digits, _ and -)."""
    value = os.path.basename(str(value or "").replace("\\", "/"))
    value = re.sub(r"\.pdf$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_-")
    value = re.sub(r"_+", "_", value)
    return (value[:max_len].strip("_-")) or default


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def purge_dir_contents(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir():
            remove_tree(child)
        else:
            try:
                child.unlink()
            except OSError:
                pass


def looks_like_pdf(first_bytes: bytes) -> bool:
    # The PDF header may be preceded by a little junk; readers accept it within the first 1 KB.
    return PDF_MAGIC in first_bytes[:1024]


def free_disk_bytes(path: Path) -> int:
    ensure_dir(path)
    return shutil.disk_usage(path).free


def write_json_atomic(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: reader has the file open for a moment
            import time

            time.sleep(0.05)
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    for _ in range(20):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (PermissionError, json.JSONDecodeError):
            import time

            time.sleep(0.05)
    return default
