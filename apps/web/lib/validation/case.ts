/**
 * Zod schemas for Case Management.
 *
 * Two jobs, both required by the code standards ("validate all external input
 * using Zod"):
 *
 * 1. **Form validation** — drives the create and edit forms through React Hook
 *    Form, mirroring the server's rules so an administrator gets immediate
 *    feedback. The server always re-validates; this is UX, not security.
 * 2. **Response validation** — API responses are external input too, so they are
 *    parsed before entering application state.
 *
 * The rules deliberately mirror `apps/api/schemas/case.py` and
 * `apps/api/core/cases.py`. Where they must agree, the API is the authority: a
 * value this schema accepts but the server rejects surfaces as a field error on
 * submit rather than as a silent inconsistency.
 */

import { z } from "zod";

import { vm } from "@/lib/validation/messages";

import { CASE_PRIORITIES, CASE_STATUSES } from "@/types/case";
import { CASE_SORT_FIELDS, SORT_ORDERS } from "@/types/case-management";
import { USER_ROLES } from "@/types/user";

/** Matches the column widths and limits in `apps/api/core/cases.py`. */
export const MAX_CASE_NUMBER_LENGTH = 50;
export const MAX_TITLE_LENGTH = 255;
export const MAX_CATEGORY_LENGTH = 100;
export const MAX_COURT_NAME_LENGTH = 255;
export const MAX_DESCRIPTION_LENGTH = 10_000;

/** Characters a case number may contain; mirrors the API's pattern. */
const CASE_NUMBER_ALLOWED = /^[A-Za-z0-9][A-Za-z0-9/._-]*$/;

/** A calendar date as `<input type="date">` produces it, and as the API expects it. */
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

// --------------------------------------------------------------------------- //
// Field schemas
// --------------------------------------------------------------------------- //

/** Collapse internal whitespace and trim, matching `normalize_text` on the API. */
const collapse = (value: string) => value.replace(/\s+/g, " ").trim();

const titleField = z
  .string()
  .transform(collapse)
  .refine((value) => value.length > 0, vm("validation.case.titleRequired"))
  .refine(
    (value) => value.length <= MAX_TITLE_LENGTH,
    vm("validation.maxLengthTitle", { max: MAX_TITLE_LENGTH }),
  );

/**
 * An optional case number. Blank means "generate one", which is what the API
 * does when the field is absent — so an empty field is valid, not an error.
 */
const optionalCaseNumberField = z
  .string()
  .transform((value) => value.replace(/\s+/g, "").trim().toUpperCase())
  .refine(
    (value) => value === "" || value.length <= MAX_CASE_NUMBER_LENGTH,
    vm("validation.case.caseNumberTooLong", { max: MAX_CASE_NUMBER_LENGTH }),
  )
  .refine(
    (value) => value === "" || CASE_NUMBER_ALLOWED.test(value),
    vm("validation.case.caseNumberCharacters"),
  );

/**
 * A one-line optional field with a length ceiling.
 *
 * **The field's own name is no longer in the message.** It used to read
 * "Category must be at most 100 characters", which needed the label as a
 * parameter — and a label passed into a schema is an English word a translator
 * cannot reach. The message renders directly beneath the input it belongs to, so
 * naming the field there was always redundant; dropping it is what lets the
 * sentence be one catalogue entry shared by every length-limited field.
 */
const optionalLineField = (limit: number) =>
  z
    .string()
    .transform(collapse)
    .refine((value) => value.length <= limit, vm("validation.maxLength", { max: limit }));

/** A description is prose: trimmed, but its paragraphs are preserved. */
const optionalDescriptionField = z
  .string()
  .transform((value) => value.trim())
  .refine(
    (value) => value.length <= MAX_DESCRIPTION_LENGTH,
    vm("validation.maxLengthDescription", { max: MAX_DESCRIPTION_LENGTH }),
  );

/** An optional calendar date. A blank field means "not recorded". */
const optionalDateField = z
  .string()
  .transform((value) => value.trim())
  .refine((value) => value === "" || ISO_DATE.test(value), vm("validation.dateInvalid"));

/**
 * An optional assignment. The Selects use a sentinel for "unassigned" and
 * translate it to `""` here, which the submit handler turns into an explicit
 * `null` — the API's way of removing an assignment.
 */
const optionalAssigneeField = z.string();

// --------------------------------------------------------------------------- //
// Form schemas
// --------------------------------------------------------------------------- //

/**
 * Fields shared by the create and edit forms.
 *
 * No `.default()` anywhere: a defaulted field makes a schema's input type differ
 * from its output type, which then has to be threaded through React Hook Form as
 * two separate generics. Every field is supplied by `defaultValues`, so the two
 * types stay identical and the forms need one type each.
 */
const sharedCaseFields = {
  title: titleField,
  description: optionalDescriptionField,
  category: optionalLineField(MAX_CATEGORY_LENGTH),
  status: z.enum(CASE_STATUSES, { required_error: vm("validation.case.statusRequired") }),
  priority: z.enum(CASE_PRIORITIES, { required_error: vm("validation.case.priorityRequired") }),
  courtName: optionalLineField(MAX_COURT_NAME_LENGTH),
  filingDate: optionalDateField,
  nextHearingDate: optionalDateField,
  assignedLawyerId: optionalAssigneeField,
  assignedCourtRepresentativeId: optionalAssigneeField,
};

