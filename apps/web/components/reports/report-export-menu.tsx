"use client";

import { Download } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Spinner } from "@/components/shared/spinner";
import { reportErrorMessage, useExportReport, useReportMetrics } from "@/hooks/use-reports";
import { usePermissions } from "@/hooks/use-permissions";
import { PERMISSION } from "@/types/authorization";
import {
  REPORT_FORMATS,
  REPORT_FORMAT_LABELS,
  type Report,
  type ReportFormat,
} from "@/types/report";

/**
 * Export control for one finished report.
 *
 * **Only formats this deployment can actually produce are offered.** The
 * monitoring endpoint reports them, and a caller who may read it gets the real
 * list; everyone else is offered both, because the alternative — hiding PDF from
 * every lawyer because they cannot see the metrics endpoint — would be worse than
 * the occasional 503 the message explains. That failure is not silent: a PDF this
 * deployment cannot render answers with a message naming Markdown, and it is
 * shown as a toast.
 *
 * **The button is absent, not disabled, before a report is ready.** A disabled
 * Download beside a progress bar reads as broken; the progress bar already says
 * what is happening.
 */
export function ReportExportMenu({
  report,
  disabled = false,
}: {
  report: Report;
  disabled?: boolean;
}) {
  const exportReport = useExportReport();
  const { can } = usePermissions();
  // Gated, because the metrics endpoint is administrative. A lawyer simply gets
  // the full list — see the component docstring for why that is the right
  // fallback rather than an empty menu.
  const metrics = useReportMetrics({ enabled: can(PERMISSION.reportsMonitor) });

  const formats: readonly ReportFormat[] = metrics.data?.availableFormats?.length
    ? metrics.data.availableFormats
    : REPORT_FORMATS;

  if (report.status !== "completed") return null;

  async function download(format: ReportFormat) {
    try {
      await exportReport.mutateAsync({
        id: report.id,
        format,
        fallbackName: `${report.title}.${format === "pdf" ? "pdf" : "md"}`,
      });
      toast.success(`Report exported as ${REPORT_FORMAT_LABELS[format]}.`);
    } catch (error) {
      toast.error(reportErrorMessage(error));
    }
  }

  const isPending = exportReport.isPending;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button type="button" variant="outline" size="sm" disabled={disabled || isPending}>
          {isPending ? (
            <Spinner className="h-4 w-4 text-current" />
          ) : (
            <Download className="h-4 w-4" aria-hidden="true" />
          )}
          Export
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {formats.map((format) => (
          <DropdownMenuItem key={format} onSelect={() => void download(format)}>
            {REPORT_FORMAT_LABELS[format]}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
