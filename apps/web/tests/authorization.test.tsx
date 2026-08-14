/**
 * Tests for client-side authorization: rule evaluation, the permission and role
 * hooks, the `Protected` component, `ProtectedRoute` / `RouteGuard`, and
 * role-aware navigation.
 *
 * These verify what the user is *shown*. The API is the actual boundary — its
 * 401/403 behaviour is covered by `tests/integration/test_authorization.py`.
 */

import { describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";

import { Protected } from "@/components/auth/protected";
import { ProtectedRoute } from "@/components/auth/protected-route";
import { RouteGuard } from "@/components/auth/route-guard";
import { SidebarNav } from "@/components/layout/sidebar-nav";
import { navItems, routeAccessRules, sidebarNavigation } from "@/config/navigation";
import { usePermissions } from "@/hooks/use-permissions";
import { useRole } from "@/hooks/use-role";
import {
  hasAllPermissions,
  hasAnyPermission,
  hasPermission,
  hasRole,
  isAllowed,
} from "@/lib/authorization/access";
import { accessRuleForPath } from "@/lib/authorization/routes";
import messages from "@/messages/en.json";
import { useSessionStore } from "@/stores/session-store";
import { PERMISSION, PERMISSIONS } from "@/types/authorization";
import type { UserRole } from "@/types/user";
import { ROLE_PERMISSIONS, sessionUserWithRole } from "./helpers";

/**
 * The label the sidebar renders for a navigation key.
 *
 * Resolved from the shipped catalogue rather than from the config, because the
 * config holds a **key**: asserting on `titleKey` would assert that the
 * navigation matches itself, while this asserts on the words a user reads.
 */
function navLabel(key: string): string {
  return (messages.navigation.items as Record<string, string>)[key];
}

let pathname = "/dashboard";

vi.mock("next/navigation", () => ({
  usePathname: () => pathname,
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn() }),
}));

function signInAs(role: UserRole) {
  act(() => {
    useSessionStore.setState({ user: sessionUserWithRole(role), status: "authenticated" });
  });
}

function stillResolving() {
  act(() => {
    useSessionStore.setState({ user: null, status: "loading" });
  });
}

function signedOut() {
  act(() => {
    useSessionStore.setState({ user: null, status: "unauthenticated" });
  });
}

/** Renders a hook's result so it can be asserted on. */
function HookProbe({ render: renderResult }: { render: () => string }) {
  return <span>{renderResult()}</span>;
}

function probe(renderResult: () => string): string {
  // Unmounted immediately so a test may probe more than once without the
  // previous render still being in the document.
  const { container, unmount } = render(<HookProbe render={renderResult} />);
  const text = container.textContent ?? "";
  unmount();
  return text;
}

// --------------------------------------------------------------------------- //
// Rule evaluation
// --------------------------------------------------------------------------- //

describe("access rule evaluation", () => {
  const lawyer = { role: "lawyer" as const, permissions: ROLE_PERMISSIONS.lawyer };

  it("checks a single permission", () => {
    expect(hasPermission(lawyer.permissions, PERMISSION.casesView)).toBe(true);
    expect(hasPermission(lawyer.permissions, PERMISSION.usersView)).toBe(false);
  });

  it("checks any-of and all-of", () => {
    expect(hasAnyPermission(lawyer.permissions, [PERMISSION.usersView, PERMISSION.aiChat])).toBe(true);
    expect(hasAnyPermission(lawyer.permissions, [PERMISSION.usersView])).toBe(false);
    expect(hasAllPermissions(lawyer.permissions, [PERMISSION.aiChat, PERMISSION.casesView])).toBe(true);
    expect(hasAllPermissions(lawyer.permissions, [PERMISSION.aiChat, PERMISSION.usersView])).toBe(false);
  });

  it("checks roles", () => {
    expect(hasRole("lawyer", ["lawyer", "administrator"])).toBe(true);
    expect(hasRole("lawyer", ["administrator"])).toBe(false);
  });

  it("treats an empty rule as satisfied", () => {
    // "Any authenticated user" — the subject has already been resolved to a
    // real session by the time a rule is evaluated.
    expect(isAllowed({}, lawyer)).toBe(true);
  });

  it("requires every clause present to pass", () => {
    expect(isAllowed({ permission: PERMISSION.casesView, roles: ["lawyer"] }, lawyer)).toBe(true);
    // The permission is held but the role does not match.
    expect(isAllowed({ permission: PERMISSION.casesView, roles: ["administrator"] }, lawyer)).toBe(
      false,
    );
  });

  it("denies an empty anyOf or allOf rather than waving it through", () => {
    // An empty requirement almost always means it was built dynamically and came
    // out empty; granting access would be the wrong reading of a bug.
    expect(isAllowed({ anyOf: [] }, lawyer)).toBe(false);
    expect(isAllowed({ allOf: [] }, lawyer)).toBe(false);
  });
});

