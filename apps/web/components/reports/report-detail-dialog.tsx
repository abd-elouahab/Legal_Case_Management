"use client";

import { RefreshCw, TriangleAlert } from "lucide-react";
import { toast } from "sonner";

import { CitationList } from "@/components/ai/citation-list";
import { ReportExportMenu } from "@/components/reports/report-export-menu";
import { ReportProgress } from "@/components/reports/report-progress";
import { ReportSections } from "@/components/reports/report-sections";
import { ReportStatusBadge } from "@/components/reports/report-status-badge";
import { ErrorState } from "@/components/shared/error-state";
import { Spinner } from "@/components/shared/spinner";
import { Protected } from "@/components/auth/protected";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import {
  reportErrorMessage,
  useRegenerateReport,
  useReport,
  useReportCompletionSync,
} from "@/hooks/use-reports";
import { useRealtimeResource } from "@/hooks/use-realtime";
import { formatDateTime } from "@/lib/format";
import { PERMISSION } from "@/types/authorization";
import { reportFailureLabel, reportTypeLabel } from "@/types/report";

/**
 * One report, opened from the history or straight after being queued.
 *
 * **A dialog rather than a route**, and for the same reason the AI Assistant
 * keeps its open conversation in component state: a report identifier in the URL
 * would be written to the browser's history and to the `Referer` header of
 * anything the page loads next — the same three logs the API refuses to put a
 * question into by making search and messaging POSTs. A report is a generated
 * interpretation of a client's file, and its identifier is a handle to one.
 *
 * It renders four states, and each is a real answer rather than a variation on
 * "loading":
 *
 * * **queued or generating** — the progress bar, with a real denominator, and no
 *   empty section headings pretending the report exists yet;
 * * **failed** — the platform's own explanation of the cause, plus Regenerate,
 *   because almost every failure here is transient and the remedy is one button;
 * * **ready** — the sections, the reference list, and the export menu;
 * * **gone** — a 404, which the API deliberately returns for a report that was
 *   deleted *and* for one belonging to somebody else.
 *
 * The **disclaimer is the server's**, shown on every report: a document that looks
 * like a lawyer's work product and is not must say so on its face.
 */
export function ReportDetailDialog({
  reportId,
  open,
  onOpenChange,
}: {
  reportId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const report = useReport(reportId, { enabled: open });
  const regenerate = useRegenerateReport();

  // **A report is followed on its own topic, never on its case's.** A report is
  // its author's private work product, so the server refuses to fan its events
  // into the case channel (`CASE_FANOUT_SCOPES` in `apps/api/core/events.py`) —
  // which means a case follower learns *that* a report exists, from the case
  // timeline, and only its author watches it being written.
  //
  // Subscribed while the dialog is closed too, and deliberately: `reportId` is
  // set the moment a row is chosen, and the run whose progress this shows was
  // usually queued from the list behind it. The polling below is unchanged and
  // remains the fallback — the subscription makes the bar move between polls
  // rather than replacing them.
  useRealtimeResource("report", reportId);

  // A finished run appends `report_generated` to the case's history, which
  // nothing on the client caused — so the poll's own result is what invalidates
  // the timeline and the list.
  useReportCompletionSync(report.data);

  const detail = report.data;

  async function onRegenerate() {
    if (!reportId) return;
    try {
      await regenerate.mutateAsync(reportId);
      toast.success("Report queued again. It will update as it is rewritten.");
    } catch (error) {
      toast.error(reportErrorMessage(error));
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-2" dir="auto">
            {detail ? detail.title : "Report"}
            {detail ? <ReportStatusBadge status={detail.status} /> : null}
          </DialogTitle>
          <DialogDescription>
            {detail
              ? `${reportTypeLabel(detail.reportType)} · requested ${formatDateTime(detail.createdAt)}`
              : "Loading the report."}
          </DialogDescription>
        </DialogHeader>

        {report.isLoading ? (
          <div className="flex flex-col gap-3" aria-busy="true">
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : report.isError ? (
          <ErrorState
            title="This report could not be opened"
            description={reportErrorMessage(report.error)}
            onRetry={() => void report.refetch()}
          />
        ) : detail ? (
          <div className="flex flex-col gap-5">
            {detail.isActive ? (
              <div className="rounded-lg border border-border bg-muted/40 p-4">
                <ReportProgress report={detail} />
                <p className="mt-3 text-xs text-muted-foreground">
                  Each section is retrieved and written separately, so a long case takes
                  a few minutes. You can close this — the report keeps building.
                </p>
              </div>
            ) : null}

            {detail.status === "failed" ? (
              <div
                role="alert"
                className="flex items-start gap-3 rounded-lg border border-destructive/30 bg-destructive/10 p-4"
              >
                <TriangleAlert
                  className="mt-0.5 h-4 w-4 shrink-0 text-destructive"
                  aria-hidden="true"
                />
                <div className="flex flex-col gap-1">
                  <p className="text-sm font-medium text-destructive">
                    {reportFailureLabel(detail.errorCode)}
                  </p>
                  <p className="text-sm text-secondary-foreground">
                    {detail.errorMessage ??
                      "The report could not be generated. Try generating it again."}
                  </p>
                </div>
              </div>
            ) : null}

            {detail.status === "completed" ? (
              <>
                <p className="text-xs text-muted-foreground">
                  {detail.groundedSections ?? 0} of {detail.sections.length} section
                  {detail.sections.length === 1 ? "" : "s"} grounded in{" "}
                  {detail.documentCount} document{detail.documentCount === 1 ? "" : "s"}
                  {detail.finishedAt ? ` · generated ${formatDateTime(detail.finishedAt)}` : ""}
                </p>

                <ReportSections sections={detail.sections} language={detail.language} />

                {detail.citations.length > 0 ? (
                  <>
                    <Separator />
                    <CitationList citations={detail.citations} />
                  </>
                ) : null}

                <Separator />
                <p className="text-xs italic text-muted-foreground" dir="auto">
                  {detail.disclaimer}
                </p>
              </>
            ) : null}
          </div>
        ) : null}

        <DialogFooter className="gap-2 sm:justify-between">
          <div className="flex gap-2">
            {detail && detail.isTerminal ? (
              <Protected allOf={[PERMISSION.reportsGenerate, PERMISSION.aiGenerateReport]}>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => void onRegenerate()}
                  disabled={regenerate.isPending}
                >
                  {regenerate.isPending ? (
                    <Spinner className="h-4 w-4 text-current" />
                  ) : (
                    <RefreshCw className="h-4 w-4" aria-hidden="true" />
                  )}
                  Regenerate
                </Button>
              </Protected>
            ) : null}
            {detail ? <ReportExportMenu report={detail} /> : null}
          </div>

          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
