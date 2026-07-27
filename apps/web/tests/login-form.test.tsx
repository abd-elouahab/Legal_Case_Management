/**
 * Tests for the login form: validation, loading state, error state, and the
 * successful-login redirect.
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { LoginForm } from "@/components/auth/login-form";
import { getAccessToken } from "@/lib/api/token-store";
import { useSessionStore } from "@/stores/session-store";
import { errorEnvelope, mockFetch, TEST_SESSION_USER, tokenResponse } from "./helpers";

const replace = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn(), refresh: vi.fn() }),
}));

/**
 * Set the browser URL. The hook reads `?next=` from `window.location` at submit
 * time (rather than via `useSearchParams`, which would break prerendering), so
 * tests drive it through real history state.
 */
function setUrl(url: string) {
  window.history.replaceState({}, "", url);
}

function resetNavigation() {
  replace.mockClear();
  setUrl("/login");
}

async function fillAndSubmit(
  email: string,
  password: string,
  user = userEvent.setup(),
): Promise<void> {
  await user.type(screen.getByLabelText("Email"), email);
  await user.type(screen.getByLabelText("Password"), password);
  await user.click(screen.getByRole("button", { name: /sign in/i }));
}

describe("LoginForm rendering", () => {
  it("renders email, password, and a submit button", () => {
    resetNavigation();
    render(<LoginForm />);

    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sign in/i })).toBeEnabled();
  });

  it("masks the password by default", () => {
    resetNavigation();
    render(<LoginForm />);

    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
  });

  it("can reveal and re-hide the password", async () => {
    resetNavigation();
    const user = userEvent.setup();
    render(<LoginForm />);

    await user.click(screen.getByRole("button", { name: "Show password" }));
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "text");

    await user.click(screen.getByRole("button", { name: "Hide password" }));
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
  });

  it("uses autocomplete hints so password managers work", () => {
    resetNavigation();
    render(<LoginForm />);

    expect(screen.getByLabelText("Email")).toHaveAttribute("autocomplete", "username");
    expect(screen.getByLabelText("Password")).toHaveAttribute(
      "autocomplete",
      "current-password",
    );
  });
});

describe("LoginForm validation", () => {
  it("rejects an empty submission without calling the API", async () => {
    resetNavigation();
    const { fetchMock } = mockFetch({ "/auth/login": { body: tokenResponse() } });
    const user = userEvent.setup();
    render(<LoginForm />);

    await user.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByText("Email is required.")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a malformed email without calling the API", async () => {
    resetNavigation();
    const { fetchMock } = mockFetch({ "/auth/login": { body: tokenResponse() } });
    const user = userEvent.setup();
    render(<LoginForm />);

    await fillAndSubmit("not-an-email", "a-password", user);

    expect(await screen.findByText("Enter a valid email address.")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("requires a password", async () => {
    resetNavigation();
    mockFetch({ "/auth/login": { body: tokenResponse() } });
    const user = userEvent.setup();
    render(<LoginForm />);

    await user.type(screen.getByLabelText("Email"), "amina.benali@example.com");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByText("Password is required.")).toBeInTheDocument();
  });

  it("marks invalid fields for assistive technology", async () => {
    resetNavigation();
    mockFetch({ "/auth/login": { body: tokenResponse() } });
    const user = userEvent.setup();
    render(<LoginForm />);

    await fillAndSubmit("not-an-email", "a-password", user);

    await waitFor(() =>
      expect(screen.getByLabelText("Email")).toHaveAttribute("aria-invalid", "true"),
    );
  });

  it("normalizes the email before sending it", async () => {
    resetNavigation();
    const { requests } = mockFetch({ "/auth/login": { body: tokenResponse() } });
    const user = userEvent.setup();
    render(<LoginForm />);

    await fillAndSubmit("  AMINA.BENALI@Example.COM  ", "correct-horse-battery", user);

    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0]?.body).toMatchObject({ email: "amina.benali@example.com" });
  });
});

