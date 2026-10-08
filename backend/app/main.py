"""FastAPI application: API routers + the static web frontend.

Run (from the project root):
    uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .routers import analysis, compression, downloads, upload, volumes
from .services.job_manager import manager
from .services.pdf_reader import PdfProcessingError
from .services.session_store import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("audit_pdf")

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.startup()
    manager.on_tick = lambda: store.cleanup_expired(manager.is_active)
    manager.start()
    log.info("Temporary files: %s (auto-delete after %d min)", settings.temp_root, settings.file_ttl_minutes)
    log.info("Login: %s", "password required" if settings.app_password else "DISABLED (set APP_PASSWORD)")
    try:
        yield
    finally:
        manager.stop()
        store.shutdown()


app = FastAPI(title="Audit PDF Volume & Annexure Manager", version="1.0.0", lifespan=lifespan)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
    )


def _authorized(request: Request) -> bool:
    """HTTP Basic check against APP_USERNAME / APP_PASSWORD (constant-time comparison)."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:].strip()).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    ok_user = secrets.compare_digest(user.encode(), settings.app_username.encode())
    ok_pass = secrets.compare_digest(password.encode(), settings.app_password.encode())
    return ok_user and ok_pass


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # The health check stays open so hosting platforms can see that the app is running.
    if settings.app_password and request.url.path != "/api/health" and not _authorized(request):
        response = JSONResponse(
            status_code=401,
            content={"detail": "Login required."},
            headers={"WWW-Authenticate": 'Basic realm="Audit PDF Manager", charset="UTF-8"'},
        )
    else:
        response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'",
    )
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(PdfProcessingError)
async def pdf_error_handler(request: Request, exc: PdfProcessingError):
    return JSONResponse(status_code=400, content={"detail": {"code": exc.code, "message": exc.message}})


@app.exception_handler(MemoryError)
async def memory_error_handler(request: Request, exc: MemoryError):
    return JSONResponse(status_code=507, content={"detail": "Not enough memory to process this PDF."})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    messages = []
    for err in exc.errors():
        loc = " → ".join(str(x) for x in err.get("loc", []) if x != "body")
        messages.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return JSONResponse(status_code=422, content={"detail": "; ".join(messages)})


@app.get("/api/health")
def health():
    return {"status": "ok"}


for r in (upload.router, analysis.router, volumes.router, compression.router, downloads.router):
    app.include_router(r)

if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
