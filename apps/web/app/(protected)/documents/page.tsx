import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { DocumentList } from "@/components/documents/document-list";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata(
  "Documents",
  "Upload, organize, version, and retrieve legal documents.",
);

/**
 * Document Management — the platform's case file.
 *
 * A thin Server Component: it renders the shared page chrome and delegates all
 * interactivity to `DocumentList`. Authorization is not asserted here — the
 * `RouteGuard` in the protected layout applies the `documents:view` rule declared
 * for `/documents` in `config/navigation.ts`, so every page under `(protected)` is
 * guarded by construction rather than by each page remembering to. Which
 * *documents* the caller sees is decided by the API, per case assignment.
 */
export default function DocumentsPage() {
  return (
    <PageContainer>
      <PageHeader
        title="Documents"
        description="Upload, organize, version, and retrieve the files attached to your cases."
      />
      <DocumentList />
    </PageContainer>
  );
}
