"use client";

import * as React from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { ArrowUpRight, RefreshCw, TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  WIDGETS_WITH_DESCRIPTION,
  WIDGET_CHROME,
} from "@/components/dashboard/labels";
import { useWidgetErrorMessage } from "@/hooks/use-dashboard";
import { cn } from "@/lib/utils";
import type { DashboardWidget, WidgetKey } from "@/types/dashboard";

/**
 * The frame every widget is drawn in.
 *
 * One component rather than nineteen, and it owns everything the spec asks of a
 * widget's *presentation* — the loading placeholder, the empty state, the failure
 * state, the refresh control, and the "see all" link. A renderer below it
 * receives data it can assume is present, which is what keeps nine small
 * components small.
 *
 * **A failed widget is a normal state here, not an error boundary.** The API
 * returns `state: "unavailable"` inside a successful response, so this card shows
 * a short explanation and a retry, the eighteen cards beside it are unaffected,
 * and the page never goes to an error screen — which is the whole of the spec's
 * *"one failing widget must not prevent the dashboard from loading"* seen from
 * the browser.
 *
 * The refresh button is per card, and it refreshes **one widget**: the request it
 * makes reads that widget's data and nothing else.
 */

export interface WidgetCardProps {
  widget: DashboardWidget;
  onRefresh?: (key: WidgetKey) => Promise<void> | void;
  /** Rendered when the widget has data. */
  children?: React.ReactNode;
  /**
   * Shown instead of `children` when the widget loaded and is empty.
   *
   * Already translated by the caller, which knows the widget's key — the shared
   * `common.states.emptyTitle` is the fallback when a widget has no wording of
   * its own.
   */
  emptyMessage?: string;
  className?: string;
}

export function WidgetCard({
  widget,
  onRefresh,
  children,
  emptyMessage,
  className,
}: WidgetCardProps) {
  const [isRefreshing, setIsRefreshing] = React.useState(false);
  const t = useTranslations("dashboard");
  const tWidgets = useTranslations("dashboard.widgets");
  const tStates = useTranslations("common.states");
  const widgetErrorMessage = useWidgetErrorMessage();

  const key = widget.widget.key;
  const chrome = WIDGET_CHROME[key];
  const Icon = chrome.icon;
  const title = tWidgets(`${key}.title`);

  const refresh = React.useCallback(async () => {
    if (!onRefresh || isRefreshing) return;
    setIsRefreshing(true);
    try {
      await onRefresh(widget.widget.key);
    } finally {
      setIsRefreshing(false);
    }
  }, [isRefreshing, onRefresh, widget.widget.key]);

  return (
    <Card className={cn("h-full", className)} aria-busy={isRefreshing}>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Icon className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          {title}
        </CardTitle>
        {WIDGETS_WITH_DESCRIPTION.has(key) ? (
          <CardDescription>{tWidgets(`${key}.description`)}</CardDescription>
        ) : null}

        <CardAction className="flex items-center gap-1">
          {chrome.href ? (
            <Button asChild variant="ghost" size="sm">
              <Link href={chrome.href}>
                {t("card.seeAll")}
                <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
              </Link>
            </Button>
          ) : null}
          {onRefresh ? (
            <Button
              variant="ghost"
              size="icon"
              onClick={refresh}
              disabled={isRefreshing}
              // The label names the widget, so a screen-reader user pressing one
              // of nineteen identical buttons knows which card they refreshed.
              // Interpolated rather than concatenated, so a language whose verb
              // follows its object can order the sentence for itself.
              aria-label={t("card.refreshWidget", { widget: title })}
            >
              <RefreshCw
                className={cn("h-4 w-4", isRefreshing && "animate-spin")}
                aria-hidden="true"
              />
            </Button>
          ) : null}
        </CardAction>
      </CardHeader>

      <CardContent>
        {isRefreshing ? (
          <WidgetSkeleton />
        ) : widget.state === "unavailable" ? (
          <p
            role="status"
            className="flex items-start gap-2 rounded-md border border-border bg-muted p-3 text-sm text-muted-foreground"
          >
            <TriangleAlert
              className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
              aria-hidden="true"
            />
            <span>{widgetErrorMessage(widget.errorCode)}</span>
          </p>
        ) : widget.state === "empty" ? (
          <p className="py-4 text-sm text-muted-foreground">
            {emptyMessage ?? tStates("emptyTitle")}
          </p>
        ) : (
          children
        )}
      </CardContent>
    </Card>
  );
}

/** The placeholder a card shows while its data is in flight. */
export function WidgetSkeleton() {
  return (
    <div className="flex flex-col gap-2" aria-hidden="true">
      <Skeleton className="h-4 w-2/3" />
      <Skeleton className="h-4 w-1/2" />
      <Skeleton className="h-4 w-3/4" />
    </div>
  );
}

/** A card-shaped placeholder for the whole page's first load. */
export function WidgetCardSkeleton() {
  return (
    <Card className="h-full">
      <CardHeader>
        <CardTitle className="text-sm">
          <Skeleton className="h-4 w-32" />
        </CardTitle>
      </CardHeader>
      <CardContent>
        <WidgetSkeleton />
      </CardContent>
    </Card>
  );
}
