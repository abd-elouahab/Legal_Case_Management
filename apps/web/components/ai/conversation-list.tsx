"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import {
  Archive,
  ArchiveRestore,
  MessageSquare,
  MoreHorizontal,
  Pencil,
  Plus,
  Search,
  Trash2,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/shared/empty-state";
import { RenameConversationDialog } from "@/components/ai/rename-conversation-dialog";
import { DeleteConversationDialog } from "@/components/ai/delete-conversation-dialog";
import { useConversations, useUpdateConversation } from "@/hooks/use-assistant";
import { cn } from "@/lib/utils";
import type { Conversation, ConversationStatus } from "@/types/assistant";

/**
 * The caller's own conversations.
 *
 * **Only theirs**, and that is the API's doing rather than this component's: the
 * list endpoint is scoped by owner in SQL, so there is no filter here that could
 * be forgotten and no total that counts somebody else's threads.
 *
 * Three decisions worth stating:
 *
 * * **archived threads are hidden until asked for.** Archiving is the user's way
 *   of saying "I am done with this"; a list that kept showing them would make the
 *   action do nothing visible. The toggle is one control, not a filter bar —
 *   there are two states.
 * * **search matches the title only**, because that is what the API searches. A
 *   box that appeared to search message contents and quietly did not would be
 *   worse than no box.
 * * **the row's actions name what they do to the data**: archive keeps it, delete
 *   withdraws it. Delete is a destructive confirmation for the same reason
 *   archiving a case is one.
 */
export function ConversationList({
  selectedId,
  onSelect,
  onCreate,
  caseId,
  className,
}: {
  selectedId: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
  /** Restricts the list to one case's conversations, for the case workspace. */
  caseId?: string;
  className?: string;
}) {
  const [status, setStatus] = React.useState<ConversationStatus>("active");
  const [search, setSearch] = React.useState("");
  const [renaming, setRenaming] = React.useState<Conversation | null>(null);
  const [deleting, setDeleting] = React.useState<Conversation | null>(null);

  const { data, isLoading, isError } = useConversations({
    status,
    search: search.trim() || null,
    ...(caseId ? { caseId } : {}),
  });
  const update = useUpdateConversation();
  const t = useTranslations("assistant.conversations");
  const tActions = useTranslations("common.actions");

  const conversations = data?.items ?? [];

  return (
    <div className={cn("flex flex-col gap-3", className)}>
      <div className="flex items-center gap-2">
        <Button type="button" size="sm" onClick={onCreate} className="flex-1">
          <Plus className="h-4 w-4" aria-hidden="true" />
          {t("new")}
        </Button>
      </div>

      <div className="relative">
        <Search
          className="pointer-events-none absolute start-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
          aria-hidden="true"
        />
        <Input
          type="search"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder={t("searchPlaceholder")}
          aria-label={t("searchLabel")}
          className="ps-9"
        />
      </div>

      <div className="flex items-center gap-1 text-xs">
        <Button
          type="button"
          variant={status === "active" ? "secondary" : "ghost"}
          size="sm"
          className="h-7 px-2"
          aria-pressed={status === "active"}
          onClick={() => setStatus("active")}
        >
          {t("active")}
        </Button>
        <Button
          type="button"
          variant={status === "archived" ? "secondary" : "ghost"}
          size="sm"
          className="h-7 px-2"
          aria-pressed={status === "archived"}
          onClick={() => setStatus("archived")}
        >
          {t("archived")}
        </Button>
      </div>

      {isLoading ? (
        <div className="flex flex-col gap-2" aria-busy="true">
          {Array.from({ length: 4 }, (_, index) => (
            <Skeleton key={index} className="h-14 rounded-lg" />
          ))}
        </div>
      ) : isError ? (
        <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
          {t("loadFailed")}
        </p>
      ) : conversations.length === 0 ? (
        <EmptyState
          icon={MessageSquare}
          className="min-h-40 p-4"
          title={
            search.trim()
              ? t("emptySearchTitle")
              : status === "archived"
                ? t("emptyArchivedTitle")
                : t("emptyTitle")
          }
          description={
            search.trim()
              ? t("emptySearchDescription")
              : status === "archived"
                ? t("emptyArchivedDescription")
                : t("emptyDescription")
          }
        />
      ) : (
        <ul className="flex flex-col gap-1" aria-label={t("label")}>
          {conversations.map((conversation) => (
            <li key={conversation.id}>
              <div
                className={cn(
                  "group flex items-start gap-1 rounded-lg border px-2 py-2 transition-colors",
                  conversation.id === selectedId
                    ? "border-primary/40 bg-primary/5"
                    : "border-transparent hover:bg-muted/60",
                )}
              >
                <button
                  type="button"
                  onClick={() => onSelect(conversation.id)}
                  aria-current={conversation.id === selectedId ? "true" : undefined}
                  className="min-w-0 flex-1 text-start"
                >
                  <span
                    dir="auto"
                    className="block truncate text-sm font-medium text-foreground"
                  >
                    {conversation.title}
                  </span>
                  <span
                    dir="auto"
                    className="block truncate text-xs text-muted-foreground"
                  >
                    {conversation.lastMessagePreview ?? t("noMessages")}
                  </span>
                </button>

                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7 shrink-0"
                      aria-label={t("actionsFor", { title: conversation.title })}
                    >
                      <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end" className="w-48">
                    <DropdownMenuItem onSelect={() => setRenaming(conversation)}>
                      <Pencil className="h-4 w-4" aria-hidden="true" />
                      {t("rename")}
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      onSelect={() =>
                        update.mutate({
                          id: conversation.id,
                          input: {
                            status:
                              conversation.status === "archived" ? "active" : "archived",
                          },
                        })
                      }
                    >
                      {conversation.status === "archived" ? (
                        <>
                          <ArchiveRestore className="h-4 w-4" aria-hidden="true" />
                          {t("restore")}
                        </>
                      ) : (
                        <>
                          <Archive className="h-4 w-4" aria-hidden="true" />
                          {t("archive")}
                        </>
                      )}
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      variant="destructive"
                      onSelect={() => setDeleting(conversation)}
                    >
                      <Trash2 className="h-4 w-4" aria-hidden="true" />
                      {tActions("delete")}
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
            </li>
          ))}
        </ul>
      )}

      <RenameConversationDialog
        conversation={renaming}
        open={renaming !== null}
        onOpenChange={(open) => {
          if (!open) setRenaming(null);
        }}
      />
      <DeleteConversationDialog
        conversation={deleting}
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
        onDeleted={(id) => {
          if (id === selectedId) onSelect("");
        }}
      />
    </div>
  );
}
