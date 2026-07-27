"use client";

import { Menu } from "lucide-react";

import { Breadcrumbs } from "@/components/layout/breadcrumbs";
import { NotificationButton } from "@/components/layout/notification-button";
import { SearchBar } from "@/components/layout/search-bar";
import { UserMenu } from "@/components/layout/user-menu";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { useSidebarStore } from "@/stores/sidebar-store";

/**
 * Application top navigation bar.
 *
 * Sticky header shown above the main content. Contains the mobile drawer
 * trigger, breadcrumbs, the (placeholder) global search, the notification bell,
 * and the user menu. Composed entirely from shared/design-system components.
 */
export function AppHeader() {
  const toggleMobile = useSidebarStore((s) => s.toggleMobile);

  return (
    <header className="sticky top-0 z-30 flex h-16 items-center gap-3 border-b border-border bg-background/95 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/80 sm:px-6">
      <Button
        variant="ghost"
        size="icon"
        className="lg:hidden"
        onClick={toggleMobile}
        aria-label="Open navigation menu"
      >
        <Menu className="h-5 w-5" />
      </Button>

      <Breadcrumbs className="hidden sm:block" />

      <div className="ml-auto flex items-center gap-2">
        <SearchBar className="hidden md:block" />
        <Separator orientation="vertical" className="mx-1 hidden h-6 md:block" />
        <NotificationButton />
        <UserMenu />
      </div>
    </header>
  );
}