// --------------------------------------------------------------------------- //
// Hooks
// --------------------------------------------------------------------------- //

describe("usePermissions", () => {
  it("exposes the permissions the API delivered with the session", () => {
    signInAs("lawyer");

    expect(probe(() => usePermissions().permissions.join(","))).toBe(
      ROLE_PERMISSIONS.lawyer.join(","),
    );
  });

  it("grants an administrator every permission", () => {
    signInAs("administrator");

    expect(probe(() => String(usePermissions().canAll(PERMISSIONS)))).toBe("true");
  });

  it("refuses a lawyer an administrative permission", () => {
    signInAs("lawyer");

    expect(probe(() => String(usePermissions().can(PERMISSION.usersView)))).toBe("false");
  });

  it("refuses a court representative reporting and AI", () => {
    signInAs("court");

    expect(
      probe(() =>
        String(usePermissions().canAny([PERMISSION.reportsView, PERMISSION.aiChat])),
      ),
    ).toBe("false");
  });

  it("reports no permissions while the session is still resolving", () => {
    stillResolving();

    expect(probe(() => `${usePermissions().permissions.length}:${usePermissions().isLoading}`)).toBe(
      "0:true",
    );
  });

  it("reports no permissions when signed out", () => {
    signedOut();

    expect(probe(() => String(usePermissions().can(PERMISSION.casesView)))).toBe("false");
  });
});

describe("useRole", () => {
  it("reports the current role", () => {
    signInAs("court");

    expect(probe(() => useRole().role ?? "none")).toBe("court");
  });

  it("matches a single role and a set of roles", () => {
    signInAs("lawyer");

    expect(probe(() => String(useRole().is("lawyer")))).toBe("true");
    expect(probe(() => String(useRole().is("administrator")))).toBe("false");
    expect(probe(() => String(useRole().isAny(["administrator", "lawyer"])))).toBe("true");
  });

  it("has no role when signed out", () => {
    signedOut();

    expect(probe(() => `${useRole().role ?? "none"}:${useRole().isAny(["administrator"])}`)).toBe(
      "none:false",
    );
  });
});

// --------------------------------------------------------------------------- //
// Protected component
// --------------------------------------------------------------------------- //

