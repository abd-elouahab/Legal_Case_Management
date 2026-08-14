"use client";

import { useTranslations } from "next-intl";

import { RedirectIfAuthenticated } from "@/components/auth/redirect-if-authenticated";
import { LoginForm } from "@/components/auth/login-form";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
/**
 * Login page.
 *
 * **A Client Component, unlike every page under `(protected)`.** Those keep their
 * `metadata` export and push the one translated block into `PageHeader`; this
 * page has no such block — its heading lives inside a card — and it is the one
 * screen reached *before* the platform knows who the reader is, so its language
 * comes from `localStorage` rather than from a setting. `app/(auth)/layout.tsx`
 * carries the route's metadata, which stays in the default language for the
 * reason `page-header.tsx` records: there is no locale in the URL, so the server
 * rendering `<head>` does not know one.
 */
export default function LoginPage() {
  const t = useTranslations("auth.login");

  return (
    <RedirectIfAuthenticated>
      <Card>
        <CardHeader>
          <CardTitle>{t("title")}</CardTitle>
          <CardDescription>{t("description")}</CardDescription>
        </CardHeader>
        <CardContent>
          <LoginForm />
        </CardContent>
      </Card>
    </RedirectIfAuthenticated>
  );
}
