"use client";

import { Check, Languages } from "lucide-react";
import { useTranslations } from "next-intl";

import { useLocale } from "@/components/i18n/locale-provider";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { LOCALES, LOCALE_NAMES } from "@/lib/i18n/config";
import { cn } from "@/lib/utils";

/**
 * Language selector, in the top navigation bar.
 *
 * `ui-context.md`: *"Language switching is available from the top navigation
 * bar."* It sits beside the notification bell rather than inside the account
 * menu, and that placement is the whole accessibility argument for this
 * component: somebody who cannot read the interface cannot find a control buried
 * one level down inside it. The trigger is an icon, which needs no language to
 * recognise.
 *
 * **Every option is written in its own language.** `Français` is `Français` on an
 * Arabic screen, not `الفرنسية` — a reader looking for their own language should
 * find the word they already recognise rather than having to read the current
 * one first. That is why `LOCALE_NAMES` lives in `lib/i18n/config.ts` and not in
 * the message catalogues: it is the one string on the platform that must never be
 * translated.
 *
 * **Switching is immediate.** `useLocale().setLocale` applies the new catalogue
 * before the save round-trips, which is `21-localization.md`'s *"immediate
 * language switching"*; the preference is persisted through Settings, so the
 * choice survives a reload and a new device. A signed-out visitor on the login
 * page can still switch — the change is kept locally and adopted by their account
 * the next time they sign in.
 *
 * **It changes presentation and nothing else.** No route changes, no data is
 * refetched for permission reasons, and no request is re-authorized: the spec's
 * *"language switching cannot affect application permissions"* holds because
 * there is nothing here that could.
 */
export function LanguageSwitcher({ className }: { className?: string }) {
  const { locale, setLocale } = useLocale();
  const t = useTranslations("shell.language");

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className={cn("rounded-full", className)}
          // The accessible name states the *current* language rather than only
          // "change language", so a screen-reader user knows what they are
          // switching from without opening the menu — the same reasoning the
          // notification bell's accessible name uses for its count.
          aria-label={t("current", { language: LOCALE_NAMES[locale] })}
        >
          <Languages className="h-5 w-5" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-44">
        <DropdownMenuLabel>{t("label")}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {LOCALES.map((option) => {
          const selected = option === locale;

          return (
            <DropdownMenuItem
              key={option}
              onSelect={() => setLocale(option)}
              // The selected entry is marked with a check *and* announced as
              // checked — `ui-context.md` forbids conveying state by appearance
              // alone, and a highlighted row is exactly that.
              aria-checked={selected}
              role="menuitemradio"
              className="justify-between"
            >
              {/* `lang` on the element is what tells a screen reader to switch
                  voice for this word, and what lets the browser shape Arabic
                  correctly inside an otherwise Latin menu. */}
              <span lang={option}>{LOCALE_NAMES[option]}</span>
              {selected ? <Check className="h-4 w-4" aria-hidden="true" /> : null}
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
