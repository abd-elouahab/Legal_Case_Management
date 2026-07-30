"use client";

import * as React from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { Upload } from "lucide-react";
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
import { Textarea } from "@/components/ui/textarea";
import { Spinner } from "@/components/shared/spinner";
import { UploadProgress } from "@/components/documents/upload-progress";
import { useDocumentCases } from "@/hooks/use-document-cases";
import {
  documentErrorMessage,
  documentFieldErrors,
  useUploadDocument,
} from "@/hooks/use-documents";
import {
  FILE_ACCEPT_ATTRIBUTE,
  MAX_DESCRIPTION_LENGTH,
  uploadDocumentFormSchema,
  type UploadDocumentFormValues,
} from "@/lib/validation/document";
import {
  DOCUMENT_CATEGORIES,
  DOCUMENT_CATEGORY_LABELS,
  MAX_DOCUMENT_SIZE_MB,
  SUPPORTED_DOCUMENT_EXTENSIONS,
  type DocumentCategory,
} from "@/types/document";

/**
 * Upload dialog: choose a file, a case, a category, and an optional description.
 *
 * Owns nothing but presentation and form state: the request, cache invalidation,
 * and error translation all live in `useUploadDocument` / `documentErrorMessage`,
 * per the standard that components carry no business logic.
 *
 * Progress is real, not a spinner — `useUploadDocument` reports transmitted bytes
 * through the XHR upload events, which matters when a scanned bundle is 25 MB on
 * a slow connection.
 *
 * Server-side validation failures are mapped back onto the offending field, so a
 * file the server refuses (wrong type, corrupted, too large) is reported against
 * the file input rather than as an opaque banner. The client checks what it can
 * see — name, size — but only the server can inspect the bytes.
 */

const EMPTY_FORM: UploadDocumentFormValues = {
  caseId: "",
  category: "other",
  description: "",
  file: null,
};

export function UploadDocumentDialog({
  open,
  onOpenChange,
  caseId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Pre-selected and locked when uploading from inside a case workspace. */
  caseId?: string;
}) {
  const upload = useUploadDocument();
  const cases = useDocumentCases({ enabled: open && !caseId });
  const [formError, setFormError] = React.useState<string | null>(null);
  const [progress, setProgress] = React.useState<number | null>(null);

  const defaults = React.useMemo<UploadDocumentFormValues>(
    () => ({ ...EMPTY_FORM, caseId: caseId ?? "" }),
    [caseId],
  );

  const form = useForm<UploadDocumentFormValues>({
    resolver: zodResolver(uploadDocumentFormSchema),
    defaultValues: defaults,
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
  const selectedCaseId = useWatch({ control, name: "caseId" });
  const category = useWatch({ control, name: "category" });
  const files = useWatch({ control, name: "file" });
  const chosenFile = files?.[0] ?? null;

  // Drop a stale server error and progress as the dialog reopens. Adjusted
  // during render rather than in an effect, so the previous attempt's banner is
  // never painted again (https://react.dev/learn/you-might-not-need-an-effect).
  const [wasOpen, setWasOpen] = React.useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) {
      setFormError(null);
      setProgress(null);
    }
  }

  // Start from a clean form each time the dialog opens; a half-filled attempt
  // from a cancelled upload is more confusing than helpful. This one *is* an
  // effect: `reset` mutates React Hook Form's store, which is exactly the
  // external-system synchronization effects are for.
  React.useEffect(() => {
    if (open) reset(defaults);
  }, [open, reset, defaults]);

  const onSubmit = handleSubmit(async (values) => {
    const file = values.file?.[0];
    if (!file) return;

    setFormError(null);
    setProgress(0);

    try {
      const created = await upload.mutateAsync({
        caseId: values.caseId,
        file,
        category: values.category,
        description: values.description || null,
        onProgress: setProgress,
      });

      toast.success(`${created.originalFilename} was uploaded.`);
      onOpenChange(false);
    } catch (error) {
      setProgress(null);
      const fields = documentFieldErrors(error);
      for (const [field, message] of Object.entries(fields)) {
        setError(field as keyof UploadDocumentFormValues, { message });
      }
      // Only surface the banner when nothing could be attached to a field —
      // otherwise the same complaint would appear twice.
      if (Object.keys(fields).length === 0) setFormError(documentErrorMessage(error));
    }
  });

  const isPending = upload.isPending;

  return (
    <Dialog open={open} onOpenChange={isPending ? undefined : onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Upload document</DialogTitle>
          <DialogDescription>
            Attach a file to a case. Supported types:{" "}
            {SUPPORTED_DOCUMENT_EXTENSIONS.join(", ")} — up to {MAX_DOCUMENT_SIZE_MB} MB.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={onSubmit} noValidate className="flex flex-col gap-6">
          <div className="flex flex-col gap-4">
            {formError ? (
              <p role="alert" className="text-sm text-destructive">
                {formError}
              </p>
            ) : null}

            <div className="flex flex-col gap-2">
              <Label htmlFor="upload-document-file">File</Label>
              <Input
                id="upload-document-file"
                type="file"
                accept={FILE_ACCEPT_ATTRIBUTE}
                disabled={isPending}
                aria-invalid={errors.file ? true : undefined}
                aria-describedby={errors.file ? "upload-document-file-error" : undefined}
                {...register("file")}
              />
              {chosenFile ? (
                <p className="text-xs text-muted-foreground">
                  {chosenFile.name} — {Math.max(1, Math.round(chosenFile.size / 1024))} KB
                </p>
              ) : null}
              {errors.file ? (
                <p id="upload-document-file-error" role="alert" className="text-sm text-destructive">
                  {errors.file.message}
                </p>
              ) : null}
            </div>

            {caseId ? null : (
              <div className="flex flex-col gap-2">
                <Label htmlFor="upload-document-case">Case</Label>
                <Select
                  value={selectedCaseId || undefined}
                  onValueChange={(value) => setValue("caseId", value, { shouldValidate: true })}
                  disabled={isPending}
                >
                  <SelectTrigger id="upload-document-case">
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
              <Label htmlFor="upload-document-category">Category</Label>
              <Select
                value={category}
                onValueChange={(value) =>
                  setValue("category", value as DocumentCategory, { shouldValidate: true })
                }
                disabled={isPending}
              >
                <SelectTrigger id="upload-document-category">
                  <SelectValue placeholder="Select a category" />
                </SelectTrigger>
                <SelectContent>
                  {DOCUMENT_CATEGORIES.map((option) => (
                    <SelectItem key={option} value={option}>
                      {DOCUMENT_CATEGORY_LABELS[option]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {errors.category ? (
                <p role="alert" className="text-sm text-destructive">
                  {errors.category.message}
                </p>
              ) : null}
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="upload-document-description">Description (optional)</Label>
              <Textarea
                id="upload-document-description"
                rows={3}
                maxLength={MAX_DESCRIPTION_LENGTH}
                disabled={isPending}
                placeholder="What this document is, and why it matters to the case."
                {...register("description")}
              />
              {errors.description ? (
                <p role="alert" className="text-sm text-destructive">
                  {errors.description.message}
                </p>
              ) : null}
            </div>

            {isPending ? <UploadProgress percent={progress} /> : null}
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
                  Uploading…
                </>
              ) : (
                <>
                  <Upload className="h-4 w-4" />
                  Upload
                </>
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
