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
from models.email import EmailDelivery, EmailDeliveryStatus
from models.indexing import DocumentIndex, IndexStatus
from models.notification import Notification, NotificationPreference
from models.ocr import OcrPage, OcrResult, OcrStatus
from models.report import Report, ReportStatus, ReportType
from models.settings import PlatformSetting, UserSetting
from models.timeline import TimelineEvent, TimelineEventCategory, TimelineEventType
from models.user import User, UserRole, UserStatus
from models.whatsapp import WhatsAppDelivery, WhatsAppDeliveryStatus

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
    "EmailDelivery",
    "EmailDeliveryStatus",
    "FeedbackRating",
    "IndexStatus",
    "MessageFeedback",
    "Notification",
    "NotificationPreference",
    "OcrPage",
    "OcrResult",
    "OcrStatus",
    "PlatformSetting",
    "Report",
    "ReportStatus",
    "ReportType",
    "TimelineEvent",
    "TimelineEventCategory",
    "TimelineEventType",
    "User",
    "UserRole",
    "UserSetting",
    "UserStatus",
    "WhatsAppDelivery",
    "WhatsAppDeliveryStatus",
]
