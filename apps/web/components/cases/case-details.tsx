"use client";

import * as React from "react";
import Link from "next/link";
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
import { CaseTimeline } from "@/components/timeline/case-timeline";
import { caseErrorMessage, useCase, useRestoreCase } from "@/hooks/use-cases";
import { formatDate, formatDateTime } from "@/lib/format";
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
      <dd className="text-sm text-foreground sm:max-w-[60%] sm:text-right">{value}</dd>
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
  if (!user) return <span className="text-muted-foreground">Not recorded</span>;
  return <span>{user.fullName}</span>;
}

function CaseDetailsContent({ legalCase }: { legalCase: LegalCase }) {
  const restoreCase = useRestoreCase();
  const [dialog, setDialog] = React.useState<DialogKind>("none");

  async function restore() {
    try {
      await restoreCase.mutateAsync(legalCase.id);
      toast.success(`Case ${legalCase.caseNumber} was restored.`);
    } catch (error) {
      toast.error(caseErrorMessage(error));
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <Button asChild variant="ghost" size="sm" className="-ml-2 w-fit">
        <Link href={ROUTES.cases}>
          <ArrowLeft className="h-4 w-4" />
          Back to cases
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
              Edit
            </Button>
          </Protected>

          <Protected permission={PERMISSION.casesAssign}>
            <Button variant="outline" onClick={() => setDialog("assign")}>
              <UserCog className="h-4 w-4" />
              Assignments
            </Button>
          </Protected>

          {legalCase.isArchived ? (
            <Protected permission={PERMISSION.casesUpdate}>
              <Button onClick={restore} disabled={restoreCase.isPending}>
                <ArchiveRestore className="h-4 w-4" />
                Restore
              </Button>
            </Protected>
          ) : (
            <Protected permission={PERMISSION.casesDelete}>
              <Button variant="destructive" onClick={() => setDialog("archive")}>
                <Archive className="h-4 w-4" />
                Archive
              </Button>
            </Protected>
          )}
        </div>
      </div>

      <Separator />

      <div className="grid gap-6 lg:grid-cols-2">
        <DetailsCard title="General information">
          <DetailRow label="Case number" value={legalCase.caseNumber} />
          <DetailRow label="Title" value={legalCase.title} />
          <DetailRow
            label="Description"
            value={
              legalCase.description ? (
                // `whitespace-pre-line` keeps the author's paragraphs, which the
                // API deliberately preserves rather than collapsing.
                <span className="whitespace-pre-line">{legalCase.description}</span>
              ) : (
                <span className="text-muted-foreground">Not provided</span>
              )
            }
          />
          <DetailRow
            label="Category"
            value={
              legalCase.category ?? <span className="text-muted-foreground">Not set</span>
            }
          />
          <DetailRow label="Status" value={<CaseStatusBadge status={legalCase.status} />} />
          <DetailRow
            label="Priority"
            value={<CasePriorityBadge priority={legalCase.priority} />}
          />
        </DetailsCard>

        <DetailsCard title="Assignment">
          <DetailRow
            label="Assigned lawyer"
            value={<CaseAssignee user={legalCase.assignedLawyer} className="sm:justify-end" />}
          />
          <DetailRow
            label="Assigned court representative"
            value={
              <CaseAssignee
                user={legalCase.assignedCourtRepresentative}
                className="sm:justify-end"
              />
            }
          />
        </DetailsCard>

        <DetailsCard title="Court information">
          <DetailRow
            label="Court"
            icon={Building2}
            value={
              legalCase.courtName ?? (
                <span className="text-muted-foreground">Not recorded</span>
              )
            }
          />
          <DetailRow
            label="Filing date"
            icon={CalendarDays}
            value={formatDate(legalCase.filingDate, "Not recorded")}
          />
          <DetailRow
            label="Next hearing"
            icon={CalendarClock}
            value={formatDate(legalCase.nextHearingDate, "Not scheduled")}
          />
        </DetailsCard>

        <DetailsCard title="Audit information">
          <DetailRow label="Created by" value={<AuditActor user={legalCase.creator} />} />
          <DetailRow label="Updated by" value={<AuditActor user={legalCase.updater} />} />
          <DetailRow label="Created at" value={formatDateTime(legalCase.createdAt)} />
          <DetailRow label="Updated at" value={formatDateTime(legalCase.updatedAt)} />
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

  if (isLoading) return <LoadingState label="Loading case…" />;

  if (isError || !data) {
    return (
      <ErrorState
        title="Could not load this case"
        description={caseErrorMessage(error)}
        onRetry={() => void refetch()}
      />
    );
  }

  return <CaseDetailsContent legalCase={data} />;
}
