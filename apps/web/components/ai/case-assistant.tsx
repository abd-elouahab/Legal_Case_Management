"use client";

import { useTranslations } from "next-intl";
import { Bot } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AssistantWorkspace } from "@/components/ai/assistant-workspace";
import { usePermissions } from "@/hooks/use-permissions";
import { PERMISSION } from "@/types/authorization";

/**
 * The AI assistant, inside a case workspace.
 *
 * `ui-context.md`: *"The AI Assistant can summarize documents, answer questions,
 * and generate reports directly within the case workspace"*, and *"Integrate AI
 * naturally into existing workflows rather than making it a separate
 * experience."* This is the same workspace the `/ai` page renders, pinned to one
 * case — the same relationship `CaseSearch` has to `/search` and the embedded
 * document list has to `/documents`.
 *
 * **Pinning is a default filter, never a grant.** Every answer is still built
 * only from documents the caller may already read: the case travels to the API as
 * a retrieval filter, and the search service refuses one the caller is not party
 * to. A user who cannot open this case never gets this far, because the case page
 * itself is refused first.
 *
 * The conversation list is scoped to this case too, so a matter's research stays
 * with the matter rather than mixing into one undifferentiated thread list.
 *
 * Rendered only for a caller holding `ai:chat` — the case page shows nothing at
 * all otherwise, rather than a card explaining what they are missing.
 */
export function CaseAssistant({ caseId }: { caseId: string }) {
  const { can, isLoading } = usePermissions();
  const t = useTranslations("assistant.caseSection");

  if (isLoading || !can(PERMISSION.aiChat)) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Bot className="h-5 w-5 text-muted-foreground" aria-hidden="true" />
          {t("title")}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex min-h-0 flex-col">
        <p className="mb-4 text-sm text-muted-foreground">{t("description")}</p>
        <AssistantWorkspace caseId={caseId} />
      </CardContent>
    </Card>
  );
}
