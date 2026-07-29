/**
 * Tests for User Management.
 *
 * Cover the whole client surface the spec asks for: creating, editing, and
 * deactivating a user, searching, filtering, sorting, paginating, resetting a
 * password, and what an unauthorized role is allowed to see.
 *
 * These verify what the administrator is *shown* and what the client *sends*.
 * The API is the real boundary — its 401/403 and CRUD behaviour is covered by
 * `tests/integration/test_users.py`.
 */

import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { RouteGuard } from "@/components/auth/route-guard";
import { CreateUserDialog } from "@/components/users/create-user-dialog";
import { DeactivateUserDialog } from "@/components/users/deactivate-user-dialog";
import { EditUserDialog } from "@/components/users/edit-user-dialog";
import { ResetPasswordDialog } from "@/components/users/reset-password-dialog";
import { UserDirectory } from "@/components/users/user-directory";
import { accessRuleForPath } from "@/lib/authorization/routes";
import { buildUserListParams } from "@/lib/api/users";
import { createUserFormSchema, editUserFormSchema } from "@/lib/validation/user";
import { ROUTES, userRoute } from "@/lib/routes";
import { useSessionStore } from "@/stores/session-store";
import { PERMISSION } from "@/types/authorization";
import { DEFAULT_USER_LIST_QUERY } from "@/types/user-management";
import type { ManagedUser, UserRole } from "@/types/user";
import {
  errorEnvelope,
  managedUserPayload,
  mockFetch,
  sessionUserWithRole,
  userPagePayload,
} from "./helpers";

let pathname = ROUTES.users;

vi.mock("next/navigation", () => ({
  usePathname: () => pathname,
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn() }),
}));

// `sonner` renders through a portal and a global store; the tests assert on the
// requests and the dialog state, not on toast text.
vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  }),
}));

function signInAs(role: UserRole) {
  act(() => {
    useSessionStore.setState({ user: sessionUserWithRole(role), status: "authenticated" });
  });
}

/** Render inside a fresh QueryClient so no cache leaks between tests. */
function renderWithQuery(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });

  return {
    queryClient,
    ...render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>),
  };
}

/** A `ManagedUser` in the app's domain shape, for the dialogs that take one. */
function managedUser(overrides: Partial<ManagedUser> = {}): ManagedUser {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    email: "karim.zahra@example.com",
    firstName: "Karim",
    lastName: "Zahra",
    fullName: "Karim Zahra",
    phone: "+212 612345678",
    profileImage: null,
    role: "lawyer",
    status: "active",
    isActive: true,
    mustChangePassword: false,
    lastLoginAt: "2026-07-20T08:30:00Z",
    createdAt: "2026-07-01T09:00:00Z",
    updatedAt: "2026-07-20T08:30:00Z",
    createdBy: null,
    updatedBy: null,
    permissions: [],
    ...overrides,
  };
}

/** Requests the client actually made to `/users`, in order. */
function userRequests(requests: Array<{ url: string; method: string; body: unknown }>) {
  return requests.filter((request) => request.url.includes("/users"));
}

// --------------------------------------------------------------------------- //
// Query building
// --------------------------------------------------------------------------- //

describe("buildUserListParams", () => {
  it("always sends page, size, and sort", () => {
    const params = new URLSearchParams(buildUserListParams(DEFAULT_USER_LIST_QUERY));

    expect(params.get("page")).toBe("1");
    expect(params.get("page_size")).toBe("20");
    expect(params.get("sort_by")).toBe("created_at");
    expect(params.get("sort_order")).toBe("desc");
  });

  it("omits an empty search and unset filters", () => {
    // Sending blanks would make the request URL — and therefore the cache key —
    // claim a filter that is not applied.
    const params = new URLSearchParams(buildUserListParams(DEFAULT_USER_LIST_QUERY));

    expect(params.has("search")).toBe(false);
    expect(params.has("role")).toBe(false);
    expect(params.has("status")).toBe(false);
  });

  it("includes a trimmed search and the active filters", () => {
    const params = new URLSearchParams(
      buildUserListParams({
        ...DEFAULT_USER_LIST_QUERY,
        search: "  zahra  ",
        role: "lawyer",
        status: "suspended",
      }),
    );

    expect(params.get("search")).toBe("zahra");
    expect(params.get("role")).toBe("lawyer");
    expect(params.get("status")).toBe("suspended");
  });
});

