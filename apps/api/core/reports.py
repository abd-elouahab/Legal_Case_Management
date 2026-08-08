"""Report-domain utilities.

Small, pure helpers and one body of data shared by the report schemas, graph,
service, exporters, and repository: what a report of each type is *made of*, what
each section is called and asks for, how a section's local citation markers are
renumbered into the report's global ones, the lifecycle's legal moves, and the
failure vocabulary.

They live here rather than inside a service for the same reason
:mod:`core.cases`, :mod:`core.documents`, :mod:`core.ocr`, :mod:`core.indexing`,
:mod:`core.search`, :mod:`core.rag`, and :mod:`core.assistant` exist — the same
rules must apply however a report is requested, and they can be unit-tested
without a database, a request, a running Qdrant, or an API key.

**Nothing here retrieves, renders a template, or calls a model.** Retrieval and
generation are both :class:`~services.rag.RagService`'s, and *only* its (see
:mod:`services.report`); the order the sections are produced in is
:mod:`services.report_graph`'s; turning a finished report into a file is
:mod:`services.report_export`'s.

--------------------------------------------------------------------------------
Why the section instructions are here rather than in ``apps/api/prompts/``
--------------------------------------------------------------------------------

``14-ai-report-agent.md`` lists **prompt construction** under *Do NOT implement*,
and says the agent *"must not duplicate retrieval, prompt construction, or LLM
interaction logic"*. It is obeyed literally: this feature builds no prompt and
calls no model. Every section of every report is produced by handing
:meth:`~services.rag.RagService.answer` a **question**, which that pipeline then
retrieves against, fences inside its own versioned ``rag/answer`` template, and
answers with citations attached.

So the strings below are not prompts. They are the *questions the platform asks
about a case*, which is domain data in exactly the sense
:data:`~core.cases.STATUS_TRANSITIONS` is — and they are versioned as a set by
:data:`REPORT_TEMPLATE_VERSION`, recorded on every report, so an evaluation can
group by them the way it groups by a prompt version.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from core.indexing import LANGUAGE_ARABIC, LANGUAGE_ENGLISH, LANGUAGE_FRENCH
from core.rag import CITATION_MARKER_PATTERN, SUPPORTED_ANSWER_LANGUAGES
from models.report import ReportStatus, ReportType

#: Revision of the template set below.
#:
#: Bumped whenever a section is added, removed, reordered, or has its question
#: rewritten — i.e. whenever two reports of the same type would no longer be
#: comparable. Recorded on every report (``reports.template_version``) for the
#: reason a message records its prompt version: configuration is *current* and a
#: report is *historical*.
REPORT_TEMPLATE_VERSION = 1

#: Shortest section prose worth keeping, in characters.
#:
#: Below this the model has produced a fragment rather than a section, and a
#: heading followed by nine characters reads as a rendering fault to a lawyer.
#: Such a section is recorded as *not covered* by the documents, which is a
#: statement the platform can stand behind.
MIN_SECTION_CHARACTERS = 40

#: Longest custom title a requester may give a report, matching the column.
MAX_REPORT_TITLE_LENGTH = 255


class ReportFormat(StrEnum):
    """A form a finished report can be delivered in.

    The two ``14-ai-report-agent.md`` requires *"at minimum"*. DOCX and HTML are
    named as *"extensible to support […] later without redesign"*, and the seam
    that makes that true is :data:`~services.report_export.RENDERER_FACTORIES` —
    one class plus one entry, with no change to the service, the router, or this
    enum's consumers beyond a member here.
    """

    #: The report as text, headings and all. Always available: it is produced by
    #: the platform itself with no library behind it.
    MARKDOWN = "markdown"
    #: The report as a paginated document, for filing and for sending on.
    PDF = "pdf"


class ReportFailureCode(StrEnum):
    """Why a report could not be generated, or could not be exported.

    Machine-readable, so the monitoring view can group failures and a client can
    branch on the cause without parsing a sentence. The members cover exactly the
    failures ``14-ai-report-agent.md`` lists under "Error Handling" — retrieval
    failures, LLM failures, export failures, timeout, insufficient context — plus
    the catch-all every other stage of this pipeline carries.

    **There is deliberately no member for "a section found no evidence".** A
    section the documents do not cover is a *recorded outcome* of a successful
    report, not a failure of one: the report says so in that section's place, and
    counting it here would make the failure rate a measure of the corpus rather
    than of the platform — the same argument :class:`~core.rag.RagFailureCode`
    makes about a question with no supporting evidence.
    """

    #: Retrieval could not run — the embedding model or the vector database was
    #: unavailable. Nothing about the request is wrong.
    RETRIEVAL_UNAVAILABLE = "retrieval_unavailable"
    #: No language model is configured, or the configured one cannot be reached.
    LLM_UNAVAILABLE = "llm_unavailable"
    #: A deadline passed — the whole run's, or one section's.
    TIMEOUT = "timeout"
    #: The model was reached and refused, or failed mid-generation.
    LLM_FAILURE = "llm_failure"
    #: The model answered with something unusable, on every section.
    MALFORMED_RESPONSE = "malformed_response"
    #: **No section of the report could be grounded.** Distinct from a single
    #: uncovered section, which is ordinary: this is the case file containing
    #: nothing the report could be built from at all, and producing a document of
    #: empty headings would look like a working report that says nothing.
    INSUFFICIENT_CONTEXT = "insufficient_context"
    #: A finished report could not be turned into a file. Only ever raised by the
    #: export path — a generation run cannot fail this way.
    EXPORT_FAILURE = "export_failure"
    #: Anything the branches above do not describe.
    UNKNOWN = "unknown"


#: Human-readable explanation per failure code.
#:
#: Kept here rather than raised with the exception so the message a user reads is
#: written once and cannot vary between call sites. Every one is deliberately
#: free of the case, the documents, and any internal detail — ``14-ai-report-agent.md``:
#: *"Provide user-friendly error messages. Never expose internal implementation
#: details."*
FAILURE_MESSAGES: Mapping[ReportFailureCode, str] = MappingProxyType(
    {
        ReportFailureCode.RETRIEVAL_UNAVAILABLE: (
            "Supporting documents could not be retrieved. The case and its documents are "
            "unaffected, and the report can be generated again."
        ),
        ReportFailureCode.LLM_UNAVAILABLE: (
            "The AI service is unavailable. The case and its documents are unaffected, and the "
            "report can be generated again."
        ),
        ReportFailureCode.TIMEOUT: (
            "The report took too long to produce. Try again, or choose a shorter report type."
        ),
        ReportFailureCode.LLM_FAILURE: "The AI service could not produce this report.",
        ReportFailureCode.MALFORMED_RESPONSE: (
            "The AI service returned an unusable report. Try generating it again."
        ),
        ReportFailureCode.INSUFFICIENT_CONTEXT: (
            "This case has no indexed documents that support a report of this type. Upload the "
            "relevant documents, or wait for their processing to finish, and try again."
        ),
        ReportFailureCode.EXPORT_FAILURE: (
            "This report could not be exported in that format. Try Markdown, which needs no "
            "additional software."
        ),
        ReportFailureCode.UNKNOWN: "The report could not be generated.",
    }
)


def failure_message(code: ReportFailureCode) -> str:
    """The message shown for a failure code.

    Falls back to the generic explanation for a code with no entry, so a future
    member added without a message still reads as a sentence rather than as an
    identifier — the same posture :func:`~core.rag.failure_message` takes.
    """
    return FAILURE_MESSAGES.get(code, FAILURE_MESSAGES[ReportFailureCode.UNKNOWN])


def normalize_error_message(value: str) -> str:
    """Reduce a failure message to one storable line.

    Collapses whitespace and caps the length, so nothing a library said can
    arrive in the column as a multi-line trace. The same shape
    :func:`~core.indexing.normalize_error_message` has.
    """
    return " ".join(value.split())[:500]


# --------------------------------------------------------------------------- #
# The lifecycle
# --------------------------------------------------------------------------- #

#: The only legal moves between report states.
#:
#: Declared once, here, and enforced by :class:`~services.report.ReportService`
#: rather than by whoever happens to be writing a status — the same shape
#: :data:`~core.indexing.STATUS_TRANSITIONS` has. ``COMPLETED`` and ``FAILED``
#: both lead back to ``PENDING`` because regenerating is re-using the row, and
#: ``PROCESSING`` leads there too so a run stranded by an ungraceful shutdown can
#: be recovered rather than being the one state nothing can leave.
STATUS_TRANSITIONS: Mapping[ReportStatus, frozenset[ReportStatus]] = MappingProxyType(
    {
        ReportStatus.PENDING: frozenset({ReportStatus.PROCESSING, ReportStatus.FAILED}),
        ReportStatus.PROCESSING: frozenset(
            {ReportStatus.COMPLETED, ReportStatus.FAILED, ReportStatus.PENDING}
        ),
        ReportStatus.COMPLETED: frozenset({ReportStatus.PENDING}),
        ReportStatus.FAILED: frozenset({ReportStatus.PENDING}),
    }
)


def can_transition(current: ReportStatus, target: ReportStatus) -> bool:
    """Whether a run may move from ``current`` to ``target``."""
    return target in STATUS_TRANSITIONS.get(current, frozenset())


def can_regenerate(current: ReportStatus) -> bool:
    """Whether a run may be started again.

    Only a *finished* run. A queued or in-flight one is refused rather than
    silently re-queued: the caller asked for a fresh report, and answering "done"
    for one already being written would make the button they pressed
    indistinguishable from one that worked — the same reasoning
    :func:`~core.indexing.can_reindex` records.
    """
    return current in {ReportStatus.COMPLETED, ReportStatus.FAILED}


# --------------------------------------------------------------------------- #
# The templates
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ReportSectionSpec:
    """One section of a report: what it is called, and what it asks the case.

    ``question`` is put to :meth:`~services.rag.RagService.answer` verbatim, in
    the report's language. It is phrased as a *report-writing instruction*
    ("Write the Parties section…") rather than as a bare question, because the
    pipeline's own prompt already supplies the grounding rules, the citation
    instruction, and the refusal sentinel — so the only thing left to say is what
    this section is for.
    """

    #: Stable identifier, used as the section's key in storage and in exports.
    #: Never shown to a user.
    key: str
    #: Heading, per language.
    titles: Mapping[str, str]
    #: The instruction put to the pipeline, per language.
    questions: Mapping[str, str]

    def title(self, language: str) -> str:
        """The heading in ``language``, falling back to French."""
        return self.titles.get(language) or self.titles[LANGUAGE_FRENCH]

    def question(self, language: str) -> str:
        """The instruction in ``language``, falling back to French."""
        return self.questions.get(language) or self.questions[LANGUAGE_FRENCH]


@dataclass(frozen=True, slots=True)
class ReportTemplate:
    """What a report of one type is made of.

    ``14-ai-report-agent.md``: *"Section ordering should be template-driven"* —
    which is this tuple, in this order, and nothing else decides it. Adding a
    template is an entry in :data:`REPORT_TEMPLATES` plus a member on
    :class:`~models.report.ReportType`; no service, graph, exporter, or route
    changes, which is the *"allow future report templates without redesign"* the
    spec asks for.
    """

    report_type: ReportType
    #: The report's default heading, per language. Combined with the case number
    #: by :func:`default_report_title`.
    titles: Mapping[str, str]
    #: One-line description of what the report is for, per language. Shown in the
    #: client's picker so nobody has to guess what they are about to generate.
    descriptions: Mapping[str, str]
    sections: tuple[ReportSectionSpec, ...]

    def title(self, language: str) -> str:
        """The report's name in ``language``, falling back to French."""
        return self.titles.get(language) or self.titles[LANGUAGE_FRENCH]

    def description(self, language: str) -> str:
        """The report's purpose in ``language``, falling back to French."""
        return self.descriptions.get(language) or self.descriptions[LANGUAGE_FRENCH]

    @property
    def section_count(self) -> int:
        """How many sections this template produces."""
        return len(self.sections)


