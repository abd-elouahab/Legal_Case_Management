"use client";

import * as React from "react";
import Link from "next/link";
import { zodResolver } from "@hookform/resolvers/zod";
import { useTranslations } from "next-intl";
import { Check, Download, Pencil } from "lucide-react";
import { useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";

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
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Textarea } from "@/components/ui/textarea";
import { ErrorState } from "@/components/shared/error-state";
import { LoadingState } from "@/components/shared/loading-state";
import { Spinner } from "@/components/shared/spinner";
import { DocumentCategoryBadge } from "@/components/documents/document-badges";
import { DocumentVersionHistory } from "@/components/documents/document-version-history";
import { DocumentIndexPanel } from "@/components/indexing/document-index-panel";
import { DocumentOcrPanel } from "@/components/ocr/document-ocr-panel";
import {
  useDocument,
  useDocumentErrorMessage,
  useDownloadDocument,
  useUpdateDocument,
} from "@/hooks/use-documents";
import { useDateFormat } from "@/hooks/use-date-format";
import { useFieldError } from "@/hooks/use-field-error";
import { caseRoute } from "@/lib/routes";
import {
  MAX_DESCRIPTION_LENGTH,
  editDocumentFormSchema,
  type EditDocumentFormValues,
} from "@/lib/validation/document";
import { PERMISSION } from "@/types/authorization";
import {
  DOCUMENT_CATEGORIES,
  type DocumentCategory,
  type DocumentVersion,
  type LegalDocument,
} from "@/types/document";

/**
 * Document details: metadata, the case it belongs to, and the version history.
 *
 * Also where the two metadata fields are edited, because they are the only thing
 * about a document a PATCH can change — a separate "edit document" dialog would
 * be a second surface showing the same two inputs. Editing is gated on
 * `documents:update` and is simply not offered to a caller who lacks it.
 *
 * The record is re-fetched by id rather than taken from the row that opened the
 * dialog, so the version history is current even if the document was replaced
 * from another screen since the list was loaded.
 */

function DetailRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1 py-2 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="text-sm text-foreground sm:max-w-[60%] sm:text-end">{value}</dd>
    </div>
  );
}

