"use client";

import * as React from "react";
import { AlertCircle } from "lucide-react";
import type { UseFormRegisterReturn } from "react-hook-form";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useCaseAssignees } from "@/hooks/use-case-assignees";
import {
  CASE_PRIORITIES,
  CASE_PRIORITY_LABELS,
  CASE_STATUS_LABELS,
  type CasePriority,
  type CaseStatus,
} from "@/types/case";

/**
 * Shared form fields for creating and editing a case.
 *
 * One component rather than two near-identical forms: the two dialogs differ
 * only in whether a case number can be supplied, and duplicating the fields
 * would mean every future addition has to be made twice — and would eventually
 * be made once.
 *
 * It takes **already-registered** inputs rather than a form object, so it stays
 * non-generic while each dialog keeps its own precisely-typed `useForm`. The
 * alternative — a component generic over the form's value type — forces
 * `Path<T>` casts at every field and gives up the type safety it was reaching
 * for. This is the same shape `components/users/user-form.tsx` uses.
 *
 * Built entirely from Design System components. Validation is React Hook Form +
 * Zod (`lib/validation/case.ts`); this component only renders the result.
 */

/** Text inputs both forms have. */
export type SharedCaseField =
  | "title"
  | "description"
  | "category"
  | "courtName"
  | "filingDate"
  | "nextHearingDate";

/** Every field that can carry a validation message. */
export type CaseFieldName =
  | SharedCaseField
  | "caseNumber"
  | "status"
  | "priority"
  | "assignedLawyerId"
  | "assignedCourtRepresentativeId";

/**
 * Sentinel for "nobody assigned".
 *
 * A Radix `SelectItem` cannot have an empty value — it reserves that for
 * "nothing selected" — so unassignment needs a value of its own. Translated back
 * to `""` at the boundary, so nothing outside this file knows about it.
 */
const UNASSIGNED = "__unassigned__";

export interface CaseFormFieldsetProps {
  /** Registered text inputs, from the owning form's `register`. */
  fields: Record<SharedCaseField, UseFormRegisterReturn>;
  /** Validation messages, keyed by field. Absent means valid. */
  errors: Partial<Record<CaseFieldName, string>>;

  status: CaseStatus;
  onStatusChange: (status: CaseStatus) => void;
  /**
   * Statuses the Status select may offer, beyond the current one.
   *
   * The create form passes the full lifecycle; the edit form passes the case's
   * `allowedTransitions`, so the UI cannot propose a move the API would refuse.
   */
  statusOptions: readonly CaseStatus[];

  priority: CasePriority;
  onPriorityChange: (priority: CasePriority) => void;

  assignedLawyerId: string;
  onAssignedLawyerChange: (id: string) => void;
  assignedCourtRepresentativeId: string;
  onAssignedCourtRepresentativeChange: (id: string) => void;
  /** Hide the assignment section entirely when the caller cannot assign. */
  canAssign?: boolean;

  /** Supplied by the create form only; omitted, the case-number field is hidden. */
  caseNumber?: UseFormRegisterReturn;

  disabled?: boolean;
  /** A server-side failure that is not tied to one field. */
  formError?: string | null;
  /** Prefix for input ids, so two forms can coexist on one page. */
  idPrefix: string;
}

/** One labelled field with its inline validation message. */
function Field({
  id,
  label,
  error,
  hint,
  children,
}: {
  id: string;
  label: string;
  error?: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {error ? (
        <p id={`${id}-error`} className="text-sm text-destructive">
          {error}
        </p>
      ) : hint ? (
        <p className="text-xs text-muted-foreground">{hint}</p>
      ) : null}
    </div>
  );
}

/** A picker over the users eligible for one assignment position. */
function AssigneeSelect({
  id,
  label,
  role,
  value,
  onChange,
  disabled,
  error,
}: {
  id: string;
  label: string;
  role: "lawyer" | "court";
  value: string;
  onChange: (id: string) => void;
  disabled: boolean;
  error?: string;
}) {
  const { users, isLoading } = useCaseAssignees(role);

  return (
    <Field
      id={id}
      label={label}
      error={error}
      hint={
        !isLoading && users.length === 0
          ? "No active accounts hold this role yet."
          : undefined
      }
    >
      <Select
        value={value || UNASSIGNED}
        disabled={disabled || isLoading}
        onValueChange={(next) => onChange(next === UNASSIGNED ? "" : next)}
      >
        <SelectTrigger id={id} aria-invalid={Boolean(error)}>
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
    </Field>
  );
}

