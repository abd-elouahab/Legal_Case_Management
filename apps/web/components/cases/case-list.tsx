"use client";

import * as React from "react";
import { FilePlus2, Scale, SearchX } from "lucide-react";
import { toast } from "sonner";

import { Protected } from "@/components/auth/protected";
import { EmptyState } from "@/components/shared/empty-state";
import { ErrorState } from "@/components/shared/error-state";
import { Button } from "@/components/ui/button";
import { ArchiveCaseDialog } from "@/components/cases/archive-case-dialog";
import { AssignCaseDialog } from "@/components/cases/assign-case-dialog";
import { CaseFilters } from "@/components/cases/case-filters";
import { CasePagination } from "@/components/cases/case-pagination";
import { CaseTable } from "@/components/cases/case-table";
import { CaseTableSkeleton } from "@/components/cases/case-table-skeleton";
import { CreateCaseDialog } from "@/components/cases/create-case-dialog";
import { EditCaseDialog } from "@/components/cases/edit-case-dialog";
import { useCaseListQuery } from "@/hooks/use-case-list-query";
import { caseErrorMessage, useCases, useRestoreCase } from "@/hooks/use-cases";
import { PERMISSION } from "@/types/authorization";
import type { LegalCase } from "@/types/case";

/**
 * The case list: search, filters, table, pagination, and the dialogs.
 *
 * A client component because everything here is interactive. It holds only *UI*
 * state — which dialog is open and which case it targets. Query state lives in
 * `useCaseListQuery`, server state in `useCases`, so this component composes
 * them rather than implementing either.
 *
 * What a user sees is scoped by the API: a caller without `cases:view-all` gets
 * only the cases they are assigned to, and the totals count only those. Nothing
 * here has to know that.
 */

/** Which dialog is open, and on which case. */
type DialogState =
  | { kind: "none" }
  | { kind: "create" }
  | { kind: "edit"; legalCase: LegalCase }
  | { kind: "assign"; legalCase: LegalCase }
  | { kind: "archive"; legalCase: LegalCase };

export function CaseList() {
  const list = useCaseListQuery();
  const { data, isLoading, isFetching, isError, error, refetch } = useCases(list.query);
  const restoreCase = useRestoreCase();

  const [dialog, setDialog] = React.useState<DialogState>({ kind: "none" });
  const close = React.useCallback(() => setDialog({ kind: "none" }), []);

  const handleRestore = React.useCallback(
    async (legalCase: LegalCase) => {
      try {
        await restoreCase.mutateAsync(legalCase.id);
        toast.success(`Case ${legalCase.caseNumber} was restored.`);
      } catch (cause) {
        toast.error(caseErrorMessage(cause));
      }
    },
    [restoreCase],
  );

  const cases = data?.items ?? [];
  // `isFetching` without `isLoading` is a background refresh — a new page or
  // filter — where the previous data is still on screen.
  const isRefreshing = isFetching && !isLoading;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-4">
        <div className="flex justify-end">
          <Protected permission={PERMISSION.casesCreate}>
            <Button onClick={() => setDialog({ kind: "create" })}>
              <FilePlus2 className="h-4 w-4" />
              New case
            </Button>
          </Protected>
        </div>

        <CaseFilters list={list} />
      </div>

      {isLoading ? (
        <CaseTableSkeleton />
      ) : isError ? (
        <ErrorState
          title="Could not load cases"
          description={caseErrorMessage(error)}
          onRetry={() => void refetch()}
        />
      ) : cases.length === 0 ? (
        // Two genuinely different situations: an empty caseload needs a way to
        // open one, a fruitless search needs a way back to the full list.
        list.isFiltered ? (
          <EmptyState
            icon={SearchX}
            title="No cases match your filters"
            description="Try a different search term or date range, or clear the filters to see everything."
            action={
              <Button variant="outline" onClick={list.reset}>
                Clear filters
              </Button>
            }
          />
        ) : (
          <EmptyState
            icon={Scale}
            title="No cases yet"
            description="Open the first case to start tracking a matter through its lifecycle."
            action={
              <Protected permission={PERMISSION.casesCreate}>
                <Button onClick={() => setDialog({ kind: "create" })}>
                  <FilePlus2 className="h-4 w-4" />
                  New case
                </Button>
              </Protected>
            }
          />
        )
      ) : (
        <>
          <CaseTable
            cases={cases}
            sortBy={list.query.sortBy}
            sortOrder={list.query.sortOrder}
            onToggleSort={list.toggleSort}
            onEdit={(legalCase) => setDialog({ kind: "edit", legalCase })}
            onAssign={(legalCase) => setDialog({ kind: "assign", legalCase })}
            onArchive={(legalCase) => setDialog({ kind: "archive", legalCase })}
            onRestore={handleRestore}
            isRefreshing={isRefreshing}
          />

          <CasePagination
            page={data?.page ?? 1}
            pageSize={data?.pageSize ?? list.query.pageSize}
            totalPages={data?.totalPages ?? 1}
            totalRecords={data?.totalRecords ?? 0}
            onPageChange={list.setPage}
            disabled={isRefreshing}
          />
        </>
      )}

      <CreateCaseDialog
        open={dialog.kind === "create"}
        onOpenChange={(open) => (open ? setDialog({ kind: "create" }) : close())}
      />

      <EditCaseDialog
        legalCase={dialog.kind === "edit" ? dialog.legalCase : null}
        open={dialog.kind === "edit"}
        onOpenChange={(open) => (open ? undefined : close())}
      />

      <AssignCaseDialog
        legalCase={dialog.kind === "assign" ? dialog.legalCase : null}
        open={dialog.kind === "assign"}
        onOpenChange={(open) => (open ? undefined : close())}
      />

      <ArchiveCaseDialog
        legalCase={dialog.kind === "archive" ? dialog.legalCase : null}
        open={dialog.kind === "archive"}
        onOpenChange={(open) => (open ? undefined : close())}
      />
    </div>
  );
}
