"use client";

import * as React from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import {
  Archive,
  ArchiveRestore,
  ArrowLeft,
  Building2,
  CalendarClock,
  CalendarDays,
  Pencil,
  UserCog,
} from "lucide-react";
import { toast } from "sonner";

import { CaseAssistant } from "@/components/ai/case-assistant";
import { Protected } from "@/components/auth/protected";
import { ErrorState } from "@/components/shared/error-state";
import { LoadingState } from "@/components/shared/loading-state";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { ArchiveCaseDialog } from "@/components/cases/archive-case-dialog";
import { AssignCaseDialog } from "@/components/cases/assign-case-dialog";
import { CaseAssignee } from "@/components/cases/case-assignee";
import { CasePriorityBadge, CaseStatusBadge } from "@/components/cases/case-badges";
import { CasePlaceholderSections } from "@/components/cases/case-placeholder-sections";
import { EditCaseDialog } from "@/components/cases/edit-case-dialog";
import { CaseDocuments } from "@/components/documents/case-documents";
import { CaseReports } from "@/components/reports/case-reports";
import { CaseSearch } from "@/components/search/case-search";
import { CaseTimeline } from "@/components/timeline/case-timeline";
import { useCase, useCaseErrorMessage, useRestoreCase } from "@/hooks/use-cases";
import { useRealtimeResource } from "@/hooks/use-realtime";
import { useDateFormat } from "@/hooks/use-date-format";
import { ROUTES } from "@/lib/routes";
import { PERMISSION } from "@/types/authorization";
import type { CaseUserSummary, LegalCase } from "@/types/case";

/**
 * One case's full record, plus the reserved layout for the modules that will
 * attach to it.
 *
 * Groups the fields the way someone working a case reads them — what the matter
 * is, who is on it, what the court is doing, and what has happened to the record
 * — rather than in the order the API happens to return them.
 */

type DialogKind = "none" | "edit" | "assign" | "archive";

/** One label/value pair inside a details card. */
function DetailRow({
  label,
  value,
  icon: Icon,
}: {
  label: string;
  value: React.ReactNode;
  icon?: React.ComponentType<{ className?: string }>;
}) {
  return (
    <div className="flex flex-col gap-1 py-3 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
      <dt className="flex items-center gap-2 text-sm text-muted-foreground">
        {Icon ? <Icon className="h-4 w-4" /> : null}
        {label}
      </dt>
      <dd className="text-sm text-foreground sm:max-w-[60%] sm:text-end">{value}</dd>
    </div>
  );
}

function DetailsCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="divide-y divide-border">{children}</dl>
      </CardContent>
    </Card>
  );
}

/** An audit actor, or a plain statement that none was recorded. */
function AuditActor({ user }: { user: CaseUserSummary | null }) {
  const t = useTranslations("cases.details");
  if (!user) return <span className="text-muted-foreground">{t("notRecorded")}</span>;
  return <span>{user.fullName}</span>;
}

