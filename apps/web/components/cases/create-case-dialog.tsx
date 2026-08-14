"use client";

import * as React from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useTranslations } from "next-intl";
import { FilePlus2 } from "lucide-react";
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
import { Spinner } from "@/components/shared/spinner";
import { CaseFormFieldset } from "@/components/cases/case-form";
import { usePermissions } from "@/hooks/use-permissions";
import { caseFieldErrors, useCaseErrorMessage, useCreateCase } from "@/hooks/use-cases";
import { useFieldError } from "@/hooks/use-field-error";
import { createCaseFormSchema, type CreateCaseFormValues } from "@/lib/validation/case";
import { PERMISSION } from "@/types/authorization";
import { CASE_STATUSES } from "@/types/case";

/**
 * Create Case dialog.
 *
 * Owns nothing but presentation and form state: the request, cache
 * invalidation, and error translation all live in `useCreateCase` /
 * `useCaseErrorMessage`, per the standard that components carry no business logic.
 *
 * Server-side validation failures are mapped back onto the offending fields
 * rather than shown only as a banner, so a rejected case number lands next to
 * the case-number input — the same place the client-side rule would complain.
 */

/**
 * A new case starts as a draft. Only the statuses reachable *from* draft are
 * offered alongside it, so the create form cannot open a case in a state the
 * lifecycle would not allow it to reach.
 */
const CREATABLE_STATUSES = CASE_STATUSES.filter(
  (status) => status === "draft" || status === "open",
);

const EMPTY_FORM: CreateCaseFormValues = {
  caseNumber: "",
  title: "",
  description: "",
  category: "",
  status: "draft",
  priority: "medium",
  courtName: "",
  filingDate: "",
  nextHearingDate: "",
  assignedLawyerId: "",
  assignedCourtRepresentativeId: "",
};

export function CreateCaseDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const createCase = useCreateCase();
  const t = useTranslations("cases.createDialog");
  const tActions = useTranslations("common.actions");
  const errorMessage = useCaseErrorMessage();
  const fieldError = useFieldError();
  const { can } = usePermissions();
  const canAssign = can(PERMISSION.casesAssign);
  const [formError, setFormError] = React.useState<string | null>(null);

  const form = useForm<CreateCaseFormValues>({
    resolver: zodResolver(createCaseFormSchema),
    defaultValues: EMPTY_FORM,
    mode: "onBlur",
  });

  const {
    control,
    register,
    handleSubmit,
    reset,
    setError,
    setValue,
    formState: { errors },
  } = form;

  // `useWatch` rather than `watch()`: it is a real hook, so the React Compiler
  // can memoize this component instead of skipping it, and it re-renders only
  // for the fields named here rather than for every keystroke in the form.
  const status = useWatch({ control, name: "status" });
  const priority = useWatch({ control, name: "priority" });
  const assignedLawyerId = useWatch({ control, name: "assignedLawyerId" });
  const assignedCourtRepresentativeId = useWatch({
    control,
    name: "assignedCourtRepresentativeId",
  });

  // Drop a stale server error as the dialog reopens. Adjusted during render
  // rather than in an effect, so the previous attempt's banner is never painted
  // again (https://react.dev/learn/you-might-not-need-an-effect).
  const [wasOpen, setWasOpen] = React.useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) setFormError(null);
  }

  // Start from a clean form each time the dialog opens; a half-filled draft from
  // a cancelled attempt is more confusing than helpful. This one *is* an effect:
  // `reset` mutates React Hook Form's store, which is exactly the external-system
  // synchronization effects are for.
  React.useEffect(() => {
    if (open) reset(EMPTY_FORM);
  }, [open, reset]);

  const onSubmit = handleSubmit(async (values) => {
    setFormError(null);

    try {
      const created = await createCase.mutateAsync({
        // An empty case number is omitted rather than sent blank, which is what
        // asks the API to generate the next one in the series.
        caseNumber: values.caseNumber || null,
        title: values.title,
        description: values.description || null,
        category: values.category || null,
        status: values.status,
        priority: values.priority,
        courtName: values.courtName || null,
        filingDate: values.filingDate || null,
        nextHearingDate: values.nextHearingDate || null,
        assignedLawyerId: values.assignedLawyerId || null,
        assignedCourtRepresentativeId: values.assignedCourtRepresentativeId || null,
      });

      toast.success(t("created", { caseNumber: created.caseNumber }));
      onOpenChange(false);
    } catch (error) {
      const fields = caseFieldErrors(error);
      for (const [field, message] of Object.entries(fields)) {
        setError(field as keyof CreateCaseFormValues, { message });
      }
      // Only surface the banner when nothing could be attached to a field —
      // otherwise the same complaint would appear twice.
      if (Object.keys(fields).length === 0) setFormError(errorMessage(error));
    }
  });

  const isPending = createCase.isPending;

  return (
    <Dialog open={open} onOpenChange={isPending ? undefined : onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t("title")}</DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </DialogHeader>

        <form onSubmit={onSubmit} noValidate className="flex flex-col gap-6">
          <CaseFormFieldset
            idPrefix="create-case"
            caseNumber={register("caseNumber")}
            fields={{
              title: register("title"),
              description: register("description"),
              category: register("category"),
              courtName: register("courtName"),
              filingDate: register("filingDate"),
              nextHearingDate: register("nextHearingDate"),
            }}
            errors={{
              caseNumber: fieldError(errors.caseNumber?.message),
              title: fieldError(errors.title?.message),
              description: fieldError(errors.description?.message),
              category: fieldError(errors.category?.message),
              status: fieldError(errors.status?.message),
              priority: fieldError(errors.priority?.message),
              courtName: fieldError(errors.courtName?.message),
              filingDate: fieldError(errors.filingDate?.message),
              nextHearingDate: fieldError(errors.nextHearingDate?.message),
              assignedLawyerId: fieldError(errors.assignedLawyerId?.message),
              assignedCourtRepresentativeId: fieldError(errors.assignedCourtRepresentativeId?.message),
            }}
            status={status}
            statusOptions={CREATABLE_STATUSES}
            onStatusChange={(value) => setValue("status", value, { shouldValidate: true })}
            priority={priority}
            onPriorityChange={(value) => setValue("priority", value, { shouldValidate: true })}
            assignedLawyerId={assignedLawyerId}
            onAssignedLawyerChange={(value) => setValue("assignedLawyerId", value)}
            assignedCourtRepresentativeId={assignedCourtRepresentativeId}
            onAssignedCourtRepresentativeChange={(value) =>
              setValue("assignedCourtRepresentativeId", value)
            }
            canAssign={canAssign}
            disabled={isPending}
            formError={formError}
          />

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={isPending}
            >
              {tActions("cancel")}
            </Button>
            <Button type="submit" disabled={isPending}>
              {isPending ? (
                <>
                  <Spinner className="h-4 w-4 text-current" />
                  {t("creating")}
                </>
              ) : (
                <>
                  <FilePlus2 className="h-4 w-4" />
                  {t("submit")}
                </>
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