// --------------------------------------------------------------------------- //
// Form validation
// --------------------------------------------------------------------------- //

describe("user form validation", () => {
  const valid = {
    firstName: "Amina",
    lastName: "Benali",
    email: "Amina.Benali@Example.com",
    phone: "+212 612345678",
    password: "correct-horse-battery",
    role: "lawyer" as const,
    status: "active" as const,
    mustChangePassword: false,
  };

  it("normalizes the email and names", () => {
    const result = createUserFormSchema.parse({
      ...valid,
      firstName: "  Amina  ",
      lastName: "Ben   Salah",
    });

    expect(result.email).toBe("amina.benali@example.com");
    expect(result.firstName).toBe("Amina");
    expect(result.lastName).toBe("Ben Salah");
  });

  it.each(["firstName", "lastName"] as const)("rejects a blank %s", (field) => {
    expect(createUserFormSchema.safeParse({ ...valid, [field]: "   " }).success).toBe(false);
  });

  it("rejects a malformed email", () => {
    expect(createUserFormSchema.safeParse({ ...valid, email: "nope" }).success).toBe(false);
  });

  it("rejects a password shorter than the policy", () => {
    expect(createUserFormSchema.safeParse({ ...valid, password: "short" }).success).toBe(false);
  });

  it("accepts an empty phone as 'not provided'", () => {
    expect(createUserFormSchema.parse({ ...valid, phone: "" }).phone).toBe("");
  });

  it("rejects a phone that is not a phone number", () => {
    const result = createUserFormSchema.safeParse({ ...valid, phone: "call me" });

    expect(result.success).toBe(false);
  });

  it("rejects a phone with too few digits", () => {
    expect(createUserFormSchema.safeParse({ ...valid, phone: "12345" }).success).toBe(false);
  });

  it("the edit form has no password field", () => {
    // Changing a password revokes sessions, so it never rides along on an edit.
    expect(Object.keys(editUserFormSchema.shape)).not.toContain("password");
  });
});

// --------------------------------------------------------------------------- //
// Authorization
// --------------------------------------------------------------------------- //