function CaseDetailsContent({ legalCase }: { legalCase: LegalCase }) {
  const { formatDate, formatDateTime } = useDateFormat();
  const restoreCase = useRestoreCase();
  const t = useTranslations("cases.details");
  const tCases = useTranslations("cases");
  const tForm = useTranslations("cases.form");
  const tActionLabels = useTranslations("cases.actions");
  const tActions = useTranslations("common.actions");
  const errorMessage = useCaseErrorMessage();
  const [dialog, setDialog] = React.useState<DialogKind>("none");

  async function restore() {
    try {
      await restoreCase.mutateAsync(legalCase.id);
      toast.success(tCases("restored", { caseNumber: legalCase.caseNumber }));
    } catch (error) {
      toast.error(errorMessage(error));
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <Button asChild variant="ghost" size="sm" className="-ms-2 w-fit">
        <Link href={ROUTES.cases}>
          <ArrowLeft data-flip-rtl className="h-4 w-4" />
          {t("backToCases")}
        </Link>
      </Button>

      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="flex flex-col gap-2">
          <span className="font-mono text-xs text-muted-foreground">
            {legalCase.caseNumber}
          </span>
          <h2 className="text-xl font-semibold text-foreground">{legalCase.title}</h2>
          <div className="flex flex-wrap items-center gap-2">
            <CaseStatusBadge status={legalCase.status} />
            <CasePriorityBadge priority={legalCase.priority} />
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Protected permission={PERMISSION.casesUpdate}>
            <Button variant="outline" onClick={() => setDialog("edit")}>
              <Pencil className="h-4 w-4" />
              {tActions("edit")}
            </Button>
          </Protected>

          <Protected permission={PERMISSION.casesAssign}>
            <Button variant="outline" onClick={() => setDialog("assign")}>
              <UserCog className="h-4 w-4" />
              {t("assignments")}
            </Button>
          </Protected>

          {legalCase.isArchived ? (
            <Protected permission={PERMISSION.casesUpdate}>
              <Button onClick={restore} disabled={restoreCase.isPending}>
                <ArchiveRestore className="h-4 w-4" />
                {tActionLabels("restore")}
              </Button>
            </Protected>
          ) : (
            <Protected permission={PERMISSION.casesDelete}>
              <Button variant="destructive" onClick={() => setDialog("archive")}>
                <Archive className="h-4 w-4" />
                {tActionLabels("archive")}
              </Button>
            </Protected>
          )}
        </div>
      </div>

      <Separator />

      <div className="grid gap-6 lg:grid-cols-2">
        <DetailsCard title={t("generalInformation")}>
          <DetailRow label={tForm("caseNumber")} value={legalCase.caseNumber} />
          <DetailRow label={tForm("title")} value={legalCase.title} />
          <DetailRow
            label={tForm("description")}
            value={
              legalCase.description ? (
                // `whitespace-pre-line` keeps the author's paragraphs, which the
                // API deliberately preserves rather than collapsing.
                <span className="whitespace-pre-line">{legalCase.description}</span>
              ) : (
                <span className="text-muted-foreground">{t("notProvided")}</span>
              )
            }
          />
          <DetailRow
            label={tForm("category")}
            value={
              legalCase.category ?? (
                <span className="text-muted-foreground">{t("notSet")}</span>
              )
            }
          />
          <DetailRow
            label={tForm("status")}
            value={<CaseStatusBadge status={legalCase.status} />}
          />
          <DetailRow
            label={tForm("priority")}
            value={<CasePriorityBadge priority={legalCase.priority} />}
          />
        </DetailsCard>

        <DetailsCard title={t("assignment")}>
          <DetailRow
            label={tForm("assignedLawyer")}
            value={<CaseAssignee user={legalCase.assignedLawyer} className="sm:justify-end" />}
          />
          <DetailRow
            label={tForm("assignedRepresentative")}
            value={
              <CaseAssignee
                user={legalCase.assignedCourtRepresentative}
                className="sm:justify-end"
              />
            }
          />
        </DetailsCard>

        <DetailsCard title={t("courtInformation")}>
          <DetailRow
            label={tForm("court")}
            icon={Building2}
            value={
              legalCase.courtName ?? (
                <span className="text-muted-foreground">{t("notRecorded")}</span>
              )
            }
          />
          <DetailRow
            label={tForm("filingDate")}
            icon={CalendarDays}
            value={formatDate(legalCase.filingDate, t("notRecorded"))}
          />
          <DetailRow
            label={tForm("nextHearing")}
            icon={CalendarClock}
            value={formatDate(legalCase.nextHearingDate, t("notScheduled"))}
          />
        </DetailsCard>

        <DetailsCard title={t("auditInformation")}>
          <DetailRow
            label={t("createdBy")}
            value={<AuditActor user={legalCase.creator} />}
          />
          <DetailRow
            label={t("updatedBy")}
            value={<AuditActor user={legalCase.updater} />}
          />
          <DetailRow label={t("createdAt")} value={formatDateTime(legalCase.createdAt)} />
          <DetailRow label={t("updatedAt")} value={formatDateTime(legalCase.updatedAt)} />
        </DetailsCard>
      </div>

      <Separator />

      {/* Gated on the capability, not the role: a caller without `documents:view`
          is shown nothing here rather than an empty list they cannot populate. */}
      <Protected permission={PERMISSION.documentsView}>
        <CaseDocuments caseId={legalCase.id} />
        <Separator />
      </Protected>

      {/* Same rule for the history. The API authorizes the request independently
          and refuses a caller who is not party to the case, so this gate is
          presentation only — it stops us rendering a section that would only ever
          show an error. */}
      <Protected permission={PERMISSION.timelineView}>
        <CaseTimeline caseId={legalCase.id} />
        <Separator />
      </Protected>

      {/* Search, pinned to this case. It sits after Documents and Timeline
          because it searches what the first lists and is not part of the second,
          and it is gated on its own capability: retrieval costs a query
          embedding per request, which reading a list does not. `CaseSearch`
          renders nothing without `search:query`, so the separator is inside the
          gate — a rule with no section under it would leave a stray line. */}
      <Protected permission={PERMISSION.searchQuery}>
        <CaseSearch caseId={legalCase.id} />
        <Separator />
      </Protected>

      {/* The assistant, pinned to this case. It sits after Search because it is
          the stage above it — the same passages, answered rather than listed —
          and it is gated on `ai:chat` rather than on `search:query`: a court
          representative may search this case's documents and may not put a
          question to the model about them. `CaseAssistant` renders nothing
          without the capability, so the separator is inside the gate. */}
      <Protected permission={PERMISSION.aiChat}>
        <CaseAssistant caseId={legalCase.id} />
        <Separator />
      </Protected>

      {/* Reports, pinned to this case. Last of the AI sections because it is the
          stage above the assistant — the same pipeline, run a dozen times into a
          document rather than once into an answer — and gated on `reports:view`
          rather than on `ai:chat`: reading a report asks nothing of the pipeline,
          and a deployment may reasonably grant one without the other. Generating
          needs both `reports:generate` and `ai:generate-report`, which the list's
          own controls check. */}
      <Protected permission={PERMISSION.reportsView}>
        <CaseReports caseId={legalCase.id} />
        <Separator />
      </Protected>

      <CasePlaceholderSections />

      <EditCaseDialog
        legalCase={legalCase}
        open={dialog === "edit"}
        onOpenChange={(open) => (open ? undefined : setDialog("none"))}
      />
      <AssignCaseDialog
        legalCase={legalCase}
        open={dialog === "assign"}
        onOpenChange={(open) => (open ? undefined : setDialog("none"))}
      />
      <ArchiveCaseDialog
        legalCase={legalCase}
        open={dialog === "archive"}
        onOpenChange={(open) => (open ? undefined : setDialog("none"))}
      />
    </div>
  );
}

export function CaseDetails({ caseId }: { caseId: string }) {
  const { data, isLoading, isError, error, refetch } = useCase(caseId);
  const t = useTranslations("cases");
  const errorMessage = useCaseErrorMessage();

  // **One subscription for the whole workspace.** Following the case delivers
  // everything that happens inside it — the case itself, every document on it,
  // their extraction and indexing progress, and its timeline — because a
  // document event is authorized against exactly the same rule as a case event
  // (see `CASE_FANOUT_SCOPES` in `apps/api/core/events.py`). Subscribing per
  // document would mean re-subscribing on every page of the document list, for
  // no additional access.
  //
  // Reports are the deliberate exception and are *not* covered: a report is its
  // author's private work product, so the panel below follows each of its own.
  //
  // Called before the early returns, because a hook must run on every render —
  // and unconditionally, because the subscription is a hint the socket may
  // decline rather than something the view depends on.
  useRealtimeResource("case", caseId);

  if (isLoading) return <LoadingState label={t("details.loading")} />;

  if (isError || !data) {
    return (
      <ErrorState
        title={t("errors.detailTitle")}
        description={errorMessage(error)}
        onRetry={() => void refetch()}
      />
    );
  }

  return <CaseDetailsContent legalCase={data} />;
}
