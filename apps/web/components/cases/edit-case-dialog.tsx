"use client";

import * as React from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { Save } from "lucide-react";
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
import { caseErrorMessage, caseFieldErrors, useUpdateCase } from "@/hooks/use-cases";
import { editCaseFormSchema, type EditCaseFormValues } from "@/lib/validation/case";
import { PERMISSION } from "@/types/authorization";
import type { LegalCase } from "@/types/case";
import type { UpdateCasePayload } from "@/types/case-management";

/**
 * Edit Case dialog.
 *
 * Deliberately sends **only the fields that changed**. A PATCH that echoed every
 * field back would work, but it would also overwrite a concurrent edit by
 * another user with values this dialog loaded before that edit happened —
 * and, worse, it would submit the assignment fields on every save, which a
 * caller without `cases:assign` is not permitted to write at all.
 *
 * The case number is absent: it identifies the case and is immutable once filed.
 */

/** Turn a case into the form's values; `null` becomes an empty input. */
function toFormValues(legalCase: LegalCase): EditCaseFormValues {
  return {
    title: legalCase.title,
    description: legalCase.description ?? "",
    category: legalCase.category ?? "",
    status: legalCase.status,
    priority: legalCase.priority,
    courtName: legalCase.courtName ?? "",
    filingDate: legalCase.filingDate ?? "",
    nextHearingDate: legalCase.nextHearingDate ?? "",
    assignedLawyerId: legalCase.assignedLawyerId ?? "",
    assignedCourtRepresentativeId: legalCase.assignedCourtRepresentativeId ?? "",
  };
}

/**
 * The fields whose value differs from the case being edited.
 *
 * An emptied optional field is a deliberate clear, which the API expresses as an
 * explicit `null` — distinct from omitting the key, which means "leave it".
 */
export function changedCaseFields(
  legalCase: LegalCase,
  values: EditCaseFormValues,
  { includeAssignments }: { includeAssignments: boolean },
): UpdateCasePayload {
  const payload: UpdateCasePayload = {};

  if (values.title !== legalCase.title) payload.title = values.title;
  if (values.status !== legalCase.status) payload.status = values.status;
  if (values.priority !== legalCase.priority) payload.priority = values.priority;

  const optional = [
    ["description", values.description, legalCase.description],
    ["category", values.category, legalCase.category],
    ["courtName", values.courtName, legalCase.courtName],
    ["filingDate", values.filingDate, legalCase.filingDate],
    ["nextHearingDate", values.nextHearingDate, legalCase.nextHearingDate],
  ] as const;

  for (const [field, next, current] of optional) {
    const value = next === "" ? null : next;
    if (value !== (current ?? null)) payload[field] = value;
  }

  if (includeAssignments) {
    const lawyer = values.assignedLawyerId || null;
    if (lawyer !== (legalCase.assignedLawyerId ?? null)) payload.assignedLawyerId = lawyer;

    const representative = values.assignedCourtRepresentativeId || null;
    if (representative !== (legalCase.assignedCourtRepresentativeId ?? null)) {
      payload.assignedCourtRepresentativeId = representative;
    }
  }

  return payload;
}

export function EditCaseDialog({
  legalCase,
  open,
  onOpenChange,
}: {
  legalCase: LegalCase | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const updateCase = useUpdateCase();
  const { can } = usePermissions();
  const canAssign = can(PERMISSION.casesAssign);
  const [formError, setFormError] = React.useState<string | null>(null);

  const form = useForm<EditCaseFormValues>({
    resolver: zodResolver(editCaseFormSchema),
    defaultValues: {
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
    },
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

  // See the note in `create-case-dialog.tsx`: `useWatch` keeps this component
  // memoizable and limits re-renders to the fields that need them.
  const status = useWatch({ control, name: "status" });
  const priority = useWatch({ control, name: "priority" });
  const assignedLawyerId = useWatch({ control, name: "assignedLawyerId" });
  const assignedCourtRepresentativeId = useWatch({
    control,
    name: "assignedCourtRepresentativeId",
  });

  // Drop a stale server error when the dialog is opened on a (possibly
  // different) case. Adjusted during render rather than in an effect, so the
  // previous attempt's banner is never painted again
  // (https://react.dev/learn/you-might-not-need-an-effect).
  const target = open ? (legalCase?.id ?? null) : null;
  const [previousTarget, setPreviousTarget] = React.useState(target);
  if (target !== previousTarget) {
    setPreviousTarget(target);
    if (target) setFormError(null);
  }

  // Reload the form whenever a different case is opened, so the dialog never
  // shows the previous target's details. An effect because `reset` mutates React
  // Hook Form's store — the external-system synchronization effects exist for.
  React.useEffect(() => {
    if (open && legalCase) reset(toFormValues(legalCase));
  }, [open, legalCase, reset]);

  const onSubmit = handleSubmit(async (values) => {
    if (!legalCase) return;
    setFormError(null);

    const payload = changedCaseFields(legalCase, values, { includeAssignments: canAssign });

    if (Object.keys(payload).length === 0) {
      // The API rejects an empty PATCH; there is genuinely nothing to save.
      toast.info("No changes to save.");
      onOpenChange(false);
      return;
    }

    try {
      const updated = await updateCase.mutateAsync({ id: legalCase.id, payload });
      toast.success(`Case ${updated.caseNumber} was updated.`);
      onOpenChange(false);
    } catch (error) {
      const fields = caseFieldErrors(error);
      for (const [field, message] of Object.entries(fields)) {
        setError(field as keyof EditCaseFormValues, { message });
      }
      if (Object.keys(fields).length === 0) setFormError(caseErrorMessage(error));
    }
  });

  const isPending = updateCase.isPending;

  return (
    <Dialog open={open} onOpenChange={isPending ? undefined : onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Edit case</DialogTitle>
          <DialogDescription>
            {legalCase
              ? `Update ${legalCase.caseNumber} — its details, status, priority, court, or assignments.`
              : "Update this case."}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={onSubmit} noValidate className="flex flex-col gap-6">
          <CaseFormFieldset
            idPrefix="edit-case"
            fields={{
              title: register("title"),
              description: register("description"),
              category: register("category"),
              courtName: register("courtName"),
              filingDate: register("filingDate"),
              nextHearingDate: register("nextHearingDate"),
            }}
            errors={{
              title: errors.title?.message,
              description: errors.description?.message,
              category: errors.category?.message,
              status: errors.status?.message,
              priority: errors.priority?.message,
              courtName: errors.courtName?.message,
              filingDate: errors.filingDate?.message,
              nextHearingDate: errors.nextHearingDate?.message,
              assignedLawyerId: errors.assignedLawyerId?.message,
              assignedCourtRepresentativeId: errors.assignedCourtRepresentativeId?.message,
            }}
            status={status}
            // Only the moves the API would accept, taken from the case itself —
            // so the lifecycle rules live on the server and the UI cannot offer
            // a transition that is about to be refused.
            statusOptions={legalCase?.allowedTransitions ?? []}
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
              Cancel
            </Button>
            <Button type="submit" disabled={isPending}>
              {isPending ? (
                <>
                  <Spinner className="h-4 w-4 text-current" />
                  Saving…
                </>
              ) : (
                <>
                  <Save className="h-4 w-4" />
                  Save changes
                </>
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
