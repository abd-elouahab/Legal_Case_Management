"""SQLAlchemy ORM models.

Every model module must be imported here so that its table registers on
``Base.metadata`` before Alembic autogenerate runs and before ``create_all`` is
used in tests.
"""

from __future__ import annotations

from models.case import Case, CasePriority, CaseStatus
from models.conversation import (
    Conversation,
    ConversationMessage,
    ConversationRole,
    ConversationStatus,
    FeedbackRating,
    MessageFeedback,
)
from models.document import Document, DocumentCategory, DocumentVersion
from models.indexing import DocumentIndex, IndexStatus
from models.ocr import OcrPage, OcrResult, OcrStatus
from models.report import Report, ReportStatus, ReportType
from models.timeline import TimelineEvent, TimelineEventCategory, TimelineEventType
from models.user import User, UserRole, UserStatus

__all__ = [
    "Case",
    "CasePriority",
    "CaseStatus",
    "Conversation",
    "ConversationMessage",
    "ConversationRole",
    "ConversationStatus",
    "Document",
    "DocumentCategory",
    "DocumentIndex",
    "DocumentVersion",
    "FeedbackRating",
    "IndexStatus",
    "MessageFeedback",
    "OcrPage",
    "OcrResult",
    "OcrStatus",
    "Report",
    "ReportStatus",
    "ReportType",
    "TimelineEvent",
    "TimelineEventCategory",
    "TimelineEventType",
    "User",
    "UserRole",
    "UserStatus",
]
