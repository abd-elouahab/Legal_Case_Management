import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

/**
 * Loading skeleton for the document list.
 *
 * Mirrors the real table's column layout, including which columns hide at which
 * breakpoint and whether the case column is shown, so the page does not reflow
 * when the data arrives — a skeleton that is the wrong shape is worse than a
 * spinner, because it promises a layout it then changes.
 */
export function DocumentTableSkeleton({
  rows = 5,
  showCase = true,
}: {
  rows?: number;
  showCase?: boolean;
}) {
  return (
    <div
      className="w-full overflow-x-auto rounded-lg border border-border"
      aria-busy="true"
      aria-label="Loading documents"
    >
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>File name</TableHead>
            {showCase ? <TableHead className="hidden lg:table-cell">Case</TableHead> : null}
            <TableHead>Category</TableHead>
            <TableHead>Size</TableHead>
            <TableHead className="hidden lg:table-cell">Version</TableHead>
            <TableHead className="hidden xl:table-cell">Uploaded by</TableHead>
            <TableHead className="hidden xl:table-cell">Upload date</TableHead>
            <TableHead className="w-12" />
          </TableRow>
        </TableHeader>

        <TableBody>
          {Array.from({ length: rows }, (_, index) => (
            <TableRow key={index}>
              <TableCell>
                <div className="flex items-center gap-2">
                  <Skeleton className="h-4 w-4 rounded" />
                  <Skeleton className="h-4 w-48" />
                </div>
              </TableCell>
              {showCase ? (
                <TableCell className="hidden lg:table-cell">
                  <Skeleton className="h-4 w-28" />
                </TableCell>
              ) : null}
              <TableCell>
                <Skeleton className="h-5 w-24 rounded-full" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-4 w-16" />
              </TableCell>
              <TableCell className="hidden lg:table-cell">
                <Skeleton className="h-4 w-10" />
              </TableCell>
              <TableCell className="hidden xl:table-cell">
                <Skeleton className="h-4 w-28" />
              </TableCell>
              <TableCell className="hidden xl:table-cell">
                <Skeleton className="h-4 w-28" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-8 w-8 rounded-md" />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
