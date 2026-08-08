"use client";

import * as React from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { Sparkles } from "lucide-react";
import { useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Spinner } from "@/components/shared/spinner";
import { useDocumentCases } from "@/hooks/use-document-cases";
import {
  reportErrorMessage,
  useGenerateReport,
  useReportTemplates,
} from "@/hooks/use-reports";
import {
  MAX_TITLE_LENGTH,
  generateReportSchema,
  type GenerateReportValues,
} from "@/lib/validation/report";
import {
  REPORT_LANGUAGES,
  REPORT_LANGUAGE_LABELS,
  type ReportLanguage,
  type ReportType,
} from "@/types/report";

/**
 * Generate-report dialog: choose a case, a report type, and a language.
 *
 * Owns nothing but presentation and form state: the request, cache invalidation,
 * and error translation all live in `useGenerateReport` / `reportErrorMessage`,
 * per the standard that components carry no business logic.
 *
 * **The report types come from the server**, not from a list in this file. Adding
 * a sixth template is a server-side entry, and this picker — including the
 * section preview below it — follows without a frontend change, which is the
 * *"allow future report templates without redesign"* the spec asks for made true
 * of the client as well.
 *
 * **The sections a report will contain are shown before it is generated.** A
 * report costs a model call per section and takes minutes, so "what am I about to
 * get" is a question that must be answerable *before* pressing the button rather
 * than by generating one to find out.
 *
 * There is deliberately **no retrieval tuning** here — no top-K, no similarity
 * floor, no document filters. The API accepts all three, but they are per-request
 * tuning for a caller who knows the pipeline, and a form that offered them would
 * ask a lawyer to choose a cosine similarity threshold.
 */

const EMPTY_FORM: GenerateReportValues = {
  caseId: "",
  reportType: "case_summary",
  language: null,
  title: null,
};

