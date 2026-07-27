"use client";

import { PanelLeftClose, PanelLeftOpen } from "lucide-react";

import { AppBrand } from "@/components/layout/app-brand";
import { SidebarNav } from "@/components/layout/sidebar-nav";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { useSidebarStore } from "@/stores/sidebar-store";
import { cn } from "@/lib/utils";

/**
 * Application sidebar.
 *
 * Two presentations driven by {@link useSidebarStore}:
 *
 * - Desktop (`lg+`): a persistent rail that collapses to an icon-only column.
 * - Mobile (`<lg`): a slide-over drawer (Sheet) toggled from the header.
 *
 * The nav list itself is shared via {@link SidebarNav}.
 */
export function AppSidebar() {
  const collapsed = useSidebarStore((s) => s.collapsed);
  const toggleCollapsed = useSidebarStore((s) => s.toggleCollapsed);
  const mobileOpen = useSidebarStore((s) => s.mobileOpen);
  const setMobileOpen = useSidebarStore((s) => s.setMobileOpen);

  return (
    <>
      {/* Desktop rail */}
      <aside
        className={cn(
          "sticky top-0 hidden h-svh shrink-0 border-r border-sidebar-border bg-sidebar lg:flex lg:flex-col",
          collapsed ? "w-[4.5rem]" : "w-64",
        )}
        aria-label="Sidebar"
      >
        <div
          className={cn(
            "flex h-16 items-center border-b border-sidebar-border px-4",
            collapsed && "justify-center px-0",
          )}
        >
          <AppBrand collapsed={collapsed} />
        </div>

        <ScrollArea className="flex-1">
          <div className={cn("p-3", collapsed && "px-2")}>
            <SidebarNav collapsed={collapsed} />
          </div>
        </ScrollArea>

        <div
          className={cn(
            "border-t border-sidebar-border p-3",
            collapsed && "px-2",
          )}
        >
          <Button
            variant="ghost"
            size={collapsed ? "icon" : "sm"}
            onClick={toggleCollapsed}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            className={cn(
              "text-muted-foreground",
              collapsed ? "mx-auto" : "w-full justify-start",
            )}
          >
            {collapsed ? (
              <PanelLeftOpen className="h-5 w-5" />
            ) : (
              <>
                <PanelLeftClose className="h-5 w-5" />
                <span>Collapse</span>
              </>
            )}
          </Button>
        </div>
      </aside>

      {/* Mobile drawer */}
      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent
          side="left"
          className="w-72 border-sidebar-border bg-sidebar p-0"
        >
          <SheetHeader className="h-16 flex-row items-center border-b border-sidebar-border px-4">
            <SheetTitle asChild>
              <AppBrand />
            </SheetTitle>
          </SheetHeader>
          <ScrollArea className="h-[calc(100svh-4rem)]">
            <div className="p-3">
              <SidebarNav onNavigate={() => setMobileOpen(false)} />
            </div>
          </ScrollArea>
        </SheetContent>
      </Sheet>
    </>
  );
}
