"""GET /api/compression/info - compression capabilities and defaults."""
from __future__ import annotations

from fastapi import APIRouter

from ..config import settings
from ..models.volume_models import CompressionSettings
from ..services.compression_engine import describe_ladder, find_ghostscript
from ..utils.size_utils import UNIT_NOTE

router = APIRouter(prefix="/api", tags=["compression"])


@router.get("/compression/info")
def compression_info():
    gs_path, gs_version = find_ghostscript(settings.ghostscript_path)
    defaults = CompressionSettings()
    return {
        "ghostscript": {"available": bool(gs_path), "version": gs_version},
        "defaults": defaults.model_dump(),
        "ladder": describe_ladder(defaults.model_dump(), bool(gs_path)),
        "unit_note": UNIT_NOTE,
        "max_upload_mb": settings.max_upload_mb,
        "file_ttl_minutes": settings.file_ttl_minutes,
    }
