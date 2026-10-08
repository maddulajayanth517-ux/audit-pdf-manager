"""Models for volume planning, compression settings and generation jobs."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from .pdf_models import Section


class CompressionSettings(BaseModel):
    # automatic: compress only when a volume is over the limit
    # lossless:  always apply lossless structure optimisation, never degrade quality
    # off:       never compress (oversized volumes are reported)
    mode: Literal["automatic", "lossless", "off"] = "automatic"
    # Defaults allow the full automatic ladder (down to 36 DPI greyscale, JPEG 5): the required
    # file size has priority over quality. Raise these to forbid the strongest steps.
    min_dpi: int = Field(36, ge=36, le=300)
    max_dpi: int = Field(300, ge=72, le=600)
    jpeg_quality_min: int = Field(5, ge=5, le=95)
    jpeg_quality_max: int = Field(95, ge=10, le=100)
    allow_grayscale: bool = True
    allow_rasterization: bool = True
    # preferred: keep text while possible, rasterise only if needed; required: never rasterise
    preserve_text: Literal["preferred", "required"] = "preferred"
    use_ghostscript: bool = True

    def reaches_floor(self) -> bool:
        """True if these settings allow the strongest automatic step (36 DPI greyscale raster)."""
        return (self.mode == "automatic" and self.allow_rasterization and self.allow_grayscale
                and self.preserve_text != "required" and self.min_dpi <= 36 and self.jpeg_quality_min <= 5)

    @model_validator(mode="after")
    def _check_ranges(self):
        if self.min_dpi > self.max_dpi:
            raise ValueError("Minimum DPI cannot be larger than maximum DPI.")
        if self.jpeg_quality_min > self.jpeg_quality_max:
            raise ValueError("Minimum JPEG quality cannot be larger than maximum JPEG quality.")
        return self


class PlanRequest(BaseModel):
    document_id: str
    sections: List[Section]
    mode: Literal["max_size", "num_volumes", "both"]
    max_size: Optional[float] = Field(None, gt=0)
    size_unit: Literal["KB", "MB", "GB"] = "MB"
    num_volumes: Optional[int] = Field(None, ge=1, le=500)
    # Optional user adjustment: first page of every volume (must be section start pages).
    volume_starts: Optional[List[int]] = Field(None, max_length=500)
    # Compression settings that will be used for generation; they decide whether the
    # "smallest reachable size" estimate (which assumes the full automatic ladder) applies.
    compression: Optional[CompressionSettings] = None

    @model_validator(mode="after")
    def _check_mode(self):
        if self.mode in ("max_size", "both") and not self.max_size:
            raise ValueError("A maximum volume size is required for this mode.")
        if self.mode in ("num_volumes", "both") and not self.num_volumes:
            raise ValueError("A number of volumes is required for this mode.")
        if not self.sections:
            raise ValueError("At least one section is required.")
        return self


class PlannedSection(BaseModel):
    title: str
    start_page: int
    end_page: int
    protected: bool
    partial: bool = False  # only possible for unprotected sections


class PlannedVolume(BaseModel):
    index: int
    start_page: int
    end_page: int
    page_count: int
    sections: List[PlannedSection]
    estimated_bytes: int
    over_limit: bool = False
    min_estimated_bytes: Optional[int] = None  # estimate at the strongest automatic compression
    achievable: bool = True        # False if even maximum compression is estimated to exceed the limit


class VolumePlan(BaseModel):
    plan_id: str
    document_id: str
    mode: str
    max_bytes: Optional[int]
    num_volumes_requested: Optional[int]
    volumes: List[PlannedVolume]
    warnings: List[str] = []
    constraints_satisfied: bool = True
    total_estimated_bytes: int
    sections: List[Section]
    adjusted: bool = False


class GenerateRequest(BaseModel):
    plan_id: str
    client_name: str = Field("", max_length=120)
    preserve_bookmarks: bool = True  # always on in the UI; kept for API callers
    compression: CompressionSettings = CompressionSettings()


class ResolveRequest(BaseModel):
    action: Literal["stronger", "accept_smallest", "accept_original"]