export function GenerateReportDialog({
  open,
  onOpenChange,
  caseId,
  onGenerated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Pre-selected and locked when generating from inside a case workspace. */
  caseId?: string;
  /** Called with the queued run, so the caller can open it and watch it progress. */
  onGenerated?: (reportId: string) => void;
}) {
  const generate = useGenerateReport();
  const cases = useDocumentCases({ enabled: open && !caseId });
  const [formError, setFormError] = React.useState<string | null>(null);

  const defaults = React.useMemo<GenerateReportValues>(
    () => ({ ...EMPTY_FORM, caseId: caseId ?? "" }),
    [caseId],
  );

  const form = useForm<GenerateReportValues>({
    resolver: zodResolver(generateReportSchema),
    defaultValues: defaults,
    mode: "onBlur",
  });

  const {
    control,
    register,
    handleSubmit,
    reset,
    setValue,
    formState: { errors },
  } = form;

  // `useWatch` rather than `watch()`: it is a real hook, so the React Compiler
  // can memoize this component instead of skipping it, and it re-renders only
  // for the fields named here rather than for every keystroke in the form.
  const selectedCaseId = useWatch({ control, name: "caseId" });
  const reportType = useWatch({ control, name: "reportType" });
  const language = useWatch({ control, name: "language" });

  // The catalogue is labelled in the language the report will be written in, so
  // the titles in this picker are byte-identical to the headings the generated
  // report will carry.
  const templates = useReportTemplates({
    enabled: open,
    language: language ?? undefined,
  });
  const selected = templates.data?.find((template) => template.reportType === reportType);

  // Drop a stale server error as the dialog reopens. Adjusted during render
  // rather than in an effect, so the previous attempt's banner is never painted
  // again (https://react.dev/learn/you-might-not-need-an-effect).
  const [wasOpen, setWasOpen] = React.useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) setFormError(null);
  }

  // Start from a clean form each time the dialog opens. This one *is* an effect:
  // `reset` mutates React Hook Form's store, which is exactly the
  // external-system synchronization effects are for.
  React.useEffect(() => {
    if (open) reset(defaults);
  }, [open, reset, defaults]);

  const onSubmit = handleSubmit(async (values) => {
    setFormError(null);

    try {
      const report = await generate.mutateAsync({
        caseId: values.caseId,
        reportType: values.reportType,
        language: values.language ?? null,
        title: values.title?.trim() || null,
      });

      // Deliberately not "your report is ready": it is queued, and a toast that
      // said otherwise would send the user looking for a document that does not
      // exist yet.
      toast.success("Report queued. It will appear as it is written.");
      onOpenChange(false);
      onGenerated?.(report.id);
    } catch (error) {
      setFormError(reportErrorMessage(error));
    }
  });

  const isPending = generate.isPending;

  return (
    <Dialog open={open} onOpenChange={isPending ? undefined : onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Generate report</DialogTitle>
          <DialogDescription>
            The report is written from this case&apos;s indexed documents, section by
            section, with a citation for every finding. Generation runs in the background —
            you can close this and come back.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={onSubmit} noValidate className="flex flex-col gap-6">
          <div className="flex flex-col gap-4">
            {formError ? (
              <p role="alert" className="text-sm text-destructive">
                {formError}
              </p>
            ) : null}

            {caseId ? null : (
              <div className="flex flex-col gap-2">
                <Label htmlFor="generate-report-case">Case</Label>
                <Select
                  value={selectedCaseId || undefined}
                  onValueChange={(value) => setValue("caseId", value, { shouldValidate: true })}
                  disabled={isPending}
                >
                  <SelectTrigger id="generate-report-case">
                    <SelectValue
                      placeholder={cases.isLoading ? "Loading cases…" : "Select a case"}
                    />
                  </SelectTrigger>
                  <SelectContent>
                    {cases.cases.map((legalCase) => (
                      <SelectItem key={legalCase.id} value={legalCase.id}>
                        {legalCase.caseNumber} — {legalCase.title}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {errors.caseId ? (
                  <p role="alert" className="text-sm text-destructive">
                    {errors.caseId.message}
                  </p>
                ) : null}
              </div>
            )}

            <div className="flex flex-col gap-2">
              <Label htmlFor="generate-report-type">Report type</Label>
              <Select
                value={reportType}
                onValueChange={(value) =>
                  setValue("reportType", value as ReportType, { shouldValidate: true })
                }
                disabled={isPending || templates.isLoading}
              >
                <SelectTrigger id="generate-report-type">
                  <SelectValue
                    placeholder={templates.isLoading ? "Loading report types…" : "Select a type"}
                  />
                </SelectTrigger>
                <SelectContent>
                  {(templates.data ?? []).map((template) => (
                    <SelectItem key={template.reportType} value={template.reportType}>
                      {template.title}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {selected ? (
                <p className="text-xs text-muted-foreground">{selected.description}</p>
              ) : null}
              {errors.reportType ? (
                <p role="alert" className="text-sm text-destructive">
                  {errors.reportType.message}
                </p>
              ) : null}
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="generate-report-language">Language</Label>
              <Select
                value={language ?? "fr"}
                onValueChange={(value) =>
                  setValue("language", value as ReportLanguage, { shouldValidate: true })
                }
                disabled={isPending}
              >
                <SelectTrigger id="generate-report-language">
                  <SelectValue placeholder="Select a language" />
                </SelectTrigger>
                <SelectContent>
                  {REPORT_LANGUAGES.map((option) => (
                    <SelectItem key={option} value={option}>
                      {REPORT_LANGUAGE_LABELS[option]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                Every section is written in this language, whatever language the
                documents are in.
              </p>
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="generate-report-title">Title (optional)</Label>
              <Input
                id="generate-report-title"
                maxLength={MAX_TITLE_LENGTH}
                disabled={isPending}
                placeholder="Leave empty to name it after the report type and case number"
                {...register("title")}
              />
              {errors.title ? (
                <p role="alert" className="text-sm text-destructive">
                  {errors.title.message}
                </p>
              ) : null}
            </div>

            {selected && selected.sections.length > 0 ? (
              <div className="rounded-lg border border-border bg-muted/40 p-3">
                <p className="text-xs font-medium text-foreground">
                  This report will contain {selected.sectionCount} section
                  {selected.sectionCount === 1 ? "" : "s"}
                </p>
                <ol className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                  {selected.sections.map((section, index) => (
                    <li key={section.key} dir="auto">
                      {index + 1}. {section.title}
                    </li>
                  ))}
                </ol>
                <p className="mt-2 text-xs text-muted-foreground">
                  A section the case file does not cover says so, rather than being
                  filled in.
                </p>
              </div>
            ) : null}
          </div>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={isPending}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={isPending}>
              {isPending ? (
                <>
                  <Spinner className="h-4 w-4 text-current" />
                  Queueing…
                </>
              ) : (
                <>
                  <Sparkles className="h-4 w-4" />
                  Generate
                </>
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