def _section(
    key: str, *, fr: tuple[str, str], ar: tuple[str, str], en: tuple[str, str]
) -> ReportSectionSpec:
    """Build one section spec from its three ``(title, question)`` pairs.

    A helper rather than three dictionaries written out per section, because the
    catalog below is long and the failure mode it prevents — a section whose
    Arabic title has drifted onto the wrong entry — is invisible in review.
    """
    return ReportSectionSpec(
        key=key,
        titles=MappingProxyType(
            {LANGUAGE_FRENCH: fr[0], LANGUAGE_ARABIC: ar[0], LANGUAGE_ENGLISH: en[0]}
        ),
        questions=MappingProxyType(
            {LANGUAGE_FRENCH: fr[1], LANGUAGE_ARABIC: ar[1], LANGUAGE_ENGLISH: en[1]}
        ),
    )


#: Every section the platform knows how to write, keyed by identifier.
#:
#: Shared across templates rather than declared per template: "Evidence" means
#: the same thing in a case summary and in a hearing preparation report, and two
#: copies of it would be two chances for them to drift apart — and for a report
#: to contain a heading a client's picker cannot label.
SECTION_CATALOG: Mapping[str, ReportSectionSpec] = MappingProxyType(
    {
        spec.key: spec
        for spec in (
            _section(
                "overview",
                fr=(
                    "Aperçu",
                    "Rédigez la section « Aperçu » d'un rapport juridique sur cette affaire : "
                    "en quoi consiste le litige, à quel stade il se trouve et ce qui est en jeu. "
                    "Trois à cinq phrases, fondées uniquement sur les documents.",
                ),
                ar=(
                    "نظرة عامة",
                    "اكتب قسم «نظرة عامة» من تقرير قانوني عن هذه القضية: موضوع النزاع، والمرحلة "
                    "التي بلغها، وما هو محل المطالبة. من ثلاث إلى خمس جمل، استنادًا إلى المستندات "
                    "وحدها.",
                ),
                en=(
                    "Overview",
                    "Write the Overview section of a legal report on this case: what the dispute "
                    "is about, what stage it has reached, and what is at stake. Three to five "
                    "sentences, based only on the documents.",
                ),
            ),
            _section(
                "case_information",
                fr=(
                    "Informations sur l'affaire",
                    "Rédigez la section « Informations sur l'affaire » : juridiction saisie, "
                    "numéro de rôle, date de dépôt, nature de la procédure et statut actuel, tels "
                    "qu'ils figurent dans les documents.",
                ),
                ar=(
                    "معلومات القضية",
                    "اكتب قسم «معلومات القضية»: المحكمة المختصة، ورقم الملف، وتاريخ الإيداع، "
                    "وطبيعة الدعوى، والحالة الراهنة، كما وردت في المستندات.",
                ),
                en=(
                    "Case Information",
                    "Write the Case Information section: the court seised, the docket number, the "
                    "filing date, the nature of the proceedings, and the current status, as they "
                    "appear in the documents.",
                ),
            ),
            _section(
                "parties",
                fr=(
                    "Parties",
                    "Rédigez la section « Parties » : identifiez chaque partie, son rôle dans la "
                    "procédure (demandeur, défendeur, intervenant) et son représentant lorsqu'il "
                    "est mentionné. Présentez-les sous forme de liste.",
                ),
                ar=(
                    "الأطراف",
                    "اكتب قسم «الأطراف»: حدّد كل طرف، وصفته في الدعوى (مدّعٍ، مدّعى عليه، متدخّل)، "
                    "ومن يمثله عند ذكره. اعرضها في شكل قائمة.",
                ),
                en=(
                    "Parties",
                    "Write the Parties section: identify each party, their role in the proceedings "
                    "(claimant, defendant, intervener), and their representation where it is "
                    "named. Present them as a list.",
                ),
            ),
            _section(
                "timeline",
                fr=(
                    "Chronologie",
                    "Rédigez la section « Chronologie » : listez les événements datés de l'affaire "
                    "dans l'ordre chronologique, chacun sur une ligne, en commençant par la date. "
                    "N'indiquez que les dates qui figurent effectivement dans les documents.",
                ),
                ar=(
                    "التسلسل الزمني",
                    "اكتب قسم «التسلسل الزمني»: اسرد الوقائع المؤرَّخة في القضية بترتيب زمني، كل "
                    "واقعة في سطر يبدأ بتاريخها. لا تذكر إلا التواريخ الواردة فعليًا في المستندات.",
                ),
                en=(
                    "Timeline",
                    "Write the Timeline section: list the dated events of the case in "
                    "chronological order, one per line, each beginning with its date. Include only "
                    "dates that actually appear in the documents.",
                ),
            ),
            _section(
                "key_dates",
                fr=(
                    "Échéances",
                    "Rédigez la section « Échéances » : relevez les délais, dates d'audience et "
                    "échéances procédurales à venir mentionnés dans les documents, avec ce qu'ils "
                    "imposent de faire.",
                ),
                ar=(
                    "المواعيد والآجال",
                    "اكتب قسم «المواعيد والآجال»: استخرج الآجال وتواريخ الجلسات والمواعيد "
                    "الإجرائية المقبلة الواردة في المستندات، مع بيان ما يترتب على كل منها.",
                ),
                en=(
                    "Key Dates",
                    "Write the Key Dates section: extract the deadlines, hearing dates, and "
                    "upcoming procedural milestones named in the documents, with what each one "
                    "requires.",
                ),
            ),
            _section(
                "evidence",
                fr=(
                    "Éléments de preuve",
                    "Rédigez la section « Éléments de preuve » : quels documents ont été produits, "
                    "ce que chacun établit et par quelle partie il a été versé.",
                ),
                ar=(
                    "الأدلة",
                    "اكتب قسم «الأدلة»: ما المستندات المقدَّمة، وما الذي يثبته كل منها، ومن الطرف "
                    "الذي قدّمه.",
                ),
                en=(
                    "Evidence",
                    "Write the Evidence section: which documents have been produced, what each one "
                    "establishes, and which party filed it.",
                ),
            ),
            _section(
                "evidence_inventory",
                fr=(
                    "Inventaire des pièces",
                    "Rédigez la section « Inventaire des pièces » : listez chaque pièce du "
                    "dossier avec sa nature, sa date et la partie qui l'a produite. Une pièce par "
                    "ligne.",
                ),
                ar=(
                    "جرد المستندات",
                    "اكتب قسم «جرد المستندات»: اسرد كل مستند في الملف مع نوعه وتاريخه والطرف الذي "
                    "قدّمه. مستند واحد في كل سطر.",
                ),
                en=(
                    "Evidence Inventory",
                    "Write the Evidence Inventory section: list every exhibit in the file with its "
                    "nature, its date, and the party that produced it. One exhibit per line.",
                ),
            ),
            _section(
                "evidence_analysis",
                fr=(
                    "Analyse des preuves",
                    "Rédigez la section « Analyse des preuves » : ce que les pièces établissent "
                    "ensemble, les points sur lesquels elles se corroborent et ceux sur lesquels "
                    "elles se contredisent.",
                ),
                ar=(
                    "تحليل الأدلة",
                    "اكتب قسم «تحليل الأدلة»: ما تثبته المستندات مجتمعة، والنقاط التي تتعاضد فيها "
                    "والنقاط التي تتعارض فيها.",
                ),
                en=(
                    "Evidence Analysis",
                    "Write the Evidence Analysis section: what the exhibits establish together, "
                    "where they corroborate one another, and where they conflict.",
                ),
            ),
            _section(
                "evidence_gaps",
                fr=(
                    "Lacunes probatoires",
                    "Rédigez la section « Lacunes probatoires » : les faits invoqués dans les "
                    "documents qui ne sont étayés par aucune pièce du dossier. N'affirmez une "
                    "lacune que si les documents la font apparaître.",
                ),
                ar=(
                    "الثغرات في الإثبات",
                    "اكتب قسم «الثغرات في الإثبات»: الوقائع المُدّعاة في المستندات التي لا يسندها "
                    "أي دليل في الملف. لا تُقرّ بوجود ثغرة إلا إذا كانت المستندات تُظهرها.",
                ),
                en=(
                    "Evidence Gaps",
                    "Write the Evidence Gaps section: facts asserted in the documents that no "
                    "exhibit in the file supports. Assert a gap only where the documents show one.",
                ),
            ),
            _section(
                "legal_issues",
                fr=(
                    "Questions juridiques",
                    "Rédigez la section « Questions juridiques » : les points de droit soulevés "
                    "par le dossier, avec les textes et décisions cités dans les documents.",
                ),
                ar=(
                    "المسائل القانونية",
                    "اكتب قسم «المسائل القانونية»: النقاط القانونية التي يثيرها الملف، مع النصوص "
                    "والأحكام المُشار إليها في المستندات.",
                ),
                en=(
                    "Legal Issues",
                    "Write the Legal Issues section: the questions of law the file raises, with "
                    "the provisions and decisions cited in the documents.",
                ),
            ),
            _section(
                "key_facts",
                fr=(
                    "Faits essentiels",
                    "Rédigez la section « Faits essentiels » : les faits que la juridiction devra "
                    "trancher, présentés sous forme de liste, chacun rattaché à la pièce qui "
                    "l'établit.",
                ),
                ar=(
                    "الوقائع الجوهرية",
                    "اكتب قسم «الوقائع الجوهرية»: الوقائع التي سيتعيّن على المحكمة الفصل فيها، في "
                    "شكل قائمة، وكل واقعة مسندة إلى المستند الذي يثبتها.",
                ),
                en=(
                    "Key Facts",
                    "Write the Key Facts section: the facts the court will have to decide, as a "
                    "list, each tied to the exhibit that establishes it.",
                ),
            ),
            _section(
                "hearing_objectives",
                fr=(
                    "Objectifs de l'audience",
                    "Rédigez la section « Objectifs de l'audience » : ce que la partie représentée "
                    "doit obtenir à la prochaine audience, d'après les demandes et conclusions "
                    "figurant dans les documents.",
                ),
                ar=(
                    "أهداف الجلسة",
                    "اكتب قسم «أهداف الجلسة»: ما يتعيّن على الطرف الممثَّل تحقيقه في الجلسة "
                    "المقبلة، استنادًا إلى الطلبات والمذكرات الواردة في المستندات.",
                ),
                en=(
                    "Hearing Objectives",
                    "Write the Hearing Objectives section: what the represented party needs to "
                    "obtain at the next hearing, based on the claims and submissions in the "
                    "documents.",
                ),
            ),
            _section(
                "anticipated_arguments",
                fr=(
                    "Arguments adverses attendus",
                    "Rédigez la section « Arguments adverses attendus » : les moyens que la partie "
                    "adverse a déjà soulevés dans les documents, et la réponse que le dossier "
                    "permet d'y apporter.",
                ),
                ar=(
                    "دفوع الخصم المتوقعة",
                    "اكتب قسم «دفوع الخصم المتوقعة»: الدفوع التي أثارها الخصم فعلًا في المستندات، "
                    "والرد الذي يتيحه الملف على كل منها.",
                ),
                en=(
                    "Anticipated Arguments",
                    "Write the Anticipated Arguments section: the points the opposing party has "
                    "already raised in the documents, and the answer the file supports for each.",
                ),
            ),
            _section(
                "preparation_checklist",
                fr=(
                    "Points à préparer",
                    "Rédigez la section « Points à préparer » : ce qu'il reste à réunir, vérifier "
                    "ou produire avant l'audience, d'après ce que les documents indiquent comme "
                    "attendu ou manquant.",
                ),
                ar=(
                    "ما يجب تحضيره",
                    "اكتب قسم «ما يجب تحضيره»: ما تبقّى جمعه أو التحقق منه أو تقديمه قبل الجلسة، "
                    "استنادًا إلى ما تشير إليه المستندات بوصفه مطلوبًا أو ناقصًا.",
                ),
                en=(
                    "Preparation Checklist",
                    "Write the Preparation Checklist section: what remains to be gathered, "
                    "verified, or filed before the hearing, based on what the documents indicate "
                    "is expected or missing.",
                ),
            ),
            _section(
                "key_findings",
                fr=(
                    "Constats principaux",
                    "Rédigez la section « Constats principaux » : les trois à cinq conclusions les "
                    "plus importantes que le dossier permet de tirer, sous forme de liste.",
                ),
                ar=(
                    "أبرز الاستنتاجات",
                    "اكتب قسم «أبرز الاستنتاجات»: من ثلاثة إلى خمسة استنتاجات هي الأهم مما يتيحه "
                    "الملف، في شكل قائمة.",
                ),
                en=(
                    "Key Findings",
                    "Write the Key Findings section: the three to five most important conclusions "
                    "the file supports, as a list.",
                ),
            ),
            _section(
                "risks",
                fr=(
                    "Risques",
                    "Rédigez la section « Risques » : les risques procéduraux et de fond que les "
                    "documents font apparaître pour la partie représentée. N'énoncez un risque que "
                    "s'il ressort du dossier.",
                ),
                ar=(
                    "المخاطر",
                    "اكتب قسم «المخاطر»: المخاطر الإجرائية والموضوعية التي تُظهرها المستندات "
                    "بالنسبة للطرف الممثَّل. لا تذكر خطرًا إلا إذا كان مستفادًا من الملف.",
                ),
                en=(
                    "Risks",
                    "Write the Risks section: the procedural and substantive risks the documents "
                    "reveal for the represented party. State a risk only where the file shows one.",
                ),
            ),
            _section(
                "recommendations",
                fr=(
                    "Recommandations",
                    "Rédigez la section « Recommandations » : les prochaines étapes que le dossier "
                    "justifie, sous forme de liste d'actions. Rappelez que ces recommandations "
                    "doivent être validées par l'avocat en charge.",
                ),
                ar=(
                    "التوصيات",
                    "اكتب قسم «التوصيات»: الخطوات المقبلة التي يبرّرها الملف، في شكل قائمة "
                    "إجراءات. ونبّه إلى أن هذه التوصيات تظل رهن مصادقة المحامي المكلّف بالملف.",
                ),
                en=(
                    "Recommendations",
                    "Write the Recommendations section: the next steps the file justifies, as a "
                    "list of actions. Note that these recommendations remain subject to the "
                    "responsible lawyer's approval.",
                ),
            ),
        )
    }
)


