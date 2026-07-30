"use client";

import * as React from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { FileUp } from "lucide-react";
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
import { Spinner } from "@/components/shared/spinner";
import { UploadProgress } from "@/components/documents/upload-progress";
import {
  documentErrorMessage,
  documentFieldErrors,
  useReplaceDocument,
} from "@/hooks/use-documents";
import {
  FILE_ACCEPT_ATTRIBUTE,
  replaceDocumentFormSchema,
  type ReplaceDocumentFormValues,
} from "@/lib/validation/document";
import { MAX_DOCUMENT_SIZE_MB, SUPPORTED_DOCUMENT_EXTENSIONS } from "@/types/document";
import type { LegalDocument } from "@/types/document";

/**
 * Replace dialog: upload a new version of an existing document.
 *
 * The copy states plainly what replacement means here, because "replace" usually
 * implies the old file is gone: it is **not**. The previous version keeps its own
 * stored file and stays downloadable from the version history, the document's
 * identifier does not change, and its category and description carry over.
 */

const EMPTY_FORM: ReplaceDocumentFormValues = { file: null };

export function ReplaceDocumentDialog({
  document,
  open,
  onOpenChange,
}: {
  document: LegalDocument | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const replace = useReplaceDocument();
  const [formError, setFormError] = React.useState<string | null>(null);
  const [progress, setProgress] = React.useState<number | null>(null);

  const form = useForm<ReplaceDocumentFormValues>({
    resolver: zodResolver(replaceDocumentFormSchema),
    defaultValues: EMPTY_FORM,
    mode: "onBlur",
  });

  const {
    control,
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = form;

  const files = useWatch({ control, name: "file" });
  const chosenFile = files?.[0] ?? null;

  // Drop a stale error and progress as the dialog reopens, during render rather
  // than in an effect, so the previous attempt's banner is never painted again.
  const [wasOpen, setWasOpen] = React.useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) {
      setFormError(null);
      setProgress(null);
    }
  }

  React.useEffect(() => {
    if (open) reset(EMPTY_FORM);
  }, [open, reset]);

  const onSubmit = handleSubmit(async (values) => {
    const file = values.file?.[0];
    if (!document || !file) return;

    setFormError(null);
    setProgress(0);

    try {
      const updated = await replace.mutateAsync({
        id: document.id,
        payload: { file, onProgress: setProgress },
      });

      toast.success(
        `${updated.originalFilename} was saved as version ${updated.version}. ` +
          `Version ${updated.version - 1} is still available.`,
      );
      onOpenChange(false);
    } catch (error) {
      setProgress(null);
      const fields = documentFieldErrors(error);
      for (const [field, message] of Object.entries(fields)) {
        if (field === "file") setError("file", { message });
      }
      if (Object.keys(fields).length === 0) setFormError(documentErrorMessage(error));
    }
  });

  const isPending = replace.isPending;

  return (
    <Dialog open={open} onOpenChange={isPending ? undefined : onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Replace document</DialogTitle>
          <DialogDescription>
            {document
              ? `Upload a new version of ${document.originalFilename}. Version ${document.version} is kept and stays downloadable from the version history — nothing is overwritten.`
              : "Upload a new version. Previous versions are kept and stay downloadable."}
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
              <Label htmlFor="replace-document-file">New file</Label>
              <Input
                id="replace-document-file"
                type="file"
                accept={FILE_ACCEPT_ATTRIBUTE}
                disabled={isPending}
                aria-invalid={errors.file ? true : undefined}
                aria-describedby={errors.file ? "replace-document-file-error" : undefined}
                {...register("file")}
              />
              <p className="text-xs text-muted-foreground">
                Supported types: {SUPPORTED_DOCUMENT_EXTENSIONS.join(", ")} — up to{" "}
                {MAX_DOCUMENT_SIZE_MB} MB. The replacement may be a different type from the
                file it replaces.
              </p>
              {chosenFile ? (
                <p className="text-xs text-muted-foreground">
                  {chosenFile.name} — {Math.max(1, Math.round(chosenFile.size / 1024))} KB
                </p>
              ) : null}
              {errors.file ? (
                <p
                  id="replace-document-file-error"
                  role="alert"
                  className="text-sm text-destructive"
                >
                  {errors.file.message}
                </p>
              ) : null}
            </div>

            {isPending ? <UploadProgress percent={progress} label="Replacement progress" /> : null}
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
                  <FileUp className="h-4 w-4" />
                  Upload new version
                </>
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
