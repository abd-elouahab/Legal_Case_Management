import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/layout/page-header";
import { SemanticSearch } from "@/components/search/semantic-search";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata(
  "Search",
  "Find passages across your indexed legal documents using natural language.",
);

/**
 * Semantic Search — natural-language retrieval across the case file.
 *
 * A thin Server Component: it renders the shared page chrome and delegates all
 * interactivity to `SemanticSearch`. Authorization is not asserted here — the
 * `RouteGuard` in the protected layout applies the `search:query` rule declared
 * for `/search` in `config/navigation.ts`, so every page under `(protected)` is
 * guarded by construction rather than by each page remembering to. Which
 * *passages* the caller sees is decided by the API, per case assignment, inside
 * the vector query itself.
 *
 * **Retrieval, not answers.** This page returns passages that already exist in
 * the documents, verbatim, with the file and page they came from. Asking a
 * question and receiving a written answer is the AI Assistant's, which is a later
 * feature grounded in exactly this retrieval.
 */
export default function SearchPage() {
  return (
    <PageContainer>
      <PageHeader
        titleKey="search.title"
        descriptionKey="search.description"
      />
      <SemanticSearch />
    </PageContainer>
  );
}