def _template(
    report_type: ReportType,
    *,
    fr: tuple[str, str],
    ar: tuple[str, str],
    en: tuple[str, str],
    sections: Sequence[str],
) -> ReportTemplate:
    """Build one template from its three ``(title, description)`` pairs and its section keys.

    The keys are resolved against :data:`SECTION_CATALOG` **here**, at import
    time, so a template naming a section that does not exist fails the moment the
    module loads rather than the first time somebody generates that report.
    """
    return ReportTemplate(
        report_type=report_type,
        titles=MappingProxyType(
            {LANGUAGE_FRENCH: fr[0], LANGUAGE_ARABIC: ar[0], LANGUAGE_ENGLISH: en[0]}
        ),
        descriptions=MappingProxyType(
            {LANGUAGE_FRENCH: fr[1], LANGUAGE_ARABIC: ar[1], LANGUAGE_ENGLISH: en[1]}
        ),
        sections=tuple(SECTION_CATALOG[key] for key in sections),
    )


#: Every report the platform can produce, and what each one is made of.
#:
#: The five ``14-ai-report-agent.md`` requires. Note that no template contains a
#: "References" section: the reference list is **derived** from the citations the
#: pipeline attached, so asking a model to write one would be asking it to
#: re-state a list the platform already holds — and inviting it to invent an
#: entry. See :func:`references_title`.
REPORT_TEMPLATES: Mapping[ReportType, ReportTemplate] = MappingProxyType(
    {
        template.report_type: template
        for template in (
            _template(
                ReportType.CASE_SUMMARY,
                fr=("Synthèse de l'affaire", "Vue complète du dossier, de bout en bout."),
                ar=("ملخّص القضية", "عرض شامل للملف من أوّله إلى آخره."),
                en=("Case Summary", "The whole matter, end to end."),
                sections=[
                    "overview",
                    "case_information",
                    "parties",
                    "timeline",
                    "evidence",
                    "legal_issues",
                    "recommendations",
                ],
            ),
            _template(
                ReportType.HEARING_PREPARATION,
                fr=(
                    "Préparation d'audience",
                    "Ce qu'il faut avoir sous les yeux à la prochaine audience.",
                ),
                ar=("تحضير الجلسة", "ما ينبغي أن يكون بين يديك في الجلسة المقبلة."),
                en=("Hearing Preparation Report", "What to have in front of you at the hearing."),
                sections=[
                    "overview",
                    "case_information",
                    "hearing_objectives",
                    "key_facts",
                    "evidence",
                    "anticipated_arguments",
                    "preparation_checklist",
                ],
            ),
            _template(
                ReportType.EVIDENCE_SUMMARY,
                fr=(
                    "Synthèse des preuves",
                    "Ce que le dossier établit, et ce qu'il n'établit pas.",
                ),
                ar=("ملخّص الأدلة", "ما يثبته الملف، وما لا يثبته."),
                en=("Evidence Summary", "What the file proves, and what it does not."),
                sections=[
                    "overview",
                    "evidence_inventory",
                    "evidence_analysis",
                    "evidence_gaps",
                ],
            ),
            _template(
                ReportType.CHRONOLOGICAL_TIMELINE,
                fr=("Chronologie du dossier", "Ce qui s'est passé, dans l'ordre, avec les dates."),
                ar=("التسلسل الزمني للملف", "ما وقع، بالترتيب، مع التواريخ."),
                en=("Chronological Timeline Report", "What happened, in order, with dates."),
                sections=["overview", "timeline", "key_dates"],
            ),
            _template(
                ReportType.EXECUTIVE_SUMMARY,
                fr=("Note de synthèse", "La version courte, pour qui ne lira pas la longue."),
                ar=("مذكّرة تنفيذية", "النسخة الموجزة لمن لن يقرأ المطوّلة."),
                en=("Executive Summary", "The short one, for whoever will not read the long one."),
                sections=["overview", "key_findings", "risks", "recommendations"],
            ),
        )
    }
)


