import type { LucideIcon } from "lucide-react";
import { Bot, FileText, StickyNote } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

/**
 * Placeholder cards for the modules that will attach to a case.
 *
 * **Layout only — no functionality.** The case workspace's shape exists now so
 * that Notes, the AI Assistant, and Reports each land in a place already reserved
 * for them, rather than forcing a redesign of the details page when the next one
 * ships.
 *
 * Each card says plainly that the feature is not built yet, so nobody mistakes an
 * empty card for a loading failure.
 *
 * **Documents and Timeline no longer have cards here.** Both modules shipped, so
 * the case details page renders the real list
 * (`components/documents/case-documents`) and the real history
 * (`components/timeline/case-timeline`) in their place — a placeholder beside a
 * working feature would read as a bug.
 */

interface PlaceholderSection {
  key: string;
  title: string;
  description: string;
  icon: LucideIcon;
}

const SECTIONS: PlaceholderSection[] = [
  {
    key: "notes",
    title: "Notes",
    description: "Working notes shared between the administrator and the assigned team.",
    icon: StickyNote,
  },
  {
    key: "ai",
    title: "AI Assistant",
    description:
      "Question answering, summaries, and information extraction grounded in this case's documents.",
    icon: Bot,
  },
  {
    key: "reports",
    title: "Reports",
    description: "Generated legal reports and exports produced from this case's material.",
    icon: FileText,
  },
];

export function CasePlaceholderSections() {
  return (
    <section className="flex flex-col gap-4" aria-labelledby="case-upcoming-modules">
      <h3 id="case-upcoming-modules" className="text-sm font-medium text-muted-foreground">
        Coming to this case
      </h3>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {SECTIONS.map(({ key, title, description, icon: Icon }) => (
          <Card key={key} className="border-dashed">
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <Icon className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
                {title}
              </CardTitle>
              <CardDescription>{description}</CardDescription>
            </CardHeader>
            <CardContent>
              <p className="text-xs text-muted-foreground">Available in an upcoming release.</p>
            </CardContent>
          </Card>
        ))}
      </div>
    </section>
  );
}