export function CaseFormFieldset({
  fields,
  errors,
  status,
  onStatusChange,
  statusOptions,
  priority,
  onPriorityChange,
  assignedLawyerId,
  onAssignedLawyerChange,
  assignedCourtRepresentativeId,
  onAssignedCourtRepresentativeChange,
  canAssign = true,
  caseNumber,
  disabled = false,
  formError,
  idPrefix,
}: CaseFormFieldsetProps) {
  const id = (field: string) => `${idPrefix}-${field}`;
  const describedBy = (field: CaseFieldName, elementId: string) =>
    errors[field] ? `${elementId}-error` : undefined;

  // The current status is always offered — it is what the field already shows —
  // plus every legal move from it. Deduplicated in case the two overlap.
  const statuses = Array.from(new Set<CaseStatus>([status, ...statusOptions]));

  return (
    <div className="flex flex-col gap-5">
      {formError ? (
        <Alert variant="destructive" role="alert">
          <AlertCircle className="h-4 w-4" />
          <AlertDescription>{formError}</AlertDescription>
        </Alert>
      ) : null}

      {caseNumber ? (
        <Field
          id={id("case-number")}
          label="Case number"
          error={errors.caseNumber}
          hint="Leave empty to generate the next number automatically."
        >
          <Input
            id={id("case-number")}
            placeholder="CASE-2026-0001"
            disabled={disabled}
            aria-invalid={Boolean(errors.caseNumber)}
            aria-describedby={describedBy("caseNumber", id("case-number"))}
            {...caseNumber}
          />
        </Field>
      ) : null}

      <Field id={id("title")} label="Title" error={errors.title}>
        <Input
          id={id("title")}
          placeholder="Benali v. Société Atlas"
          disabled={disabled}
          aria-invalid={Boolean(errors.title)}
          aria-describedby={describedBy("title", id("title"))}
          {...fields.title}
        />
      </Field>

      <Field
        id={id("description")}
        label="Description"
        error={errors.description}
        hint="Optional."
      >
        <Textarea
          id={id("description")}
          rows={4}
          placeholder="Summary of the matter."
          disabled={disabled}
          aria-invalid={Boolean(errors.description)}
          aria-describedby={describedBy("description", id("description"))}
          {...fields.description}
        />
      </Field>

      <div className="grid gap-5 sm:grid-cols-2">
        <Field
          id={id("category")}
          label="Category"
          error={errors.category}
          hint="Optional — e.g. Commercial, Labour, Family."
        >
          <Input
            id={id("category")}
            placeholder="Commercial"
            disabled={disabled}
            aria-invalid={Boolean(errors.category)}
            aria-describedby={describedBy("category", id("category"))}
            {...fields.category}
          />
        </Field>

        <Field id={id("priority")} label="Priority" error={errors.priority}>
          <Select
            value={priority}
            disabled={disabled}
            onValueChange={(value) => onPriorityChange(value as CasePriority)}
          >
            <SelectTrigger id={id("priority")} aria-invalid={Boolean(errors.priority)}>
              <SelectValue placeholder="Select a priority" />
            </SelectTrigger>
            <SelectContent>
              {CASE_PRIORITIES.map((option) => (
                <SelectItem key={option} value={option}>
                  {CASE_PRIORITY_LABELS[option]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
      </div>

      <Field
        id={id("status")}
        label="Status"
        error={errors.status}
        hint="Only the moves this case can legally make are offered."
      >
        <Select
          value={status}
          disabled={disabled}
          onValueChange={(value) => onStatusChange(value as CaseStatus)}
        >
          <SelectTrigger id={id("status")} aria-invalid={Boolean(errors.status)}>
            <SelectValue placeholder="Select a status" />
          </SelectTrigger>
          <SelectContent>
            {statuses.map((option) => (
              <SelectItem key={option} value={option}>
                {CASE_STATUS_LABELS[option]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Field>

      <Field id={id("court-name")} label="Court" error={errors.courtName} hint="Optional.">
        <Input
          id={id("court-name")}
          placeholder="Tribunal de Commerce de Casablanca"
          disabled={disabled}
          aria-invalid={Boolean(errors.courtName)}
          aria-describedby={describedBy("courtName", id("court-name"))}
          {...fields.courtName}
        />
      </Field>

      <div className="grid gap-5 sm:grid-cols-2">
        <Field id={id("filing-date")} label="Filing date" error={errors.filingDate}>
          <Input
            id={id("filing-date")}
            type="date"
            disabled={disabled}
            aria-invalid={Boolean(errors.filingDate)}
            aria-describedby={describedBy("filingDate", id("filing-date"))}
            {...fields.filingDate}
          />
        </Field>

        <Field
          id={id("next-hearing-date")}
          label="Next hearing"
          error={errors.nextHearingDate}
        >
          <Input
            id={id("next-hearing-date")}
            type="date"
            disabled={disabled}
            aria-invalid={Boolean(errors.nextHearingDate)}
            aria-describedby={describedBy("nextHearingDate", id("next-hearing-date"))}
            {...fields.nextHearingDate}
          />
        </Field>
      </div>

      {canAssign ? (
        <div className="grid gap-5 sm:grid-cols-2">
          <AssigneeSelect
            id={id("assigned-lawyer")}
            label="Assigned lawyer"
            role="lawyer"
            value={assignedLawyerId}
            onChange={onAssignedLawyerChange}
            disabled={disabled}
            error={errors.assignedLawyerId}
          />

          <AssigneeSelect
            id={id("assigned-representative")}
            label="Assigned court representative"
            role="court"
            value={assignedCourtRepresentativeId}
            onChange={onAssignedCourtRepresentativeChange}
            disabled={disabled}
            error={errors.assignedCourtRepresentativeId}
          />
        </div>
      ) : null}
    </div>
  );
}
