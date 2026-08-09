import {
  Bell,
  Bot,
  CircleAlert,
  CircleCheck,
  FileText,
  FileBarChart,
  Gavel,
  Info,
  Megaphone,
  ScanText,
  Scale,
  TriangleAlert,
  UserCog,
  type LucideIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";
import {
  isKnownCategory,
  type KnownNotificationCategory,
  type NotificationType,
} from "@/types/notification";

/**
 * The icon and colour for one notification.
 *
 * **Two vocabularies decide it, and they answer different questions.** The
 * *category* says which part of the platform the news came from and chooses the
 * glyph; the *type* says whether it is news, a success, or a problem and chooses
 * the colour. Using one for both would mean every failed extraction looked like
 * every successful one, or that reports and hearings were indistinguishable.
 *
 * Colour comes from the platform's state tokens (`info` / `success` / `warning` /
 * `destructive`), never from a hardcoded value, and it is **never the only
 * signal**: the notification's title sits beside it as text and the icon is
 * `aria-hidden`, so the state is not conveyed by colour or shape alone. The same
 * WCAG rule every other badge on the platform follows.
 *
 * A category this build has never heard of renders the neutral bell rather than
 * failing — the registry is open on the server by design, and a feed that broke
 * when a ninth category shipped would defeat the point of that.
 */

const CATEGORY_ICONS: Record<KnownNotificationCategory, LucideIcon> = {
  case: Scale,
  document: FileText,
  hearing: Gavel,
  ocr: ScanText,
  ai: Bot,
  report: FileBarChart,
  user: UserCog,
  system: Megaphone,
};

const TYPE_STYLES: Record<NotificationType, string> = {
  information: "border-info/30 bg-info/10 text-info",
  success: "border-success/30 bg-success/10 text-success",
  warning: "border-warning/30 bg-warning/10 text-warning",
  error: "border-destructive/30 bg-destructive/10 text-destructive",
};

/** Fallback glyphs, used when a category is one this build does not know. */
const TYPE_ICONS: Record<NotificationType, LucideIcon> = {
  information: Info,
  success: CircleCheck,
  warning: TriangleAlert,
  error: CircleAlert,
};

export function NotificationIcon({
  category,
  notificationType,
  className,
}: {
  category: string;
  notificationType: NotificationType;
  className?: string;
}) {
  const Icon = isKnownCategory(category)
    ? CATEGORY_ICONS[category]
    : (TYPE_ICONS[notificationType] ?? Bell);

  return (
    <span
      className={cn(
        "flex h-9 w-9 shrink-0 items-center justify-center rounded-full border",
        TYPE_STYLES[notificationType],
        className,
      )}
    >
      <Icon className="h-4 w-4" aria-hidden="true" />
    </span>
  );
}