/**
 * A hearing cannot precede the filing that produced it — the same rule the API
 * enforces. Attached to the field the user would fix, so the message appears
 * next to the hearing input rather than as a form-wide banner.
 *
 * Written as a refinement *function* applied to each schema rather than as a
 * generic `withCoherentDates(schema)` wrapper: a helper generic over
 * `ZodTypeAny` erases the object's output type, which turns `z.infer` into `any`
 * and silently costs every form its field typing. Both schemas share the
 * function, so the rule still lives in one place. Dates are ISO-8601, so a
 * string comparison is a chronological one.
 */
function refineDateOrder(
  values: { filingDate: string; nextHearingDate: string },
  ctx: z.RefinementCtx,
): void {
  if (values.filingDate && values.nextHearingDate && values.nextHearingDate < values.filingDate) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["nextHearingDate"],
      message: vm("validation.case.hearingBeforeFiling"),
    });
  }
}

/**
 * Create-case form.
 *
 * Carries the optional case number; the edit form does not, because the number
 * identifies the case and is immutable once filed.
 */
export const createCaseFormSchema = z
  .object({ ...sharedCaseFields, caseNumber: optionalCaseNumberField })
  .superRefine(refineDateOrder);

export type CreateCaseFormValues = z.infer<typeof createCaseFormSchema>;

/** Edit-case form — the same fields minus the case number. */
export const editCaseFormSchema = z.object(sharedCaseFields).superRefine(refineDateOrder);

export type EditCaseFormValues = z.infer<typeof editCaseFormSchema>;

// --------------------------------------------------------------------------- //
// Response schemas
// --------------------------------------------------------------------------- //

const caseUserSummarySchema = z.object({
  id: z.string(),
  full_name: z.string(),
  email: z.string(),
  role: z.enum(USER_ROLES),
});

/**
 * A case record from `GET /cases` and `GET /cases/{id}`.
 *
 * Unknown status values in `allowed_transitions` are dropped rather than failing
 * the whole response, for the same reason the user schema drops unknown
 * permissions: a backend that has added a status this build does not know about
 * must not be able to break the case list, and a status the client cannot render
 * is one it cannot offer anyway.
 */
export const legalCaseSchema = z.object({
  id: z.string(),
  case_number: z.string(),
  title: z.string(),
  description: z.string().nullable().default(null),
  category: z.string().nullable().default(null),
  status: z.enum(CASE_STATUSES),
  priority: z.enum(CASE_PRIORITIES),
  court_name: z.string().nullable().default(null),
  filing_date: z.string().nullable().default(null),
  next_hearing_date: z.string().nullable().default(null),
  assigned_lawyer_id: z.string().nullable().default(null),
  assigned_court_representative_id: z.string().nullable().default(null),
  assigned_lawyer: caseUserSummarySchema.nullable().default(null),
  assigned_court_representative: caseUserSummarySchema.nullable().default(null),
  created_by: z.string().nullable().default(null),
  updated_by: z.string().nullable().default(null),
  creator: caseUserSummarySchema.nullable().default(null),
  updater: caseUserSummarySchema.nullable().default(null),
  created_at: z.string(),
  updated_at: z.string(),
  is_archived: z.boolean(),
  allowed_transitions: z
    .array(z.string())
    .default([])
    .transform((values) =>
      values.filter((value): value is (typeof CASE_STATUSES)[number] =>
        (CASE_STATUSES as readonly string[]).includes(value),
      ),
    ),
});

export const casePageSchema = z.object({
  items: z.array(legalCaseSchema),
  total_records: z.number().int().nonnegative(),
  page: z.number().int().positive(),
  page_size: z.number().int().positive(),
  total_pages: z.number().int().nonnegative(),
});

// --------------------------------------------------------------------------- //
// Query validation
// --------------------------------------------------------------------------- //

/**
 * The list query, validated before it becomes a query string.
 *
 * The values come from UI state rather than from a user typing a URL, but they
 * are still the input to a request — and validating here means an out-of-range
 * page from a stale link is corrected rather than sent to the API to be rejected.
 */
export const caseListQuerySchema = z.object({
  page: z.coerce.number().int().min(1).catch(1),
  pageSize: z.coerce.number().int().min(1).max(100).catch(20),
  search: z.string().trim().max(200).catch(""),
  status: z.enum(CASE_STATUSES).nullable().catch(null),
  priority: z.enum(CASE_PRIORITIES).nullable().catch(null),
  assignedLawyerId: z.string().nullable().catch(null),
  assignedCourtRepresentativeId: z.string().nullable().catch(null),
  courtName: z.string().trim().max(MAX_COURT_NAME_LENGTH).catch(""),
  filingDateFrom: z.string().regex(ISO_DATE).or(z.literal("")).catch(""),
  filingDateTo: z.string().regex(ISO_DATE).or(z.literal("")).catch(""),
  hearingDateFrom: z.string().regex(ISO_DATE).or(z.literal("")).catch(""),
  hearingDateTo: z.string().regex(ISO_DATE).or(z.literal("")).catch(""),
  sortBy: z.enum(CASE_SORT_FIELDS).catch("created_at"),
  sortOrder: z.enum(SORT_ORDERS).catch("desc"),
});