def template_for(report_type: ReportType) -> ReportTemplate:
    """The template a report type is built from.

    Raises:
        KeyError: the type has no template. Unreachable through the API — the
            request schema accepts only :class:`~models.report.ReportType`
            members — and deliberately loud rather than defaulted, because
            silently producing a *different* report than the one asked for is
            worse than failing.
    """
    return REPORT_TEMPLATES[report_type]


# --------------------------------------------------------------------------- #
# Language and titles
# --------------------------------------------------------------------------- #

#: Heading of the derived reference list, per language.
_REFERENCES_TITLES: Mapping[str, str] = MappingProxyType(
    {
        LANGUAGE_FRENCH: "Références",
        LANGUAGE_ARABIC: "المراجع",
        LANGUAGE_ENGLISH: "References",
    }
)

#: What a section says when the case file does not cover it, per language.
#:
#: Written by the platform rather than generated, and for exactly the reason
#: :data:`~core.rag.NO_EVIDENCE_MESSAGES` is: a model asked to explain that it
#: found nothing will sometimes explain it *and then answer anyway* from its own
#: training, which in a legal report is indistinguishable from a grounded
#: finding. A fixed sentence cannot speculate.
_NO_CONTENT_MESSAGES: Mapping[str, str] = MappingProxyType(
    {
        LANGUAGE_FRENCH: (
            "Les documents indexés de cette affaire ne contiennent pas d'élément permettant de "
            "rédiger cette section."
        ),
        LANGUAGE_ARABIC: (
            "لا تتضمّن مستندات هذه القضية المفهرسة ما يكفي لتحرير هذا القسم."
        ),
        LANGUAGE_ENGLISH: (
            "The indexed documents for this case contain nothing that supports this section."
        ),
    }
)