describe("LoginForm submission", () => {
  it("signs in, stores the session, and redirects to the dashboard", async () => {
    resetNavigation();
    mockFetch({ "/auth/login": { body: tokenResponse() } });
    const user = userEvent.setup();
    render(<LoginForm />);

    await fillAndSubmit("amina.benali@example.com", "correct-horse-battery", user);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/dashboard"));
    expect(useSessionStore.getState().user).toEqual(TEST_SESSION_USER);
    expect(useSessionStore.getState().status).toBe("authenticated");
    expect(getAccessToken()).toBe("access-token-1");
  });

  it("returns the user to the page they were intercepted on", async () => {
    resetNavigation();
    setUrl("/login?next=%2Fcases");
    mockFetch({ "/auth/login": { body: tokenResponse() } });
    const user = userEvent.setup();
    render(<LoginForm />);

    await fillAndSubmit("amina.benali@example.com", "correct-horse-battery", user);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/cases"));
  });

  it("ignores an off-site redirect target", async () => {
    // `?next=` is attacker-controllable, so an absolute URL must not be followed.
    resetNavigation();
    setUrl("/login?next=https%3A%2F%2Fevil.example.com%2Fsteal");
    mockFetch({ "/auth/login": { body: tokenResponse() } });
    const user = userEvent.setup();
    render(<LoginForm />);

    await fillAndSubmit("amina.benali@example.com", "correct-horse-battery", user);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/dashboard"));
  });

  it("disables the form while the request is in flight", async () => {
    resetNavigation();
    let release: (() => void) | undefined;
    const pending = new Promise<void>((resolve) => {
      release = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        await pending;
        return new Response(JSON.stringify(tokenResponse()), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );

    const user = userEvent.setup();
    render(<LoginForm />);
    await fillAndSubmit("amina.benali@example.com", "correct-horse-battery", user);

    expect(await screen.findByText("Signing in…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /signing in/i })).toBeDisabled();
    expect(screen.getByLabelText("Email")).toBeDisabled();

    release?.();
    await waitFor(() => expect(replace).toHaveBeenCalled());
  });
});

describe("LoginForm error states", () => {
  it("shows a generic message for invalid credentials", async () => {
    resetNavigation();
    mockFetch({
      "/auth/login": { status: 401, body: errorEnvelope("invalid_credentials") },
    });
    const user = userEvent.setup();
    render(<LoginForm />);

    await fillAndSubmit("amina.benali@example.com", "wrong-password", user);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Incorrect email or password.");
    // Must not hint which of the two was wrong.
    expect(alert).not.toHaveTextContent(/email.*not found|no such user|unknown/i);
    expect(replace).not.toHaveBeenCalled();
  });

  it("explains a disabled account", async () => {
    resetNavigation();
    mockFetch({ "/auth/login": { status: 403, body: errorEnvelope("account_disabled") } });
    const user = userEvent.setup();
    render(<LoginForm />);

    await fillAndSubmit("disabled@example.com", "correct-horse-battery", user);

    expect(await screen.findByRole("alert")).toHaveTextContent(/disabled/i);
  });

  it("reports an unreachable server", async () => {
    resetNavigation();
    mockFetch({ "/auth/login": { networkError: true } });
    const user = userEvent.setup();
    render(<LoginForm />);

    await fillAndSubmit("amina.benali@example.com", "correct-horse-battery", user);

    expect(await screen.findByRole("alert")).toHaveTextContent(/unable to reach the server/i);
  });

  it("shows the lockout message when throttled", async () => {
    resetNavigation();
    mockFetch({
      "/auth/login": {
        status: 429,
        body: errorEnvelope(
          "too_many_login_attempts",
          "Too many failed sign-in attempts. Try again in about 15 minutes.",
        ),
      },
    });
    const user = userEvent.setup();
    render(<LoginForm />);

    await fillAndSubmit("amina.benali@example.com", "wrong-password", user);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/too many failed sign-in attempts/i);
    // The server states the wait; the client must not invent its own number.
    expect(alert).toHaveTextContent(/15 minutes/);
  });

  it("does not redirect or create a session when throttled", async () => {
    resetNavigation();
    mockFetch({
      "/auth/login": { status: 429, body: errorEnvelope("too_many_login_attempts") },
    });
    const user = userEvent.setup();
    render(<LoginForm />);

    await fillAndSubmit("amina.benali@example.com", "wrong-password", user);
    await screen.findByRole("alert");

    expect(replace).not.toHaveBeenCalled();
    expect(useSessionStore.getState().status).not.toBe("authenticated");
  });

  it("lets the user try again after the lockout message", async () => {
    // The form must not latch into a dead state; the lockout is time-based.
    resetNavigation();
    mockFetch({
      "/auth/login": { status: 429, body: errorEnvelope("too_many_login_attempts") },
    });
    const user = userEvent.setup();
    render(<LoginForm />);

    await fillAndSubmit("amina.benali@example.com", "wrong-password", user);
    await screen.findByRole("alert");

    expect(screen.getByRole("button", { name: /sign in/i })).toBeEnabled();
  });

  it("re-enables the form after a failure so the user can retry", async () => {
    resetNavigation();
    mockFetch({ "/auth/login": { status: 401, body: errorEnvelope("invalid_credentials") } });
    const user = userEvent.setup();
    render(<LoginForm />);

    await fillAndSubmit("amina.benali@example.com", "wrong-password", user);
    await screen.findByRole("alert");

    expect(screen.getByRole("button", { name: /sign in/i })).toBeEnabled();
    expect(screen.getByLabelText("Email")).toBeEnabled();
  });

  it("clears the error once the user edits the form", async () => {
    resetNavigation();
    mockFetch({ "/auth/login": { status: 401, body: errorEnvelope("invalid_credentials") } });
    const user = userEvent.setup();
    render(<LoginForm />);

    await fillAndSubmit("amina.benali@example.com", "wrong-password", user);
    await screen.findByRole("alert");

    await user.type(screen.getByLabelText("Password"), "x");

    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });

  it("leaves the session signed out after a failed login", async () => {
    resetNavigation();
    mockFetch({ "/auth/login": { status: 401, body: errorEnvelope("invalid_credentials") } });
    const user = userEvent.setup();
    render(<LoginForm />);

    await fillAndSubmit("amina.benali@example.com", "wrong-password", user);
    await screen.findByRole("alert");

    expect(useSessionStore.getState().user).toBeNull();
    expect(getAccessToken()).toBeNull();
  });
});
