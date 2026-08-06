"""SQLAlchemy ORM models.

Every model module must be imported here so that its table registers on
``Base.metadata`` before Alembic autogenerate runs and before ``create_all`` is
used in tests.
"""

from __future__ import annotations

from models.case import Case, CasePriority, CaseStatus
from models.document import Document, DocumentCategory, DocumentVersion
from models.indexing import DocumentIndex, IndexStatus
from models.ocr import OcrPage, OcrResult, OcrStatus
from models.timeline import TimelineEvent, TimelineEventCategory, TimelineEventType
from models.user import User, UserRole, UserStatus

__all__ = [
    "Case",
    "CasePriority",
    "CaseStatus",
    "Document",
    "DocumentCategory",
    "DocumentIndex",
    "DocumentVersion",
    "IndexStatus",
    "OcrPage",
    "OcrResult",
    "OcrStatus",
    "TimelineEvent",
    "TimelineEventCategory",
    "TimelineEventType",
    "User",
    "UserRole",
    "UserStatus",
]