#: The standing note every generated report carries, per language.
#:
#: ``ai-workflow-rules.md``: *"AI features are assistants, not decision-makers."*
#: A report that looks like a lawyer's work product and is not must say so on its
#: face — in the document itself, so the statement survives the export, the email
#: it is forwarded in, and the print-out.
_DISCLAIMERS: Mapping[str, str] = MappingProxyType(
    {
        LANGUAGE_FRENCH: (
            "Rapport généré automatiquement à partir des documents indexés de cette affaire. Il ne "
            "constitue pas un conseil juridique et doit être vérifié par l'avocat en charge du "
            "dossier."
        ),
        LANGUAGE_ARABIC: (
            "تقرير مُولَّد آليًا انطلاقًا من مستندات هذه القضية المفهرسة. لا يُعدّ استشارة قانونية "
            "ويجب أن يتحقّق منه المحامي المكلّف بالملف."
        ),
        LANGUAGE_ENGLISH: (
            "Automatically generated from the indexed documents of this case. It is not legal "
            "advice and must be checked by the lawyer responsible for the matter."
        ),
    }
)


def resolve_report_language(requested: str | None) -> str:
    """Decide which language a report is written in.

    Deliberately simpler than :func:`~core.rag.resolve_answer_language`, and the
    difference is instructive: that function has a *question* to detect from, and
    a report request has no free text at all. So there is nothing to detect —
    only an explicit choice to honour, and French to fall back to, which is the
    fallback ``project-overview.md`` supports by naming Arabic and French as the
    platform's interface and AI-interaction languages.
    """
    if requested:
        wanted = requested.strip().lower()
        if wanted in SUPPORTED_ANSWER_LANGUAGES:
            return wanted
    return LANGUAGE_FRENCH


