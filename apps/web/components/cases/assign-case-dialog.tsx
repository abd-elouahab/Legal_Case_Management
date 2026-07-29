"use client";

import * as React from "react";
import { AlertCircle, UserCog } from "lucide-react";
import { toast } from "sonner";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Spinner } from "@/components/shared/spinner";
import { useCaseAssignees } from "@/hooks/use-case-assignees";
import { caseErrorMessage, useUpdateCaseAssignments } from "@/hooks/use-cases";
import type { LegalCase } from "@/types/case";
import type { UpdateCaseAssignmentsPayload } from "@/types/case-management";
import type { UserRole } from "@/types/user";

/**
 * Assignment dialog: assign, change, or remove the people on a case.
 *
 * Separate from the edit dialog because assignment is a separate capability
 * (`cases:assign`) and its own endpoint — a user who may edit a case's details
 * is not necessarily the one who decides who works on it.
 *
 * Only the assignments that actually changed are sent, so re-saving without
 * touching a select does not overwrite a colleague's concurrent reassignment.
 */

/** See `case-form.tsx`: a Radix `SelectItem` cannot carry an empty value. */
const UNASSIGNED = "__unassigned__";

/** One assignment position, with the users eligible to fill it. */
function AssignmentSelect({
  id,
  label,
  role,
  value,
  onChange,
  disabled,
}: {
  id: string;
  label: string;
  role: UserRole;
  value: string;
  onChange: (id: string) => void;
  disabled: boolean;
}) {
  const { users, isLoading } = useCaseAssignees(role);

  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={id}>{label}</Label>
      <Select
        value={value || UNASSIGNED}
        disabled={disabled || isLoading}
        onValueChange={(next) => onChange(next === UNASSIGNED ? "" : next)}
      >
        <SelectTrigger id={id}>
          <SelectValue placeholder={isLoading ? "Loading…" : "Unassigned"} />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={UNASSIGNED}>Unassigned</SelectItem>
          {users.map((user) => (
            <SelectItem key={user.id} value={user.id}>
              {user.fullName}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {!isLoading && users.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          No active accounts hold this role yet.
        </p>
      ) : null}
    </div>
  );
}

export function AssignCaseDialog({
  legalCase,
  open,
  onOpenChange,
}: {
  legalCase: LegalCase | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const updateAssignments = useUpdateCaseAssignments();
  const [error, setError] = React.useState<string | null>(null);
  const [lawyerId, setLawyerId] = React.useState("");
  const [representativeId, setRepresentativeId] = React.useState("");

  // Reload the selects whenever a different case is opened, and drop a stale
  // error. Adjusted during render rather than in an effect, so the previous
  // target's assignments are never painted
  // (https://react.dev/learn/you-might-not-need-an-effect).
  const target = open ? (legalCase?.id ?? null) : null;
  const [previousTarget, setPreviousTarget] = React.useState<string | null>(null);
  if (target !== previousTarget) {
    setPreviousTarget(target);
    setError(null);
    setLawyerId(legalCase?.assignedLawyerId ?? "");
    setRepresentativeId(legalCase?.assignedCourtRepresentativeId ?? "");
  }

  async function confirm() {
    if (!legalCase) return;
    setError(null);

    const payload: UpdateCaseAssignmentsPayload = {};
    // `|| null` turns "unassigned" into the explicit null the API reads as
    // "remove this assignment"; an unchanged position is omitted entirely.
    if ((lawyerId || null) !== (legalCase.assignedLawyerId ?? null)) {
      payload.assignedLawyerId = lawyerId || null;
    }
    if ((representativeId || null) !== (legalCase.assignedCourtRepresentativeId ?? null)) {
      payload.assignedCourtRepresentativeId = representativeId || null;
    }

    if (Object.keys(payload).length === 0) {
      toast.info("No assignment changes to save.");
      onOpenChange(false);
      return;
    }

    try {
      const updated = await updateAssignments.mutateAsync({ id: legalCase.id, payload });
      toast.success(`Assignments for ${updated.caseNumber} were updated.`);
      onOpenChange(false);
    } catch (cause) {
      setError(caseErrorMessage(cause));
    }
  }

  const isPending = updateAssignments.isPending;

  return (
    <Dialog open={open} onOpenChange={isPending ? undefined : onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Manage assignments</DialogTitle>
          <DialogDescription>
            {legalCase
              ? `Choose who works on ${legalCase.caseNumber}. Selecting "Unassigned" removes the current assignment.`
              : "Choose who works on this case."}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-5">
          {error ? (
            <Alert variant="destructive" role="alert">
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : null}

          <AssignmentSelect
            id="assign-case-lawyer"
            label="Assigned lawyer"
            role="lawyer"
            value={lawyerId}
            onChange={setLawyerId}
            disabled={isPending}
          />

          <AssignmentSelect
            id="assign-case-representative"
            label="Assigned court representative"
            role="court"
            value={representativeId}
            onChange={setRepresentativeId}
            disabled={isPending}
          />
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
          <Button type="button" onClick={confirm} disabled={isPending}>
            {isPending ? (
              <>
                <Spinner className="h-4 w-4 text-current" />
                Saving…
              </>
            ) : (
              <>
                <UserCog className="h-4 w-4" />
                Save assignments
              </>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
