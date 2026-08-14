"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { Archive, ArchiveRestore, Eye, MoreHorizontal, Pencil, UserCog } from "lucide-react";

import { Protected } from "@/components/auth/protected";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { caseRoute } from "@/lib/routes";
import { PERMISSION } from "@/types/authorization";
import type { LegalCase } from "@/types/case";

/**
 * Per-row actions: View, Edit, Manage assignments, Archive / Restore.
 *
 * Each privileged item is wrapped in `<Protected>` and names a *permission*,
 * never a role — so a policy change in `core/roles.py` reaches this menu with no
 * edit here. Archive and Restore are mutually exclusive on purpose: offering
 * "Archive" on an already-archived case is an action that can only be a no-op.
 */
export function CaseRowActions({
  legalCase,
  onEdit,
  onArchive,
  onRestore,
  onAssign,
}: {
  legalCase: LegalCase;
  onEdit: (legalCase: LegalCase) => void;
  onArchive: (legalCase: LegalCase) => void;
  onRestore: (legalCase: LegalCase) => void;
  onAssign: (legalCase: LegalCase) => void;
}) {
  const t = useTranslations("cases.actions");
  const tActions = useTranslations("common.actions");

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8"
          aria-label={t("menuFor", { caseNumber: legalCase.caseNumber })}
        >
          <MoreHorizontal className="h-4 w-4" />
        </Button>
      </DropdownMenuTrigger>

      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuLabel>{t("label")}</DropdownMenuLabel>

        <DropdownMenuItem asChild>
          <Link href={caseRoute(legalCase.id)}>
            <Eye className="h-4 w-4" />
            {t("viewDetails")}
          </Link>
        </DropdownMenuItem>

        <Protected permission={PERMISSION.casesUpdate}>
          <DropdownMenuItem onSelect={() => onEdit(legalCase)}>
            <Pencil className="h-4 w-4" />
            {tActions("edit")}
          </DropdownMenuItem>
        </Protected>

        <Protected permission={PERMISSION.casesAssign}>
          <DropdownMenuItem onSelect={() => onAssign(legalCase)}>
            <UserCog className="h-4 w-4" />
            {t("manageAssignments")}
          </DropdownMenuItem>
        </Protected>

        <DropdownMenuSeparator />

        {/* Restoring is a status change (`cases:update`), archiving is the soft
            delete (`cases:delete`) — each names the permission its request
            actually needs, so neither offers an action the API would refuse. */}
        {legalCase.isArchived ? (
          <Protected permission={PERMISSION.casesUpdate}>
            <DropdownMenuItem onSelect={() => onRestore(legalCase)}>
              <ArchiveRestore className="h-4 w-4" />
              {t("restore")}
            </DropdownMenuItem>
          </Protected>
        ) : (
          <Protected permission={PERMISSION.casesDelete}>
            <DropdownMenuItem variant="destructive" onSelect={() => onArchive(legalCase)}>
              <Archive className="h-4 w-4" />
              {t("archive")}
            </DropdownMenuItem>
          </Protected>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
