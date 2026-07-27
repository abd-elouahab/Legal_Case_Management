import { PageContainer } from "@/components/layout/page-container";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * Skeleton placeholder for a loading page.
 *
 * Mirrors the standard page layout (header block + a grid of card-shaped
 * blocks) so route-level `loading.tsx` files render a stable shape while the
 * real content streams in.
 */
export function PageSkeleton() {
  return (
    <PageContainer>
      <div className="flex flex-col gap-2">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-4 w-80" />
      </div>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {Array.from({ length: 6 }).map((_, index) => (
          <Skeleton key={index} className="h-32 w-full rounded-xl" />
        ))}
      </div>
      <Skeleton className="h-64 w-full rounded-xl" />
    </PageContainer>
  );
}
