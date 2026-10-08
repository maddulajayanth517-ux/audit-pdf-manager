"""Models describing an uploaded PDF, its bookmarks and its protected sections."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class BookmarkNode(BaseModel):
    id: int
    level: int
    title: str
    page: Optional[int] = Field(None, description="1-based target page; None when the destination is missing")
    children: List["BookmarkNode"] = []


class LevelStat(BaseModel):
    level: int
    count: int
    valid: int
    keyword_hits: int


class Section(BaseModel):
    title: str
    start_page: int = Field(..., ge=1)
    end_page: int = Field(..., ge=1)
    protected: bool = True
    path: str = ""
    size_bytes: Optional[int] = None
    # Estimated smallest size at the strongest automatic compression (sample-page measurement).
    min_size_bytes: Optional[int] = None
    source: str = "bookmark"  # bookmark | front-matter | manual | gap | whole-document

    @property
    def page_count(self) -> int:
        return self.end_page - self.start_page + 1


class DocumentInfo(BaseModel):
    document_id: str
    filename: str
    size_bytes: int
    page_count: int
    bookmark_count: int
    was_encrypted: bool = False


class AnalyzeRequest(BaseModel):
    document_id: str
    level: Optional[int] = Field(None, ge=1, le=20)


class AnalysisResponse(BaseModel):
    document: DocumentInfo
    bookmarks: List[BookmarkNode]
    levels: List[LevelStat]
    suggested_level: Optional[int]
    selected_level: Optional[int]
    sections: List[Section]
    warnings: List[str] = []
    has_usable_bookmarks: bool


BookmarkNode.model_rebuild()
