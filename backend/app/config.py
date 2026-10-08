"""Application configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv() -> None:
    """Minimal .env loader (no extra dependency). Existing env vars win."""
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"):
        if candidate.is_file():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
            break


_load_dotenv()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Root folder for all temporary working files. Each upload gets its own random sub-folder.
    temp_root: Path = field(
        default_factory=lambda: Path(
            os.environ.get("APP_TEMP_DIR") or Path(tempfile.gettempdir()) / "audit_pdf_manager"
        )
    )
    # Maximum accepted upload, in MB (1 MB = 1,000,000 bytes).
    max_upload_mb: int = field(default_factory=lambda: _env_int("MAX_UPLOAD_MB", 1000))
    # Uploaded documents and generated volumes are deleted this many minutes after last use.
    file_ttl_minutes: int = field(default_factory=lambda: _env_int("FILE_TTL_MINUTES", 60))
    # Delete everything under temp_root when the server starts (leftovers from a crash).
    purge_on_startup: bool = field(default_factory=lambda: _env_bool("PURGE_ON_STARTUP", True))
    # Explicit path to the Ghostscript executable (optional; auto-detected otherwise).
    ghostscript_path: str = field(default_factory=lambda: os.environ.get("GHOSTSCRIPT_PATH", ""))
    ghostscript_timeout_s: int = field(default_factory=lambda: _env_int("GHOSTSCRIPT_TIMEOUT_S", 600))
    # Maximum number of generation jobs running at the same time (each runs in its own process).
    max_concurrent_jobs: int = field(default_factory=lambda: _env_int("MAX_CONCURRENT_JOBS", 2))
    # Parallel compression worker processes per job (0 = automatic: CPU cores - 1, max 8,
    # reduced automatically when free memory is low).
    workers_per_job: int = field(default_factory=lambda: _env_int("WORKERS_PER_JOB", 0))
    # Extra free disk space required, as a multiple of the source PDF size.
    disk_space_factor: int = field(default_factory=lambda: _env_int("DISK_SPACE_FACTOR", 4))
    cors_origins: str = field(default_factory=lambda: os.environ.get("CORS_ORIGINS", ""))
    # Password protection (HTTP Basic). Strongly recommended whenever the app is reachable from
    # the internet. Empty password = no login (fine for 127.0.0.1 only).
    app_username: str = field(default_factory=lambda: os.environ.get("APP_USERNAME", "audit"))
    app_password: str = field(default_factory=lambda: os.environ.get("APP_PASSWORD", ""))

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1_000_000


settings = Settings()
