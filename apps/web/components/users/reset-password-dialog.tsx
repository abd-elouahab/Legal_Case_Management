"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { AlertCircle, Check, Copy, KeyRound } from "lucide-react";
import { toast } from "sonner";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Spinner } from "@/components/shared/spinner";
import { useResetUserPassword, useUserErrorMessage } from "@/hooks/use-users";
import type { ManagedUser } from "@/types/user";

/**
 * Reset Password dialog.
 *
 * Two states in one dialog: confirm, then reveal. The reveal step exists because
 * the API returns the generated password **once** — only its hash is stored, so
 * there is no way to show it again. Closing the dialog without noting it means
 * running another reset.
 *
 * The user is signed out everywhere and must choose a new password at their next
 * sign-in, which the copy states so the administrator can tell them.
 */
export function ResetPasswordDialog({
  user,
  open,
  onOpenChange,
}: {
  user: ManagedUser | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const resetPassword = useResetUserPassword();
  const t = useTranslations("users.resetPasswordDialog");
  const tActions = useTranslations("common.actions");
  const errorMessage = useUserErrorMessage();
  const [temporaryPassword, setTemporaryPassword] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [copied, setCopied] = React.useState(false);

  // Start each reset from a clean slate. Adjusted during render rather than in
  // an effect so a previously revealed password is never briefly visible again
  // (https://react.dev/learn/you-might-not-need-an-effect) — which for a
  // credential is the difference between a cosmetic glitch and a leak.
  const [wasOpen, setWasOpen] = React.useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) {
      setTemporaryPassword(null);
      setError(null);
      setCopied(false);
    }
  }

  async function confirm() {
    if (!user) return;
    setError(null);

    try {
      const result = await resetPassword.mutateAsync(user.id);
      setTemporaryPassword(result.temporaryPassword);
    } catch (cause) {
      setError(errorMessage(cause));
    }
  }

  async function copy() {
    if (!temporaryPassword) return;

    try {
      await navigator.clipboard.writeText(temporaryPassword);
      setCopied(true);
      toast.success(t("copied"));
    } catch {
      // Clipboard access can be denied or unavailable (insecure context). The
      // password is on screen either way, so this is not worth an error banner.
      toast.error(t("copyRefused"));
    }
  }

  const isPending = resetPassword.isPending;
  const isRevealed = temporaryPassword !== null;

  return (
    <Dialog open={open} onOpenChange={isPending ? undefined : onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>
            {isRevealed
              ? t("revealedTitle")
              : t("title", { name: user?.fullName ?? "" })}
          </DialogTitle>
          <DialogDescription>
            {isRevealed
              ? t("revealedDescription")
              : t("description")}
          </DialogDescription>
        </DialogHeader>

        {error ? (
          <Alert variant="destructive" role="alert">
            <AlertCircle className="h-4 w-4" />
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        ) : null}

        {isRevealed ? (
          <div className="flex items-center gap-2 rounded-lg border border-border bg-muted/50 p-3">
            <code className="flex-1 break-all font-mono text-sm text-foreground">
              {temporaryPassword}
            </code>
            <Button
              type="button"
              variant="outline"
              size="icon"
              onClick={copy}
              aria-label={t("copyLabel")}
            >
              {copied ? <Check className="h-4 w-4 text-success" /> : <Copy className="h-4 w-4" />}
            </Button>
          </div>
        ) : null}

        <DialogFooter>
          {isRevealed ? (
            <Button type="button" onClick={() => onOpenChange(false)}>
              {t("done")}
            </Button>
          ) : (
            <>
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={isPending}
              >
                {tActions("cancel")}
              </Button>
              <Button type="button" onClick={confirm} disabled={isPending}>
                {isPending ? (
                  <>
                    <Spinner className="h-4 w-4 text-current" />
                    {t("resetting")}
                  </>
                ) : (
                  <>
                    <KeyRound className="h-4 w-4" />
                    {t("confirm")}
                  </>
                )}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
