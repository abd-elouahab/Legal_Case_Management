import { RedirectIfAuthenticated } from "@/components/auth/redirect-if-authenticated";
import { LoginForm } from "@/components/auth/login-form";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { createMetadata } from "@/lib/metadata";

export const metadata = createMetadata(
  "Sign In",
  "Sign in to the Legal Case Management Platform.",
);

/**
 * Login page.
 *
 * A lightweight Server Component: the interactive form and the session check are
 * isolated in Client Components, keeping this page a thin composition layer.
 */
export default function LoginPage() {
  return (
    <RedirectIfAuthenticated>
      <Card>
        <CardHeader>
          <CardTitle>Sign in</CardTitle>
          <CardDescription>
            Enter your credentials to access your cases.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <LoginForm />
        </CardContent>
      </Card>
    </RedirectIfAuthenticated>
  );
}