describe("Protected", () => {
  it("renders children when the permission is held", () => {
    signInAs("administrator");

    render(
      <Protected permission={PERMISSION.casesAssign}>
        <p>Assign lawyer</p>
      </Protected>,
    );

    expect(screen.getByText("Assign lawyer")).toBeInTheDocument();
  });

  it("renders nothing when the permission is missing", () => {
    signInAs("lawyer");

    render(
      <Protected permission={PERMISSION.casesAssign}>
        <p>Assign lawyer</p>
      </Protected>,
    );

    expect(screen.queryByText("Assign lawyer")).not.toBeInTheDocument();
  });

  it("renders the fallback when the permission is missing", () => {
    signInAs("court");

    render(
      <Protected permission={PERMISSION.aiChat} fallback={<p>Not available</p>}>
        <p>Ask the assistant</p>
      </Protected>,
    );

    expect(screen.getByText("Not available")).toBeInTheDocument();
    expect(screen.queryByText("Ask the assistant")).not.toBeInTheDocument();
  });

  it("supports role rules", () => {
    signInAs("court");

    render(
      <Protected roles={["administrator"]}>
        <p>Admin only</p>
      </Protected>,
    );

    expect(screen.queryByText("Admin only")).not.toBeInTheDocument();
  });

  it("hides protected content while the session is still resolving", () => {
    // Rendering first and hiding afterwards would flash a control the user is
    // not entitled to.
    stillResolving();

    render(
      <Protected permission={PERMISSION.casesView} loading={<p>Checking…</p>}>
        <p>Case list</p>
      </Protected>,
    );

    expect(screen.getByText("Checking…")).toBeInTheDocument();
    expect(screen.queryByText("Case list")).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// ProtectedRoute
// --------------------------------------------------------------------------- //

describe("ProtectedRoute", () => {
  it("renders the page for an authorized user", () => {
    signInAs("administrator");

    render(
      <ProtectedRoute permission={PERMISSION.usersView}>
        <p>Lawyer directory</p>
      </ProtectedRoute>,
    );

    expect(screen.getByText("Lawyer directory")).toBeInTheDocument();
  });

  it("shows the Unauthorized page instead of the content", () => {
    signInAs("lawyer");

    render(
      <ProtectedRoute permission={PERMISSION.usersView}>
        <p>Lawyer directory</p>
      </ProtectedRoute>,
    );

    expect(screen.getByText("Access denied")).toBeInTheDocument();
    expect(screen.queryByText("Lawyer directory")).not.toBeInTheDocument();
  });

  it("never names the missing permission", () => {
    signInAs("lawyer");

    const { container } = render(
      <ProtectedRoute permission={PERMISSION.usersView}>
        <p>Lawyer directory</p>
      </ProtectedRoute>,
    );

    expect(container.textContent).not.toContain(PERMISSION.usersView);
  });

  it("waits instead of denying while the session is still resolving", () => {
    stillResolving();

    render(
      <ProtectedRoute permission={PERMISSION.casesView}>
        <p>Case list</p>
      </ProtectedRoute>,
    );

    expect(screen.getByText("Checking your access…")).toBeInTheDocument();
    expect(screen.queryByText("Access denied")).not.toBeInTheDocument();
    expect(screen.queryByText("Case list")).not.toBeInTheDocument();
  });

  it("reveals the page once a pending session resolves as authorized", () => {
    stillResolving();

    render(
      <ProtectedRoute permission={PERMISSION.reportsView}>
        <p>Reports</p>
      </ProtectedRoute>,
    );
    expect(screen.queryByText("Reports")).not.toBeInTheDocument();

    signInAs("lawyer");

    expect(screen.getByText("Reports")).toBeInTheDocument();
  });

  it("accepts a custom fallback", () => {
    signInAs("court");

    render(
      <ProtectedRoute permission={PERMISSION.reportsView} fallback={<p>Ask an administrator</p>}>
        <p>Reports</p>
      </ProtectedRoute>,
    );

    expect(screen.getByText("Ask an administrator")).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Route rules
// --------------------------------------------------------------------------- //

describe("accessRuleForPath", () => {
  it("returns no rule for routes every signed-in user may reach", () => {
    expect(accessRuleForPath("/dashboard")).toBeNull();
    expect(accessRuleForPath("/access-denied")).toBeNull();
  });

  it("resolves the rule declared on the navigation item", () => {
    expect(accessRuleForPath("/reports")).toEqual({ permission: PERMISSION.reportsView });
    expect(accessRuleForPath("/lawyers")).toEqual({ permission: PERMISSION.usersView });
  });

  it("applies a section's rule to its nested routes", () => {
    expect(accessRuleForPath("/cases/123/documents")).toEqual({
      permission: PERMISSION.casesView,
    });
  });

  it("does not match a route that merely shares a prefix", () => {
    expect(accessRuleForPath("/cases-archive")).toBeNull();
  });
});

describe("RouteGuard", () => {
  it("renders an unguarded route for every role", () => {
    pathname = "/dashboard";

    for (const role of ["administrator", "lawyer", "court"] as const) {
      signInAs(role);
      const { unmount } = render(
        <RouteGuard>
          <p>Dashboard</p>
        </RouteGuard>,
      );
      expect(screen.getByText("Dashboard")).toBeInTheDocument();
      unmount();
    }
  });

  it("blocks a route the current role may not open", () => {
    pathname = "/lawyers";
    signInAs("lawyer");

    render(
      <RouteGuard>
        <p>Lawyer directory</p>
      </RouteGuard>,
    );

    expect(screen.getByText("Access denied")).toBeInTheDocument();
    expect(screen.queryByText("Lawyer directory")).not.toBeInTheDocument();
  });

  it("blocks a nested route under a section the role may not open", () => {
    pathname = "/reports/monthly";
    signInAs("court");

    render(
      <RouteGuard>
        <p>Monthly report</p>
      </RouteGuard>,
    );

    expect(screen.queryByText("Monthly report")).not.toBeInTheDocument();
  });

  it("allows a route the role may open", () => {
    pathname = "/cases";
    signInAs("court");

    render(
      <RouteGuard>
        <p>Case list</p>
      </RouteGuard>,
    );

    expect(screen.getByText("Case list")).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Role-aware navigation
// --------------------------------------------------------------------------- //

describe("SidebarNav", () => {
  function visibleLinks(): string[] {
    return screen
      .getAllByRole("link")
      .map((link) => link.textContent?.trim() ?? "")
      .filter(Boolean);
  }

  it("shows every destination to an administrator", () => {
    signInAs("administrator");
    render(<SidebarNav />);

    // The labels are the catalogue's, resolved the way the sidebar resolves
    // them — asserting on `titleKey` would assert that the config matches
    // itself.
    expect(visibleLinks()).toEqual(navItems.map((item) => navLabel(item.titleKey)));
  });

  it("hides user management from a lawyer", () => {
    signInAs("lawyer");
    render(<SidebarNav />);

    const links = visibleLinks();
    expect(links).not.toContain("Lawyers");
    expect(links).toContain("Cases");
    expect(links).toContain("AI Assistant");
    expect(links).toContain("Reports");
  });

  it("hides reporting and AI from a court representative", () => {
    signInAs("court");
    render(<SidebarNav />);

    const links = visibleLinks();
    expect(links).not.toContain("Lawyers");
    expect(links).not.toContain("Reports");
    expect(links).not.toContain("AI Assistant");
    expect(links).toContain("Court Updates");
    expect(links).toContain("Documents");
  });

  it("shows nothing but always-available items while the session resolves", () => {
    stillResolving();
    render(<SidebarNav />);

    expect(visibleLinks()).toEqual(["Dashboard"]);
  });

  it("never offers a destination the route guard would block", () => {
    // The invariant the whole design rests on: both read the same rules.
    for (const role of ["administrator", "lawyer", "court"] as const) {
      signInAs(role);
      const { unmount } = render(<SidebarNav />);
      const links = visibleLinks();

      for (const item of navItems) {
        const rule = accessRuleForPath(item.href);
        const allowed =
          rule === null || isAllowed(rule, { role, permissions: ROLE_PERMISSIONS[role] });
        expect(links.includes(navLabel(item.titleKey))).toBe(allowed);
      }
      unmount();
    }
  });
});

describe("navigation configuration", () => {
  it("declares access rules centrally, not inside components", () => {
    // Every guarded destination must be resolvable from the config alone.
    for (const { path, access } of routeAccessRules) {
      expect(accessRuleForPath(path)).toEqual(access);
    }
  });

  it("only references permissions the platform defines", () => {
    for (const item of navItems) {
      for (const permission of [
        item.access?.permission,
        ...(item.access?.anyOf ?? []),
        ...(item.access?.allOf ?? []),
      ]) {
        if (permission) expect(PERMISSIONS).toContain(permission);
      }
    }
  });

  it("keeps every section reachable by at least one role", () => {
    for (const section of sidebarNavigation) {
      const reachable = section.items.some((item) =>
        (["administrator", "lawyer", "court"] as const).some(
          (role) =>
            item.access === undefined ||
            isAllowed(item.access, { role, permissions: ROLE_PERMISSIONS[role] }),
        ),
      );
      expect(reachable).toBe(true);
    }
  });
});
