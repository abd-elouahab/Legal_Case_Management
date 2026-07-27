"use client";

import * as React from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { AlertCircle, Eye, EyeOff, LogIn } from "lucide-react";
import { useForm } from "react-hook-form";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Spinner } from "@/components/shared/spinner";
import { useLogin } from "@/hooks/use-login";
import { loginFormSchema, type LoginFormValues } from "@/lib/validation/auth";

/**
 * Sign-in form.
 *
 * Built entirely from Design System components (`Input`, `Label`, `Button`,
 * `Alert`) — no bespoke UI. Validation is React Hook Form + Zod per
 * `architecture.md`; the server re-validates everything.
 *
 * States covered: idle, field-level validation errors, submitting (loading),
 * request failure (error), and success (redirect handled by `useLogin`).
 */
export function LoginForm() {
  const { submit, isPending, error, reset } = useLogin();
  const [showPassword, setShowPassword] = React.useState(false);

  const form = useForm<LoginFormValues>({
    resolver: zodResolver(loginFormSchema),
    defaultValues: { email: "", password: "" },
    // Validate on blur so users are not corrected mid-typing.
    mode: "onBlur",
  });

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = form;

  const onSubmit = handleSubmit(async (values) => {
    await submit({
      email: values.email.trim().toLowerCase(),
      password: values.password,
    });
  });

  // Clear a stale server error as soon as the user edits the form again.
  const clearServerError = React.useCallback(() => {
    if (error) reset();
  }, [error, reset]);

  const emailErrorId = "login-email-error";
  const passwordErrorId = "login-password-error";

  return (
    <form onSubmit={onSubmit} noValidate className="flex flex-col gap-5">
      {error ? (
        <Alert variant="destructive" role="alert">
          <AlertCircle className="h-4 w-4" />
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      <div className="flex flex-col gap-2">
        <Label htmlFor="email">Email</Label>
        <Input
          id="email"
          type="email"
          inputMode="email"
          autoComplete="username"
          autoFocus
          placeholder="you@example.com"
          aria-invalid={Boolean(errors.email)}
          aria-describedby={errors.email ? emailErrorId : undefined}
          disabled={isPending}
          {...register("email", { onChange: clearServerError })}
        />
        {errors.email ? (
          <p id={emailErrorId} className="text-sm text-destructive">
            {errors.email.message}
          </p>
        ) : null}
      </div>

      <div className="flex flex-col gap-2">
        <Label htmlFor="password">Password</Label>
        <div className="relative">
          <Input
            id="password"
            type={showPassword ? "text" : "password"}
            autoComplete="current-password"
            placeholder="Enter your password"
            className="pr-10"
            aria-invalid={Boolean(errors.password)}
            aria-describedby={errors.password ? passwordErrorId : undefined}
            disabled={isPending}
            {...register("password", { onChange: clearServerError })}
          />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={() => setShowPassword((visible) => !visible)}
            disabled={isPending}
            aria-label={showPassword ? "Hide password" : "Show password"}
            aria-pressed={showPassword}
            className="absolute right-1 top-1/2 h-8 w-8 -translate-y-1/2 text-muted-foreground"
          >
            {showPassword ? (
              <EyeOff className="h-4 w-4" />
            ) : (
              <Eye className="h-4 w-4" />
            )}
          </Button>
        </div>
        {errors.password ? (
          <p id={passwordErrorId} className="text-sm text-destructive">
            {errors.password.message}
          </p>
        ) : null}
      </div>

      <Button type="submit" className="w-full" disabled={isPending}>
        {isPending ? (
          <>
            <Spinner className="h-4 w-4 text-current" />
            Signing in…
          </>
        ) : (
          <>
            <LogIn className="h-4 w-4" />
            Sign in
          </>
        )}
      </Button>
    </form>
  );
}
