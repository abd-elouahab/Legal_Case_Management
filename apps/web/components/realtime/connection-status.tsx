"use client";

import { Loader2, RefreshCw, Wifi, WifiOff } from "lucide-react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useConnectionStatus, useRealtimeClient } from "@/hooks/use-realtime";
import { cn } from "@/lib/utils";
import type { ConnectionStatus } from "@/types/realtime";

/**
 * The header's live-updates indicator.
 *
 * `15-real-time-synchronization.md` asks for a connection status indicator and
 * for graceful degradation, and the two are the same requirement seen from
 * either side: the application never *depends* on the socket, so the only thing
 * a user needs from this control is an honest answer to "am I seeing the latest?"
 *
 * Three deliberate choices about what it shows:
 *
 * * **Nothing at all while connected.** A green dot that is always green is
 *   furniture — it trains people to ignore the one place that would tell them
 *   something is wrong. The indicator appears when the answer stops being yes.
 * * **Never colour alone**, per `ui-context.md`'s accessibility rule: every
 *   state carries an icon and a label, and the label is what a screen reader
 *   announces through `role="status"`.
 * * **A retry, only when retrying is meaningful.** `offline` means the client
 *   has stopped trying — it is the one state a person can act on, so it is the
 *   one state with a button. Offering retry mid-reconnect would invite somebody
 *   to reset a backoff that is already working.
 */

interface StatePresentation {
  /** Translation key under `realtime`, resolved at render. */
  key: string;
  icon: typeof Wifi;
  className: string;
  spin?: boolean;
}

/**
 * How each state reads.
 *
 * The wording is about **the data**, not about the transport: "updates paused"
 * rather than "WebSocket disconnected". A lawyer needs to know whether the case
 * in front of them is current; the name of the protocol carrying it is not their
 * concern, and a message that names it invites a support ticket instead of a
 * page refresh. That argument is about *what* the sentence says, so it survives
 * translation — which is why what lives here is a key and an icon, and the
 * sentences live in `messages/*.json` beside every other one on the platform.
 */
const PRESENTATION: Record<Exclude<ConnectionStatus, "idle" | "connected">, StatePresentation> = {
  connecting: {
    key: "connecting",
    icon: Loader2,
    className: "text-muted-foreground",
    spin: true,
  },
  reconnecting: {
    key: "reconnecting",
    icon: Loader2,
    className: "text-warning",
    spin: true,
  },
  offline: {
    key: "offline",
    icon: WifiOff,
    className: "text-muted-foreground",
  },
};

export function ConnectionStatusIndicator({ className }: { className?: string }) {
  const status = useConnectionStatus();
  const client = useRealtimeClient();
  const t = useTranslations("realtime");

  // `idle` is "no channel in this tree" — an authentication page, or a
  // deployment with the feature off. Both are normal, and neither is something
  // to report.
  if (status === "idle" || status === "connected") return null;

  const presentation = PRESENTATION[status];
  const Icon = presentation.icon;
  const label = t(`${presentation.key}.label`);
  const detail = t(`${presentation.key}.detail`);

  return (
    <div
      // `polite` rather than `assertive`: losing live updates is worth
      // announcing, and not worth interrupting someone mid-sentence for.
      role="status"
      aria-live="polite"
      className={cn("flex items-center gap-1.5", className)}
    >
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            className={cn(
              "flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium",
              presentation.className,
            )}
          >
            <Icon className={cn("h-4 w-4", presentation.spin && "animate-spin")} />
            <span className="hidden sm:inline">{label}</span>
            {/* The label carries the meaning on small screens too, where the
                text is hidden — an icon alone would be colour-and-shape only. */}
            <span className="sr-only sm:hidden">{label}</span>
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs">{detail}</TooltipContent>
      </Tooltip>

      {status === "offline" && client ? (
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          onClick={() => client.retry()}
          aria-label={t("retry")}
        >
          <RefreshCw className="h-4 w-4" />
        </Button>
      ) : null}
    </div>
  );
}