def default_report_title(report_type: ReportType, *, case_number: str, language: str) -> str:
    """The heading a report gets when the requester does not choose one.

    Built from the template's name and the case's *number* — never its title,
    which is client-confidential and would then travel in an export filename, a
    list row, and a timeline description. The number identifies the matter to
    everyone entitled to it and to nobody else.
    """
    name = template_for(report_type).title(language)
    return normalize_report_title(f"{name} — {case_number}")


def normalize_report_title(value: str) -> str:
    """Collapse a title's whitespace and truncate it to the column width.

    Truncated rather than rejected, exactly as a timeline title is: a title is a
    label, and refusing to store a report because its heading was verbose would
    lose the report — which is the expensive thing here.
    """
    return " ".join(value.split())[:MAX_REPORT_TITLE_LENGTH]


def references_title(language: str) -> str:
    """The heading of the derived reference list."""
    return _REFERENCES_TITLES.get(language, _REFERENCES_TITLES[LANGUAGE_ENGLISH])


def no_content_message(language: str) -> str:
    """What a section says when the documents do not cover it."""
    return _NO_CONTENT_MESSAGES.get(language, _NO_CONTENT_MESSAGES[LANGUAGE_ENGLISH])


def report_disclaimer(language: str) -> str:
    """The standing note every generated report carries."""
    return _DISCLAIMERS.get(language, _DISCLAIMERS[LANGUAGE_ENGLISH])


