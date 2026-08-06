"use client";

import { Search } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { SemanticSearch } from "@/components/search/semantic-search";
import { usePermissions } from "@/hooks/use-permissions";
import { PERMISSION } from "@/types/authorization";

/**
 * Semantic search inside the case workspace, pinned to one case.
 *
 * The same component the `/search` page renders, with the case fixed: the case
 * filter disappears, every search is scoped to this matter, and *Clear filters*
 * does not widen the view to the whole platform — exactly the shape the embedded
 * document list already established for a case-scoped view.
 *
 * Rendered only for a caller holding `search:query`; the card is hidden entirely
 * otherwise, because a search box that answers 403 is worse than no search box.
 * That is presentation only — the API authorizes every request independently, and
 * scopes the results to the cases the caller is party to whatever this renders.
 */
export function CaseSearch({ caseId }: { caseId: string }) {
  const { can, isLoading } = usePermissions();

  if (isLoading || !can(PERMISSION.searchQuery)) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Search className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          Search this case
        </CardTitle>
        <CardDescription>
          Ask a question in your own words and find the passages that answer it, with
          the file and page they came from.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <SemanticSearch caseId={caseId} compact />
      </CardContent>
    </Card>
  );
}
