"""AI report ORM model.

The sixth stage of the AI pipeline, and the first **non-conversational** consumer
of it. ``architecture.md`` has listed *Reports* under PostgreSQL since the
storage model was written; this module is that listing.

**One table, deliberately.** The obvious alternative is two — a report and its
sections — and it was rejected for the reason :mod:`models.indexing` gives for
its own single table: a section is never read, filtered, paged, or referenced on
its own. It is read as part of the report, exported as part of the report, and
regenerated with the report. A ``report_sections`` table would buy a join on
every read and a second place for the section order to live, in exchange for a
normalisation nothing queries against. The sections are therefore JSON on the
report, exactly as a message's citations are JSON on the message.

**A report is a run, not a document.** It has the same shape as an OCR result and
an indexing run — ``pending`` → ``processing`` → ``completed`` | ``failed``, an
attempt counter, a duration, a machine-readable failure code — because it is the
same kind of thing: long-running background work whose *state* a client polls.
That is why the progress the spec asks to be queryable is two integer columns
here rather than a Redis key: the row is already the record of the work, and a
progress counter that lives anywhere else can disagree with it.

**A report belongs to one user and to one case, and both matter.** The case is
what its content is drawn from and what decides whether it could be generated at
all; the *user* is whose history it appears in, because
``14-ai-report-agent.md`` requires that report history *"remain user-specific"*
and that generated reports *"remain private"*. So reads are keyed by
``requested_by`` exactly as a conversation's are keyed by ``owner_id`` — see
:mod:`repositories.report`.

**Nothing here retrieves, prompts, or cites.** A report *records* what the RAG
pipeline produced, section by section; the pipeline remains the only thing that
can produce any of it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from db.base import Base
from models.case import Case
from models.user import User

#: ``JSONB`` on PostgreSQL, plain ``JSON`` everywhere else — the same variant
#: :mod:`models.timeline` and :mod:`models.conversation` use, and for the same
#: two reasons: JSONB stores parsed and is indexable, and the SQLite test
#: database has no JSONB at all.
_JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class ReportType(StrEnum):
    """Which report was asked for.

    Exactly the five ``14-ai-report-agent.md`` requires *"at minimum"*. The
    *content* of each — which sections, in what order, asking what — lives in
    :data:`~core.reports.REPORT_TEMPLATES` rather than here, so adding a sixth
    type is a member below plus a template entry, with **no migration**… except
    that this is a PostgreSQL enum, so it *is* one.

    That trade is deliberate and is the opposite of the one
    ``timeline_events.event_type`` makes. The timeline's registry is open by
    design — the spec requires future modules to publish without modifying it.
    A report type is not open: every one of them needs a template, a title in
    three languages, and a place in the client's picker, so a type that arrived
    without a migration would be a type nothing could render. Closed, small, and
    platform-defined is exactly the shape ``case_status``, ``document_category``,
    ``ocr_status``, and ``index_status`` have.
    """

    #: The whole matter at a glance: parties, timeline, evidence, issues.
    CASE_SUMMARY = "case_summary"
    #: What a lawyer needs in front of them at the hearing.
    HEARING_PREPARATION = "hearing_preparation"
    #: What the file actually proves, and what it does not.
    EVIDENCE_SUMMARY = "evidence_summary"
    #: What happened, in order, with dates.
    CHRONOLOGICAL_TIMELINE = "chronological_timeline"
    #: The short one, for someone who will not read the long one.
    EXECUTIVE_SUMMARY = "executive_summary"


class ReportStatus(StrEnum):
    """Lifecycle state of one generation run.

    Exactly the four states ``14-ai-report-agent.md`` lists under "Progress
    Tracking". The legal moves between them are declared once, in
    :data:`~core.reports.STATUS_TRANSITIONS`, and every write goes through
    :class:`~services.report.ReportService` — so a run cannot arrive at
    ``COMPLETED`` without having been ``PROCESSING``, which would make its
    duration and its start time a lie.
    """

    #: Queued, not yet picked up by a worker.
    PENDING = "pending"
    #: Claimed by a worker: retrieving and writing sections.
    PROCESSING = "processing"
    #: Every section that could be produced has been, and the report is readable.
    COMPLETED = "completed"
    #: The run ended without a usable report. Nothing else was touched, and the
    #: run can be regenerated.
    FAILED = "failed"


class Report(Base):
    """One generated report: what was asked for, what came back, and how."""

    __tablename__ = "reports"

    __table_args__ = (
        # The history query, whole: a user's own reports, newest first, filtered
        # by status. One composite index rather than three single-column ones,
        # because every read of this table starts from the requester — the same
        # reasoning as `ix_conversations_owner_id_status_last_message_at`.
        Index(
            "ix_reports_requested_by_status_created_at",
            "requested_by",
            "status",
            "created_at",
        ),
        # The case workspace's "reports on this matter" list, and the monitoring
        # aggregate's window. Also what the startup requeue reads.
        Index("ix_reports_case_id_created_at", "case_id", "created_at"),
        Index("ix_reports_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    #: The case the report is about. ``CASCADE`` for the same reason as
    #: ``document_indexes.case_id``: cases are archived rather than removed, so
    #: this only guards against manual cleanup — but a report pointing at a case
    #: that no longer exists would be unreachable by every query here *and*
    #: impossible to authorize, since its access is decided by the case.
    case_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: The assistant conversation this report was generated from, when it was.
    #:
    #: ``14-ai-report-agent.md`` lists *"associated conversation (when
    #: applicable)"* among the metadata to persist. Nothing on the report
    #: generation path *needs* it — the report is built from the case's documents
    #: through the pipeline, not from a transcript — so it is a **provenance
    #: link**: "this report came out of that thread". ``SET NULL`` because losing
    #: the conversation must not cost the report, which is the more durable of
    #: the two.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )

    report_type: Mapped[ReportType] = mapped_column(
        Enum(
            ReportType,
            name="report_type",
            # Persist the enum *values* rather than the Python member names,
            # matching `user_role`, `case_status`, `document_category`,
            # `ocr_status`, `index_status`, and `conversation_status`.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        index=True,
    )

    #: The report's own heading. Derived from the type and the case number by
    #: :func:`~core.reports.default_report_title`, or supplied by the requester.
    #: Never generated by a model: a hallucinated title is the one hallucination
    #: nobody would ever check — the same reasoning
    #: :func:`~core.assistant.derive_title` records for a conversation.
    title: Mapped[str] = mapped_column(String(255), nullable=False)

    #: ISO 639-1 code every section is written in. Settled once, at request time,
    #: so two sections of one report cannot come back in different languages.
    language: Mapped[str] = mapped_column(String(10), nullable=False)

    status: Mapped[ReportStatus] = mapped_column(
        Enum(
            ReportStatus,
            name="report_status",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=ReportStatus.PENDING,
        server_default=ReportStatus.PENDING.value,
        index=True,
    )

    #: Which revision of :data:`~core.reports.REPORT_TEMPLATES` produced this
    #: report. Recorded per report rather than read from the constant, for the
    #: reason an index records its embedding model and a message records its
    #: prompt version: the template set is *current* and a report is
    #: *historical*, and an evaluation comparing two months of reports cannot
    #: group ones that do not say what shaped them.
    template_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    # ---------------------------------------------------------- the progress #

    #: Sections the template asks for, and how many have been written.
    #:
    #: Two plain integers rather than a percentage, because a percentage is a
    #: presentation of these two and storing it would be a third number that can
    #: disagree with them. ``sections_total`` is ``None`` until the run is
    #: planned — before that the platform genuinely does not know, and ``0``
    #: would read as "this report has no sections".
    sections_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sections_completed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # ----------------------------------------------------------- the content #

    #: The report itself, section by section, in template order.
    #:
    #: Each entry carries its key, its heading, its prose, and whether it was
    #: grounded. JSON rather than a second table for the reason the module
    #: docstring gives — and JSON rather than one assembled string because
    #: ``14-ai-report-agent.md`` requires reports to be *"structured rather than
    #: free-form text"* with *"section ordering […] template-driven"*. An
    #: assembled Markdown blob would satisfy neither, and both exports would then
    #: have to parse the platform's own output back into sections.
    sections: Mapped[list[dict[str, Any]]] = mapped_column(
        _JSON_TYPE, nullable=False, default=list, server_default="[]"
    )

    #: Every source the report rests on, de-duplicated across sections and
    #: numbered globally, exactly as the RAG pipeline produced each one.
    #:
    #: The pipeline numbers a section's sources ``[1]``…``[n]`` *within that
    #: section*; a report is one document, so the markers are renumbered once
    #: into this list and the section prose is rewritten to match (see
    #: :class:`~services.report.CitationLedger`). Nothing here is invented: a
    #: marker that resolves to no source is removed rather than pointed
    #: anywhere, which is what *"reports should never invent citations"* means
    #: in practice.
    citations: Mapped[list[dict[str, Any]]] = mapped_column(
        _JSON_TYPE, nullable=False, default=list, server_default="[]"
    )

    # --------------------------------------------------------------- the run #

    #: When the worker claimed the run, and when it reached a terminal state.
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Wall-clock duration in milliseconds. Stored rather than derived from the
    #: two timestamps above so the monitoring aggregate is one ``AVG`` over a
    #: numeric column, and so a run whose timestamps were written by different
    #: clocks cannot report a negative duration — the same reasoning
    #: ``document_indexes.duration_ms`` records.
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: How many times generation has been attempted, including the first. A
    #: regeneration increments it and never resets it, which is what makes "this
    #: report keeps failing" visible without reading the log.
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # -------------------------------------------------- what it was built on #

    #: Passages retrieved across every section, and how many reached a prompt.
    #: "Retrieved 48, used 31" is what tells an operator whether the context
    #: budget rather than the corpus is doing the limiting.
    retrieved_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Sections the pipeline answered from evidence, as opposed to reporting that
    #: the documents do not support one. The report-level equivalent of the
    #: pipeline's grounding rate, and the number that says whether a report is
    #: worth reading.
    grounded_sections: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Total characters of generated prose — the spec's *"average report size"*
    #: metric. The *size* of the content, never the content: it is aggregated by
    #: a monitoring query that must not read a client's matter.
    character_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: How the sections were produced. Recorded per report rather than read from
    #: configuration, for the reason ``conversation_messages.provider`` records.
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Token usage summed across the run's sections, when the provider reports
    #: it. ``None`` rather than ``0`` throughout: ``0`` would read as "this
    #: report was free", which is a different and false statement.
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ----------------------------------------------------------- the failure #

    #: Machine-readable reason, from :class:`~core.reports.ReportFailureCode`.
    #: Separate from the message so a client can branch on the cause without
    #: parsing prose, and so the monitoring view can group failures.
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)

    #: Human-readable explanation, safe to show an authorized user. **Never a
    #: library's raw message**, which can echo the text it was processing.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ------------------------------------------------------------ the export #

    #: How many times this report has been exported, and when it last was.
    #:
    #: Persisted rather than counted in the process, unlike search's and RAG's
    #: figures, and for the same reason every other number on this table is: a
    #: report *is* a persisted run, so its metrics are exact SQL aggregates that
    #: survive a restart. Counting exports in memory would make one of the six
    #: figures the spec's Monitoring section names mean something different from
    #: the other five.
    export_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_exported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ------------------------------------------------------------- the actor #

    #: Who asked for it — and therefore whose history it appears in.
    #:
    #: ``CASCADE`` rather than the ``SET NULL`` most user references on this
    #: platform use, matching ``conversations.owner_id`` and for the same
    #: reason: every read here is ``requested_by = :caller``, so an owner-less
    #: row would be unreachable data that still contains a generated
    #: interpretation of a client's file.
    requested_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # -------------------------------------------------------------- deletion #

    #: When this report was deleted, or ``None``.
    #:
    #: Logical, like a conversation's and for the same reason: the report carries
    #: the citations of an analysis a lawyer may have acted on, so a delete that
    #: destroyed the row would destroy the record of advice that was given. A
    #: deleted report is excluded from every read — the API answers 404 — and a
    #: future retention job is what actually reclaims it.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # -------------------------------------------------------- relationships #

    case: Mapped[Case] = relationship("Case", lazy="selectin")

    requester: Mapped[User | None] = relationship(
        "User", foreign_keys=[requested_by], lazy="selectin"
    )

    # ------------------------------------------------------------- derived #

    @property
    def is_terminal(self) -> bool:
        """Whether the run has finished, successfully or not."""
        return self.status in {ReportStatus.COMPLETED, ReportStatus.FAILED}

    @property
    def is_active(self) -> bool:
        """Whether the run is queued or currently generating."""
        return self.status in {ReportStatus.PENDING, ReportStatus.PROCESSING}

    @property
    def is_deleted(self) -> bool:
        """Whether the report has been withdrawn."""
        return self.deleted_at is not None

    @property
    def is_exportable(self) -> bool:
        """Whether there is anything to export yet.

        A run that has not completed has no sections, and offering an export of
        one would produce an empty file that looks like a finished report.
        """
        return self.status is ReportStatus.COMPLETED and bool(self.sections)

    @property
    def progress_percent(self) -> int:
        """How far along the run is, 0-100.

        Derived rather than stored, so it cannot disagree with the two counters.
        A run with no planned total reports ``0`` rather than dividing by it —
        the honest answer before planning is "nothing yet".
        """
        if self.status is ReportStatus.COMPLETED:
            return 100
        if not self.sections_total:
            return 0
        return min(100, int(self.sections_completed / self.sections_total * 100))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Never the title and never a section: the first names a case and the
        # second *is* a generated interpretation of a client's file, and a repr
        # ends up in whatever log line interpolates the object. The same rule
        # `DocumentIndex.__repr__` and `Conversation.__repr__` follow.
        return (
            f"<Report id={self.id!s} case_id={self.case_id!s} "
            f"type={self.report_type.value!r} status={self.status.value!r}>"
        )


__all__ = ["Report", "ReportStatus", "ReportType"]
