"use client";

import { Menu } from "lucide-react";
import { useTranslations } from "next-intl";

import { Breadcrumbs } from "@/components/layout/breadcrumbs";
import { LanguageSwitcher } from "@/components/layout/language-switcher";
import { NotificationButton } from "@/components/layout/notification-button";
import { SearchBar } from "@/components/layout/search-bar";
import { UserMenu } from "@/components/layout/user-menu";
import { ConnectionStatusIndicator } from "@/components/realtime/connection-status";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { useSidebarStore } from "@/stores/sidebar-store";

/**
 * Application top navigation bar.
 *
 * Sticky header shown above the main content. Contains the mobile drawer
 * trigger, breadcrumbs, the (placeholder) global search, the connection
 * indicator, the language switcher, the notification bell, and the user menu.
 * Composed entirely from shared/design-system components.
 */
export function AppHeader() {
  const toggleMobile = useSidebarStore((s) => s.toggleMobile);
  const t = useTranslations("common.a11y");

  return (
    <header className="sticky top-0 z-30 flex h-16 items-center gap-3 border-b border-border bg-background/95 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/80 sm:px-6">
      <Button
        variant="ghost"
        size="icon"
        className="lg:hidden"
        onClick={toggleMobile}
        aria-label={t("openNavigation")}
      >
        <Menu className="h-5 w-5" />
      </Button>

      <Breadcrumbs className="hidden sm:block" />

      <div className="ms-auto flex items-center gap-2">
        {/* Renders nothing while updates are live, which is nearly always. It is
            the one control on this bar that appears only when something is
            wrong — a permanently-green indicator is furniture people learn to
            ignore. */}
        <ConnectionStatusIndicator />
        <SearchBar className="hidden md:block" />
        <Separator orientation="vertical" className="mx-1 hidden h-6 md:block" />
        {/* `ui-context.md`: "Language switching is available from the top
            navigation bar." Beside the bell rather than inside the account menu,
            because a reader who cannot read the interface cannot find a control
            buried one level down in it. */}
        <LanguageSwitcher />
        <NotificationButton />
        <UserMenu />
      </div>
    </header>
  );
}