function DocumentMetadataForm({ document }: { document: LegalDocument }) {
  const update = useUpdateDocument();
  const t = useTranslations("documents.detailsDialog");
  const tCategories = useTranslations("documents.categories");
  const tUpload = useTranslations("documents.uploadDialog");
  const tActions = useTranslations("common.actions");
  const errorMessage = useDocumentErrorMessage();
  const fieldError = useFieldError();
  const [formError, setFormError] = React.useState<string | null>(null);

  const defaults = React.useMemo<EditDocumentFormValues>(
    () => ({ category: document.category, description: document.description ?? "" }),
    [document.category, document.description],
  );

  const form = useForm<EditDocumentFormValues>({
    resolver: zodResolver(editDocumentFormSchema),
    defaultValues: defaults,
    mode: "onBlur",
  });

  const {
    control,
    register,
    handleSubmit,
    reset,
    setValue,
    formState: { errors, isDirty },
  } = form;

  const category = useWatch({ control, name: "category" });

  // Re-seed the form when a different document is shown, or when a replacement
  // changes the record underneath it. `reset` mutates React Hook Form's store,
  // which is what effects are for.
  React.useEffect(() => {
    reset(defaults);
  }, [reset, defaults]);

  const onSubmit = handleSubmit(async (values) => {
    setFormError(null);

    try {
      await update.mutateAsync({
        id: document.id,
        payload: {
          category: values.category,
          // Sent as an explicit null so a cleared field is cleared on the server
          // rather than left at its previous value.
          description: values.description || null,
        },
      });
      toast.success(t("saved"));
    } catch (error) {
      setFormError(errorMessage(error));
    }
  });

  const isPending = update.isPending;

  return (
    <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
      {formError ? (
        <p role="alert" className="text-sm text-destructive">
          {formError}
        </p>
      ) : null}

      <div className="flex flex-col gap-2">
        <Label htmlFor="document-details-category">{tUpload("category")}</Label>
        <Select
          value={category}
          onValueChange={(value) =>
            setValue("category", value as DocumentCategory, {
              shouldValidate: true,
              shouldDirty: true,
            })
          }
          disabled={isPending}
        >
          <SelectTrigger id="document-details-category">
            <SelectValue placeholder={tUpload("selectCategory")} />
          </SelectTrigger>
          <SelectContent>
            {DOCUMENT_CATEGORIES.map((option) => (
              <SelectItem key={option} value={option}>
                {tCategories(option)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {errors.category ? (
          <p role="alert" className="text-sm text-destructive">
            {fieldError(errors.category.message)}
          </p>
        ) : null}
      </div>

      <div className="flex flex-col gap-2">
        <Label htmlFor="document-details-description">{t("fields.description")}</Label>
        <Textarea
          id="document-details-description"
          rows={3}
          maxLength={MAX_DESCRIPTION_LENGTH}
          disabled={isPending}
          {...register("description")}
        />
        {errors.description ? (
          <p role="alert" className="text-sm text-destructive">
            {fieldError(errors.description.message)}
          </p>
        ) : null}
      </div>

      <div className="flex justify-end">
        <Button type="submit" size="sm" disabled={isPending || !isDirty}>
          {isPending ? (
            <>
              <Spinner className="h-4 w-4 text-current" />
              {tActions("saving")}
            </>
          ) : (
            <>
              <Check className="h-4 w-4" />
              {t("saveDetails")}
            </>
          )}
        </Button>
      </div>
    </form>
  );
}

function DocumentDetailsContent({
  document,
  onOpenChange,
}: {
  document: LegalDocument;
  onOpenChange: (open: boolean) => void;
}) {
  const { formatDateTime } = useDateFormat();
  const download = useDownloadDocument();
  const t = useTranslations("documents.detailsDialog");
  const tActions = useTranslations("common.actions");
  const errorMessage = useDocumentErrorMessage();
  const [downloadingVersion, setDownloadingVersion] = React.useState<number | null>(null);

  async function fetchVersion(version: DocumentVersion | null) {
    const target = version ?? {
      version: document.version,
      originalFilename: document.originalFilename,
    };
    setDownloadingVersion(target.version);

    try {
      await download.mutateAsync({
        id: document.id,
        filename: target.originalFilename,
        version: target.version,
      });
    } catch (error) {
      toast.error(errorMessage(error));
    } finally {
      setDownloadingVersion(null);
    }
  }

  return (
    <>
      <dl className="divide-y divide-border">
        <DetailRow label={t("fields.fileName")} value={document.originalFilename} />
        <DetailRow
          label={t("fields.category")}
          value={<DocumentCategoryBadge category={document.category} />}
        />
        <DetailRow
          label={t("fields.case")}
          value={
            document.case ? (
              <Link href={caseRoute(document.case.id)} className="hover:underline">
                {document.case.caseNumber} — {document.case.title}
              </Link>
            ) : (
              <span className="text-muted-foreground">{t("notAvailable")}</span>
            )
          }
        />
        <DetailRow
          label={t("fields.type")}
          value={document.fileExtension.toUpperCase()}
        />
        <DetailRow label={t("fields.size")} value={document.fileSizeLabel} />
        <DetailRow
          label={t("fields.currentVersion")}
          value={t("versionShort", { version: document.version })}
        />
        <DetailRow
          label={t("fields.uploadedBy")}
          value={
            document.uploader?.fullName ?? (
              <span className="text-muted-foreground">{t("notRecorded")}</span>
            )
          }
        />
        <DetailRow
          label={t("fields.addedToCase")}
          value={formatDateTime(document.createdAt)}
        />
        <DetailRow
          label={t("fields.currentVersionUploaded")}
          value={formatDateTime(document.uploadedAt)}
        />
        <DetailRow
          label={t("fields.description")}
          value={
            document.description ? (
              // `whitespace-pre-line` keeps the author's paragraphs, which the
              // API deliberately preserves rather than collapsing.
              <span className="whitespace-pre-line">{document.description}</span>
            ) : (
              <span className="text-muted-foreground">{t("notProvided")}</span>
            )
          }
        />
      </dl>

      <Protected permission={PERMISSION.documentsUpdate}>
        <Separator />
        <DocumentMetadataForm document={document} />
      </Protected>

      <Protected permission={PERMISSION.ocrView}>
        <Separator />
        {/* Extraction is a property of the document, so it belongs beside the
            document rather than on a screen of its own. Gated on `ocr:view`,
            which the API requires independently. */}
        <DocumentOcrPanel document={document} />
      </Protected>

      <Protected permission={PERMISSION.indexingView}>
        <Separator />
        {/* Directly after extraction, because it is the next stage of the same
            pipeline and reads the text the panel above produced. Gated on
            `indexing:view`, which the API requires independently — a court
            representative holds it, and holds neither retry nor rebuild. */}
        <DocumentIndexPanel document={document} />
      </Protected>

      <Separator />

      <DocumentVersionHistory
        document={document}
        onDownloadVersion={(version) => void fetchVersion(version)}
        downloadingVersion={downloadingVersion}
      />

      <DialogFooter>
        <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
          {tActions("close")}
        </Button>
        <Button
          type="button"
          onClick={() => void fetchVersion(null)}
          disabled={downloadingVersion !== null}
        >
          <Download className="h-4 w-4" />
          {t("downloadCurrent")}
        </Button>
      </DialogFooter>
    </>
  );
}

export function DocumentDetailsDialog({
  documentId,
  open,
  onOpenChange,
}: {
  documentId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { data, isLoading, isError, error, refetch } = useDocument(documentId ?? "", {
    enabled: open && Boolean(documentId),
  });
  const t = useTranslations("documents.detailsDialog");
  const errorMessage = useDocumentErrorMessage();

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Pencil className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
            {t("title")}
          </DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </DialogHeader>

        {isLoading ? (
          <LoadingState label={t("loading")} />
        ) : isError || !data ? (
          <ErrorState
            title={t("loadFailed")}
            description={errorMessage(error)}
            onRetry={() => void refetch()}
          />
        ) : (
          <DocumentDetailsContent document={data} onOpenChange={onOpenChange} />
        )}
      </DialogContent>
    </Dialog>
  );
}