# --------------------------------------------------------------------------- #
# Citations
# --------------------------------------------------------------------------- #

#: What identifies a source for the purpose of de-duplicating it across sections.
#:
#: Document, version, and page — the three references a lawyer reads, and
#: deliberately *not* the passage. Two sections that both lean on page 7 of the
#: same contract are citing one source, and numbering it twice would produce a
#: reference list in which ``[3]`` and ``[9]`` are the same line.
CitationKey = tuple[uuid.UUID, int, int]


def citation_key(document_id: uuid.UUID, document_version: int, page_number: int) -> CitationKey:
    """The de-duplication identity of one source."""
    return (document_id, document_version, page_number)


#: Collapses the whitespace left behind when an unresolvable marker is removed.
_SPACE_RUN = re.compile(r"[ \t]{2,}")

#: Tidies the space a removed marker leaves in front of punctuation.
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?،؛؟])")


def remap_markers(text: str, mapping: Mapping[int, int]) -> str:
    """Rewrite a section's local citation markers into the report's global ones.

    The RAG pipeline numbers each answer's sources ``[1]``…``[n]`` *within that
    answer*, because an answer is the whole document it is numbering. A report is
    one document made of several answers, so ``[1]`` in its fourth section is not
    ``[1]`` in its first — and a reader following the marker would land on the
    wrong contract.

    **Substituted in one pass**, deliberately: rewriting ``[1]``→``[3]`` and then
    ``[3]``→``[1]`` sequentially would swap a marker back onto itself, and the
    bug would only appear when two sections happened to share a source.

    A marker with no entry in the mapping is **removed** rather than left
    dangling, exactly as :meth:`~services.rag.RagService._strip_invented_markers`
    removes an invented one: ``14-ai-report-agent.md`` says reports *"should
    never invent citations"*, and a reference a reader cannot resolve is an
    invented one from where they are sitting. The two regexes afterwards do
    nothing but repair the whitespace the removal left.
    """

    def replace(match: re.Match[str]) -> str:
        target = mapping.get(int(match.group(1)))
        return f"[{target}]" if target is not None else ""

    rewritten = CITATION_MARKER_PATTERN.sub(replace, text)
    rewritten = _SPACE_RUN.sub(" ", rewritten)
    rewritten = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", rewritten)
    return rewritten.strip()