describe("user management authorization", () => {
  it("requires a session for /users and everything under it", async () => {
    // The proxy is a pre-filter, not the boundary — but a route missing from it
    // silently ships the app shell to an anonymous visitor, so it fails *open*.
    // Deriving the list from ROUTES is what makes forgetting one impossible.
    const { proxy } = await import("@/proxy");
    const { NextRequest } = await import("next/server");

    for (const path of [ROUTES.users, userRoute("abc")]) {
      const response = proxy(new NextRequest(new URL(path, "http://localhost:3000")));
      expect(response?.status).toBe(307);
      expect(response?.headers.get("location")).toContain("/login");
    }
  });

  it("protects every route in ROUTES except the public ones", async () => {
    const { proxy } = await import("@/proxy");
    const { NextRequest } = await import("next/server");

    const publicRoutes: string[] = [ROUTES.home, ROUTES.login];
    for (const route of Object.values(ROUTES)) {
      if (publicRoutes.includes(route)) continue;
      const response = proxy(new NextRequest(new URL(route, "http://localhost:3000")));
      expect(response?.headers.get("location"), `${route} is unprotected`).toContain("/login");
    }
  });

  it("gates /users on users:view", () => {
    expect(accessRuleForPath(ROUTES.users)).toEqual({ permission: PERMISSION.usersView });
  });

  it("gates a user's details page through the same rule", () => {
    // Longest-prefix matching, so a new page under /users needs no declaration.
    expect(accessRuleForPath(userRoute("abc"))).toEqual({ permission: PERMISSION.usersView });
  });

  it.each(["lawyer", "court"] as const)("shows %s the Unauthorized page", (role) => {
    pathname = ROUTES.users;
    signInAs(role);

    renderWithQuery(
      <RouteGuard>
        <p>User directory</p>
      </RouteGuard>,
    );

    expect(screen.getByText("Access denied")).toBeInTheDocument();
    expect(screen.queryByText("User directory")).not.toBeInTheDocument();
  });

  it("lets an administrator through", () => {
    pathname = ROUTES.users;
    signInAs("administrator");

    renderWithQuery(
      <RouteGuard>
        <p>User directory</p>
      </RouteGuard>,
    );

    expect(screen.getByText("User directory")).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Directory: listing, search, filters, sorting, pagination
// --------------------------------------------------------------------------- //

describe("UserDirectory", () => {
  it("renders a row per user with the documented columns", async () => {
    signInAs("administrator");
    mockFetch({ "/users": { body: userPagePayload() } });

    renderWithQuery(<UserDirectory />);

    expect(await screen.findByText("Karim Zahra")).toBeInTheDocument();
    expect(screen.getByText("karim.zahra@example.com")).toBeInTheDocument();
    expect(screen.getByText("Lawyer")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("shows a skeleton while the first page loads", () => {
    signInAs("administrator");
    mockFetch({ "/users": { body: userPagePayload() } });

    renderWithQuery(<UserDirectory />);

    expect(screen.getByLabelText("Loading users")).toBeInTheDocument();
  });

  it("shows an empty state with a call to action when there are no users", async () => {
    signInAs("administrator");
    mockFetch({ "/users": { body: userPagePayload([], { total_records: 0 }) } });

    renderWithQuery(<UserDirectory />);

    expect(await screen.findByText("No users yet")).toBeInTheDocument();
  });

  it("shows an error state with a retry when the request fails", async () => {
    signInAs("administrator");
    mockFetch({ "/users": { status: 500, body: errorEnvelope("internal_error") } });

    renderWithQuery(<UserDirectory />);

    expect(await screen.findByText("Could not load users")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("searches, and resets to the first page when it does", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockFetch({ "/users": { body: userPagePayload() } });

    renderWithQuery(<UserDirectory />);
    await screen.findByText("Karim Zahra");

    await user.type(screen.getByLabelText("Search"), "zahra");

    await waitFor(() => {
      const last = userRequests(requests).at(-1);
      expect(last?.url).toContain("search=zahra");
      expect(last?.url).toContain("page=1");
    });
  });

  it("debounces typing into a single request", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockFetch({ "/users": { body: userPagePayload() } });

    renderWithQuery(<UserDirectory />);
    await screen.findByText("Karim Zahra");
    const before = userRequests(requests).length;

    await user.type(screen.getByLabelText("Search"), "zahra");
    await waitFor(() => {
      expect(userRequests(requests).at(-1)?.url).toContain("search=zahra");
    });

    // One request for the settled term, not one per keystroke.
    expect(userRequests(requests).length - before).toBeLessThanOrEqual(2);
  });

  it("shows a distinct empty state when a search matches nothing", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    mockFetch({
      "/users": [
        { body: userPagePayload() },
        { body: userPagePayload([], { total_records: 0 }) },
      ],
    });

    renderWithQuery(<UserDirectory />);
    await screen.findByText("Karim Zahra");

    await user.type(screen.getByLabelText("Search"), "nobody");

    // A fruitless search offers a way back; an empty directory offers a way in.
    expect(await screen.findByText("No users match your search")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("filters by role", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockFetch({ "/users": { body: userPagePayload() } });

    renderWithQuery(<UserDirectory />);
    await screen.findByText("Karim Zahra");

    await user.click(screen.getByLabelText("Role"));
    await user.click(await screen.findByRole("option", { name: "Lawyer" }));

    await waitFor(() => {
      expect(userRequests(requests).at(-1)?.url).toContain("role=lawyer");
    });
  });

  it("filters by status, combining with the role filter", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockFetch({ "/users": { body: userPagePayload() } });

    renderWithQuery(<UserDirectory />);
    await screen.findByText("Karim Zahra");

    await user.click(screen.getByLabelText("Role"));
    await user.click(await screen.findByRole("option", { name: "Lawyer" }));
    await waitFor(() => expect(userRequests(requests).at(-1)?.url).toContain("role=lawyer"));

    await user.click(screen.getByLabelText("Status"));
    await user.click(await screen.findByRole("option", { name: "Suspended" }));

    await waitFor(() => {
      const url = userRequests(requests).at(-1)?.url ?? "";
      expect(url).toContain("role=lawyer");
      expect(url).toContain("status=suspended");
    });
  });

  it("sorts by a column, and reverses it on a second activation", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockFetch({ "/users": { body: userPagePayload() } });

    renderWithQuery(<UserDirectory />);
    await screen.findByText("Karim Zahra");

    await user.click(screen.getByRole("button", { name: /Sort by Name/ }));
    await waitFor(() => {
      const url = userRequests(requests).at(-1)?.url ?? "";
      expect(url).toContain("sort_by=name");
      expect(url).toContain("sort_order=asc");
    });

    await user.click(screen.getByRole("button", { name: /Sorted ascending/ }));
    await waitFor(() => {
      expect(userRequests(requests).at(-1)?.url).toContain("sort_order=desc");
    });
  });

  it("announces the sorted column to assistive technology", async () => {
    signInAs("administrator");
    mockFetch({ "/users": { body: userPagePayload() } });

    renderWithQuery(<UserDirectory />);
    await screen.findByText("Karim Zahra");

    const nameHeader = screen.getByRole("columnheader", { name: /Name/ });
    expect(nameHeader).toHaveAttribute("aria-sort", "none");
  });

  it("paginates and reports the record range", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const page = userPagePayload([managedUserPayload()], {
      total_records: 41,
      total_pages: 3,
    });
    const { requests } = mockFetch({ "/users": { body: page } });

    renderWithQuery(<UserDirectory />);
    await screen.findByText("Karim Zahra");

    // The range comes from the page size and the total, not from the number of
    // rows returned — which is what makes it correct on a partially-full page.
    expect(screen.getByText("Showing 1–20 of 41 users")).toBeInTheDocument();
    expect(screen.getByText("Page 1 of 3")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Previous/ })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: /Next/ }));

    await waitFor(() => {
      expect(userRequests(requests).at(-1)?.url).toContain("page=2");
    });
  });

  it("disables Next on the last page", async () => {
    signInAs("administrator");
    mockFetch({ "/users": { body: userPagePayload() } });

    renderWithQuery(<UserDirectory />);
    await screen.findByText("Karim Zahra");

    expect(screen.getByRole("button", { name: /Next/ })).toBeDisabled();
  });

  it("offers Add user to an administrator", async () => {
    signInAs("administrator");
    mockFetch({ "/users": { body: userPagePayload() } });

    renderWithQuery(<UserDirectory />);
    await screen.findByText("Karim Zahra");

    expect(screen.getByRole("button", { name: /Add user/ })).toBeInTheDocument();
  });

  it("hides row actions the caller lacks the permission for", async () => {
    const user = userEvent.setup();
    // A signed-in user holding users:view but not users:create/update/delete —
    // the permission list, not the role name, is what the UI reads.
    act(() => {
      useSessionStore.setState({
        user: { ...sessionUserWithRole("administrator"), permissions: ["users:view"] },
        status: "authenticated",
      });
    });
    mockFetch({ "/users": { body: userPagePayload() } });

    renderWithQuery(<UserDirectory />);
    await screen.findByText("Karim Zahra");

    expect(screen.queryByRole("button", { name: /Add user/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Actions for Karim Zahra" }));

    expect(await screen.findByRole("menuitem", { name: /View details/ })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /^Edit/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Deactivate/ })).not.toBeInTheDocument();
  });

  it("does not offer to deactivate the signed-in administrator's own row", async () => {
    const user = userEvent.setup();
    const me = sessionUserWithRole("administrator");
    act(() => {
      useSessionStore.setState({ user: me, status: "authenticated" });
    });
    mockFetch({
      "/users": {
        body: userPagePayload([
          managedUserPayload({ id: me.id, full_name: "Amina Benali", role: "administrator" }),
        ]),
      },
    });

    renderWithQuery(<UserDirectory />);
    await screen.findByText("Amina Benali");

    await user.click(screen.getByRole("button", { name: "Actions for Amina Benali" }));

    // The API refuses it, so offering it would only produce a failure.
    expect(screen.queryByRole("menuitem", { name: /Deactivate/ })).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Create
// --------------------------------------------------------------------------- //

describe("CreateUserDialog", () => {
  async function fillValidForm(user: ReturnType<typeof userEvent.setup>) {
    await user.type(screen.getByLabelText("First name"), "Amina");
    await user.type(screen.getByLabelText("Last name"), "Benali");
    await user.type(screen.getByLabelText("Email"), "amina.benali@example.com");
    await user.type(screen.getByLabelText("Password"), "correct-horse-battery");
  }

  it("creates a user and closes", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const onOpenChange = vi.fn();
    const { requests } = mockFetch({
      "/users": { status: 201, body: managedUserPayload({ full_name: "Amina Benali" }) },
    });

    renderWithQuery(<CreateUserDialog open onOpenChange={onOpenChange} />);
    await fillValidForm(user);
    await user.click(screen.getByRole("button", { name: /Create user/ }));

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));

    const request = userRequests(requests).at(-1);
    expect(request?.method).toBe("POST");
    expect(request?.body).toMatchObject({
      email: "amina.benali@example.com",
      first_name: "Amina",
      last_name: "Benali",
      password: "correct-horse-battery",
      role: "lawyer",
      status: "active",
    });
  });

  it("does not call the API when the form is invalid", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockFetch({ "/users": { status: 201, body: managedUserPayload() } });

    renderWithQuery(<CreateUserDialog open onOpenChange={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: /Create user/ }));

    expect(await screen.findByText("First name is required.")).toBeInTheDocument();
    expect(userRequests(requests)).toHaveLength(0);
  });

  it("shows a field-level message for a malformed email", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    mockFetch({ "/users": { status: 201, body: managedUserPayload() } });

    renderWithQuery(<CreateUserDialog open onOpenChange={vi.fn()} />);
    await user.type(screen.getByLabelText("Email"), "not-an-email");
    await user.click(screen.getByRole("button", { name: /Create user/ }));

    expect(await screen.findByText("Enter a valid email address.")).toBeInTheDocument();
  });

  it("reports a duplicate email from the server", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const onOpenChange = vi.fn();
    mockFetch({
      "/users": {
        status: 409,
        body: errorEnvelope("email_already_exists", "Already exists."),
      },
    });

    renderWithQuery(<CreateUserDialog open onOpenChange={onOpenChange} />);
    await fillValidForm(user);
    await user.click(screen.getByRole("button", { name: /Create user/ }));

    expect(
      await screen.findByText("A user with this email address already exists."),
    ).toBeInTheDocument();
    // The dialog stays open so the administrator can correct the email.
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("attaches a server validation error to its field", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    mockFetch({
      "/users": {
        status: 422,
        body: errorEnvelope("validation_error", "Request validation failed.", [
          { field: "phone", message: "Phone number must contain between 7 and 15 digits." },
        ]),
      },
    });

    renderWithQuery(<CreateUserDialog open onOpenChange={vi.fn()} />);
    await fillValidForm(user);
    await user.click(screen.getByRole("button", { name: /Create user/ }));

    // Next to the phone input, where the client-side rule would have complained.
    expect(
      await screen.findByText("Phone number must contain between 7 and 15 digits."),
    ).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Edit
// --------------------------------------------------------------------------- //

describe("EditUserDialog", () => {
  it("pre-fills the form with the user's current values", () => {
    signInAs("administrator");
    mockFetch({ "/users": { body: managedUserPayload() } });

    renderWithQuery(<EditUserDialog user={managedUser()} open onOpenChange={vi.fn()} />);

    expect(screen.getByLabelText("First name")).toHaveValue("Karim");
    expect(screen.getByLabelText("Email")).toHaveValue("karim.zahra@example.com");
    expect(screen.getByLabelText("Phone")).toHaveValue("+212 612345678");
  });

  it("has no password field", () => {
    signInAs("administrator");
    mockFetch({ "/users": { body: managedUserPayload() } });

    renderWithQuery(<EditUserDialog user={managedUser()} open onOpenChange={vi.fn()} />);

    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
  });

  it("sends only the fields that changed", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockFetch({
      "/users": { body: managedUserPayload({ first_name: "Yasmine" }) },
    });

    renderWithQuery(<EditUserDialog user={managedUser()} open onOpenChange={vi.fn()} />);

    await user.clear(screen.getByLabelText("First name"));
    await user.type(screen.getByLabelText("First name"), "Yasmine");
    await user.click(screen.getByRole("button", { name: /Save changes/ }));

    await waitFor(() => expect(userRequests(requests).length).toBeGreaterThan(0));

    const request = userRequests(requests).at(-1);
    expect(request?.method).toBe("PATCH");
    // A full echo would overwrite a concurrent edit by another administrator.
    expect(request?.body).toEqual({ first_name: "Yasmine" });
  });

  it("clears the phone with an explicit null", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockFetch({ "/users": { body: managedUserPayload({ phone: null }) } });

    renderWithQuery(<EditUserDialog user={managedUser()} open onOpenChange={vi.fn()} />);

    await user.clear(screen.getByLabelText("Phone"));
    await user.click(screen.getByRole("button", { name: /Save changes/ }));

    await waitFor(() => expect(userRequests(requests).length).toBeGreaterThan(0));
    // null clears it; omitting the key would have left it alone.
    expect(userRequests(requests).at(-1)?.body).toEqual({ phone: null });
  });

  it("does not call the API when nothing changed", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const onOpenChange = vi.fn();
    const { requests } = mockFetch({ "/users": { body: managedUserPayload() } });

    renderWithQuery(<EditUserDialog user={managedUser()} open onOpenChange={onOpenChange} />);
    await user.click(screen.getByRole("button", { name: /Save changes/ }));

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    // The API rejects an empty PATCH, and there is genuinely nothing to save.
    expect(userRequests(requests)).toHaveLength(0);
  });

  it("surfaces a refused self-modification", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    mockFetch({
      "/users": { status: 400, body: errorEnvelope("cannot_modify_own_account") },
    });

    renderWithQuery(<EditUserDialog user={managedUser()} open onOpenChange={vi.fn()} />);
    await user.clear(screen.getByLabelText("First name"));
    await user.type(screen.getByLabelText("First name"), "Yasmine");
    await user.click(screen.getByRole("button", { name: /Save changes/ }));

    expect(
      await screen.findByText("You cannot change your own role or account status."),
    ).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Deactivate
// --------------------------------------------------------------------------- //

describe("DeactivateUserDialog", () => {
  it("explains that the account is kept, not deleted", () => {
    signInAs("administrator");
    mockFetch({ "/users": { body: managedUserPayload() } });

    renderWithQuery(<DeactivateUserDialog user={managedUser()} open onOpenChange={vi.fn()} />);

    expect(screen.getByText(/Deactivate Karim Zahra\?/)).toBeInTheDocument();
    // "Delete" would be a misleading word for a soft delete; say so up front.
    expect(screen.getByText(/kept — not deleted/)).toBeInTheDocument();
    expect(screen.getByText(/signed out of every device/)).toBeInTheDocument();
  });

  it("sends a DELETE on confirmation", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const onOpenChange = vi.fn();
    const { requests } = mockFetch({
      "/users": { body: managedUserPayload({ status: "inactive", is_active: false }) },
    });

    renderWithQuery(
      <DeactivateUserDialog user={managedUser()} open onOpenChange={onOpenChange} />,
    );
    await user.click(screen.getByRole("button", { name: "Deactivate" }));

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));

    const request = userRequests(requests).at(-1);
    expect(request?.method).toBe("DELETE");
    expect(request?.url).toContain(managedUser().id);
  });

  it("does nothing when cancelled", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockFetch({ "/users": { body: managedUserPayload() } });

    renderWithQuery(<DeactivateUserDialog user={managedUser()} open onOpenChange={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(userRequests(requests)).toHaveLength(0);
  });

  it("keeps the dialog open and reports a failure", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const onOpenChange = vi.fn();
    mockFetch({ "/users": { status: 404, body: errorEnvelope("user_not_found") } });

    renderWithQuery(
      <DeactivateUserDialog user={managedUser()} open onOpenChange={onOpenChange} />,
    );
    await user.click(screen.getByRole("button", { name: "Deactivate" }));

    expect(
      await screen.findByText("This user no longer exists. Refresh the list and try again."),
    ).toBeInTheDocument();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });
});

// --------------------------------------------------------------------------- //
// Reset password
// --------------------------------------------------------------------------- //

describe("ResetPasswordDialog", () => {
  it("confirms before resetting", async () => {
    signInAs("administrator");
    mockFetch({ "/users": { body: managedUserPayload() } });

    renderWithQuery(<ResetPasswordDialog user={managedUser()} open onOpenChange={vi.fn()} />);

    expect(screen.getByText(/Reset password for Karim Zahra\?/)).toBeInTheDocument();
    expect(screen.getByText(/signed out of every device/)).toBeInTheDocument();
  });

  it("reveals the temporary password and keeps it on screen", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const onOpenChange = vi.fn();
    mockFetch({
      "/users": {
        body: {
          user: managedUserPayload({ must_change_password: true }),
          temporary_password: "Xy7-temp-Password",
          must_change_password: true,
          message: "Temporary password generated.",
        },
      },
    });

    renderWithQuery(
      <ResetPasswordDialog user={managedUser()} open onOpenChange={onOpenChange} />,
    );
    await user.click(screen.getByRole("button", { name: /Reset password/ }));

    // The API shows it once; closing the dialog without it means another reset.
    expect(await screen.findByText("Xy7-temp-Password")).toBeInTheDocument();
    expect(screen.getByText(/not shown again/)).toBeInTheDocument();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("posts to the reset endpoint", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockFetch({
      "/users": {
        body: {
          user: managedUserPayload(),
          temporary_password: "Xy7-temp-Password",
          must_change_password: true,
          message: "Temporary password generated.",
        },
      },
    });

    renderWithQuery(<ResetPasswordDialog user={managedUser()} open onOpenChange={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: /Reset password/ }));

    await waitFor(() => expect(userRequests(requests).length).toBeGreaterThan(0));
    const request = userRequests(requests).at(-1);
    expect(request?.method).toBe("POST");
    expect(request?.url).toContain("/reset-password");
  });

  it("reports a failure without revealing anything", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    mockFetch({ "/users": { status: 403, body: errorEnvelope("forbidden") } });

    renderWithQuery(<ResetPasswordDialog user={managedUser()} open onOpenChange={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: /Reset password/ }));

    expect(
      await screen.findByText("You do not have permission to perform this action."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Done" })).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Row actions wiring
// --------------------------------------------------------------------------- //

describe("row actions", () => {
  it("links to the user's details page", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    mockFetch({ "/users": { body: userPagePayload() } });

    renderWithQuery(<UserDirectory />);
    await screen.findByText("Karim Zahra");

    await user.click(screen.getByRole("button", { name: "Actions for Karim Zahra" }));

    const link = await screen.findByRole("menuitem", { name: /View details/ });
    expect(link).toHaveAttribute("href", userRoute(managedUser().id));
  });

  it("offers Activate rather than Deactivate for a disabled account", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    mockFetch({
      "/users": {
        body: userPagePayload([
          managedUserPayload({ status: "inactive", is_active: false }),
        ]),
      },
    });

    renderWithQuery(<UserDirectory />);
    await screen.findByText("Karim Zahra");

    await user.click(screen.getByRole("button", { name: "Actions for Karim Zahra" }));

    expect(await screen.findByRole("menuitem", { name: /Activate/ })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Deactivate/ })).not.toBeInTheDocument();
  });

  it("reactivates through a PATCH", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockFetch({
      "/users": [
        {
          body: userPagePayload([managedUserPayload({ status: "inactive", is_active: false })]),
        },
        { body: managedUserPayload() },
      ],
    });

    renderWithQuery(<UserDirectory />);
    await screen.findByText("Karim Zahra");

    await user.click(screen.getByRole("button", { name: "Actions for Karim Zahra" }));
    await user.click(await screen.findByRole("menuitem", { name: /Activate/ }));

    await waitFor(() => {
      const patch = userRequests(requests).find((request) => request.method === "PATCH");
      expect(patch?.body).toEqual({ status: "active" });
    });
  });

  it("opens the edit dialog on the chosen row", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    mockFetch({ "/users": { body: userPagePayload() } });

    renderWithQuery(<UserDirectory />);
    await screen.findByText("Karim Zahra");

    await user.click(screen.getByRole("button", { name: "Actions for Karim Zahra" }));
    await user.click(await screen.findByRole("menuitem", { name: /^Edit/ }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Edit user")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("First name")).toHaveValue("Karim");
  });
});
