import type { Metadata } from "next";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";

import { Providers } from "@/components/providers";
import { DEFAULT_LOCALE, LOCALE_DIRECTIONS } from "@/lib/i18n/config";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "Legal Case Management Platform",
    template: "%s · Legal Case Management Platform",
  },
  description:
    "AI-powered collaborative platform for administrators, lawyers, and court representatives to manage legal cases.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    // No `dark` class here any more: `next-themes` writes the theme class onto
    // this element before paint, from the choice stored in `localStorage`.
    // Hard-coding one would make every light-mode session flash dark on load, and
    // `suppressHydrationWarning` is what lets the class the script wrote differ
    // from the server's markup without React complaining.
    //
    // `lang` and `dir` are the *application default* here and are rewritten by
    // `components/i18n/locale-provider.tsx` once the reader's language is known.
    // They cannot be resolved on the server: the preference arrives from an
    // authenticated request, and the access token lives in browser memory rather
    // than in a cookie, so this render has no way to ask. The same
    // `suppressHydrationWarning` covers the difference, and Arabic is applied in
    // an effect that runs before paint.
    <html
      lang={DEFAULT_LOCALE}
      dir={LOCALE_DIRECTIONS[DEFAULT_LOCALE]}
      className={`${GeistSans.variable} ${GeistMono.variable}`}
      suppressHydrationWarning
    >
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