def is_usable_section(text: str) -> bool:
    """Whether generated prose is long enough to stand as a section.

    Deliberately a length floor and nothing more. Judging a section's *quality*
    would be the platform second-guessing the model on legal reasoning, which is
    not something this module can do honestly; judging that a heading is followed
    by a fragment is something it can.
    """
    return len(text.strip()) >= MIN_SECTION_CHARACTERS


def total_characters(values: Iterable[str]) -> int:
    """Total length of a report's prose. Named so call sites read as intent."""
    return sum(len(value) for value in values)


__all__ = [
    "FAILURE_MESSAGES",
    "MAX_REPORT_TITLE_LENGTH",
    "MIN_SECTION_CHARACTERS",
    "REPORT_TEMPLATES",
    "REPORT_TEMPLATE_VERSION",
    "SECTION_CATALOG",
    "STATUS_TRANSITIONS",
    "CitationKey",
    "ReportFailureCode",
    "ReportFormat",
    "ReportSectionSpec",
    "ReportTemplate",
    "can_regenerate",
    "can_transition",
    "citation_key",
    "default_report_title",
    "failure_message",
    "is_usable_section",
    "no_content_message",
    "normalize_error_message",
    "normalize_report_title",
    "references_title",
    "remap_markers",
    "report_disclaimer",
    "resolve_report_language",
    "template_for",
    "total_characters",
]
