/**
 * Tests for route protection: the client-side guards, the request-level proxy,
 * and the open-redirect defence on the post-login target.
 */

import { describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";

import { RedirectIfAuthenticated } from "@/components/auth/redirect-if-authenticated";
import { RequireAuth } from "@/components/auth/require-auth";
import { safeRedirectTarget } from "@/lib/routes";
import { useSessionStore } from "@/stores/session-store";
import { TEST_SESSION_USER } from "./helpers";

const replace = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn(), refresh: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

/**
 * Session transitions are wrapped in `act` because a store update outside it
 * would re-render the guard without React flushing effects, producing an
 * `act(...)` warning and hiding the redirect the test is asserting on.
 */
function signedIn() {
  act(() => {
    useSessionStore.setState({ user: TEST_SESSION_USER, status: "authenticated" });
  });
}

function signedOut() {
  act(() => {
    useSessionStore.setState({ user: null, status: "unauthenticated" });
  });
}

function stillResolving() {
  act(() => {
    useSessionStore.setState({ user: null, status: "loading" });
  });
}

describe("RequireAuth", () => {
  it("renders protected content for an authenticated user", () => {
    replace.mockClear();
    signedIn();

    render(
      <RequireAuth>
        <p>Case dashboard</p>
      </RequireAuth>,
    );

    expect(screen.getByText("Case dashboard")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it("redirects an unauthenticated user to the login page", async () => {
    replace.mockClear();
    signedOut();

    render(
      <RequireAuth>
        <p>Case dashboard</p>
      </RequireAuth>,
    );

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
  });

  it("never renders protected content to an unauthenticated user", () => {
    replace.mockClear();
    signedOut();

    render(
      <RequireAuth>
        <p>Case dashboard</p>
      </RequireAuth>,
    );

    expect(screen.queryByText("Case dashboard")).not.toBeInTheDocument();
  });

  it("waits instead of redirecting while the session is still resolving", () => {
    // Redirecting here would bounce a signed-in user off every page reload,
    // before the refresh call has had a chance to restore the session.
    replace.mockClear();
    stillResolving();

    render(
      <RequireAuth>
        <p>Case dashboard</p>
      </RequireAuth>,
    );

    expect(replace).not.toHaveBeenCalled();
    expect(screen.queryByText("Case dashboard")).not.toBeInTheDocument();
    expect(screen.getByText("Restoring your session…")).toBeInTheDocument();
  });

  it("reveals content once a pending session resolves as authenticated", async () => {
    replace.mockClear();
    stillResolving();

    render(
      <RequireAuth>
        <p>Case dashboard</p>
      </RequireAuth>,
    );
    expect(screen.queryByText("Case dashboard")).not.toBeInTheDocument();

    signedIn();

    expect(await screen.findByText("Case dashboard")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it("redirects when an established session is lost (automatic logout)", async () => {
    replace.mockClear();
    signedIn();

    render(
      <RequireAuth>
        <p>Case dashboard</p>
      </RequireAuth>,
    );
    expect(screen.getByText("Case dashboard")).toBeInTheDocument();

    signedOut();

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
    expect(screen.queryByText("Case dashboard")).not.toBeInTheDocument();
  });
});

describe("RedirectIfAuthenticated", () => {
  it("shows the login form to a signed-out visitor", () => {
    replace.mockClear();
    signedOut();

    render(
      <RedirectIfAuthenticated>
        <p>Sign-in form</p>
      </RedirectIfAuthenticated>,
    );

    expect(screen.getByText("Sign-in form")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it("sends an authenticated user to the dashboard instead", async () => {
    replace.mockClear();
    signedIn();

    render(
      <RedirectIfAuthenticated>
        <p>Sign-in form</p>
      </RedirectIfAuthenticated>,
    );

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/dashboard"));
    expect(screen.queryByText("Sign-in form")).not.toBeInTheDocument();
  });

  it("shows the form immediately while the session is still resolving", () => {
    // The proxy already diverts cookie-holders away from /login, so blocking here
    // would only make anonymous visitors wait on a refresh call expected to fail.
    replace.mockClear();
    stillResolving();

    render(
      <RedirectIfAuthenticated>
        <p>Sign-in form</p>
      </RedirectIfAuthenticated>,
    );

    expect(screen.getByText("Sign-in form")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it("redirects if a session appears while the login page is open", async () => {
    replace.mockClear();
    stillResolving();

    render(
      <RedirectIfAuthenticated>
        <p>Sign-in form</p>
      </RedirectIfAuthenticated>,
    );
    expect(screen.getByText("Sign-in form")).toBeInTheDocument();

    signedIn();

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/dashboard"));
    expect(screen.queryByText("Sign-in form")).not.toBeInTheDocument();
  });
});

describe("safeRedirectTarget", () => {
  it.each(["/dashboard", "/cases", "/cases/123", "/documents?page=2"])(
    "accepts the same-origin path %s",
    (target) => {
      expect(safeRedirectTarget(target)).toBe(target);
    },
  );

  it.each([
    ["an absolute URL", "https://evil.example.com/steal"],
    ["a protocol-relative URL", "//evil.example.com"],
    ["a scheme in a path", "/javascript:alert(1)"],
    ["a backslash host", "/\\evil.example.com"],
    ["a bare host", "evil.example.com"],
  ])("rejects %s", (_label, target) => {
    expect(safeRedirectTarget(target)).toBeNull();
  });

  it.each([null, undefined, ""])("returns null for %s", (target) => {
    expect(safeRedirectTarget(target)).toBeNull();
  });

  it("refuses to bounce back to the login page", () => {
    expect(safeRedirectTarget("/login")).toBeNull();
    expect(safeRedirectTarget("/login?next=/cases")).toBeNull();
  });
});
