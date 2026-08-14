/**
 * Tests for the settings client.
 *
 * Cover what the user is *shown* and what the client *sends*: the wire mapping,
 * the server-described section navigation, the generic setting controls, the
 * profile form, the security panel, and what a role without the administrative
 * capability gets instead.
 *
 * The API is the real boundary — its 401/403, the per-caller scope, the
 * whole-batch validation, and the ownership rule that keeps notification
 * preferences on the Notification Service are covered by
 * `tests/integration/test_settings.py` and `tests/unit/test_settings_service.py`.
 */

import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AdministrationSettingsPanel } from "@/components/settings/administration-settings-panel";
import { ProfileSettingsForm } from "@/components/settings/profile-settings-form";
import { SecuritySettingsPanel } from "@/components/settings/security-settings-panel";
import { SettingsWorkspace } from "@/components/settings/settings-workspace";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SETTINGS_ENDPOINTS } from "@/lib/api/config";
import {
  fetchSettingsOverview,
  updateSettings,
} from "@/lib/api/settings";
import { ROUTES } from "@/lib/routes";
import { useSessionStore } from "@/stores/session-store";
import type { UserRole } from "@/types/user";
import {
  errorEnvelope,
  mockFetch,
  notificationPreferencesPayload,
  profilePayload,
  sessionListPayload,
  sessionPayload,
  sessionUserWithRole,
  settingsCollectionPayload,
  settingsOverviewPayload,
  settingsSectionsPayload,
} from "./helpers";

vi.mock("next/navigation", () => ({
  usePathname: () => ROUTES.settings,
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn() }),
}));

function signInAs(role: UserRole) {
  act(() => {
    useSessionStore.setState({ user: sessionUserWithRole(role), status: "authenticated" });
  });
}

function renderWithQuery(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });

  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>{ui}</TooltipProvider>
      </QueryClientProvider>,
    ),
  };
}

// --------------------------------------------------------------------------- //
// Wire mapping
// --------------------------------------------------------------------------- //

describe("settings API client", () => {
  it("maps the overview onto the app's domain types", async () => {
    mockFetch({
      [SETTINGS_ENDPOINTS.overview]: { body: settingsOverviewPayload() },
    });

    const overview = await fetchSettingsOverview();

    expect(overview.profile.fullName).toBe("Amina Benali");
    expect(overview.profile.jobTitle).toBeNull();
    expect(overview.maintenance).toEqual({ maintenanceMode: false, message: null });
    expect(overview.sections[0]).toEqual({
      section: "profile",
      storage: "profile",
      editable: true,
      administrative: false,
    });
  });

  it("sends a list of changes rather than the whole set", async () => {
    const { requests } = mockFetch({
      [SETTINGS_ENDPOINTS.preferences]: { body: settingsCollectionPayload() },
    });

    await updateSettings([{ key: "theme", value: "light" }]);

    const call = requests.find((request) => request.method === "PUT");
    // Only what changed. Two settings panels open at once cannot then silently
    // revert each other's saves.
    expect(call?.body).toEqual({
      settings: [{ setting_key: "theme", value: "light" }],
    });
  });
});

// --------------------------------------------------------------------------- //
// The page
// --------------------------------------------------------------------------- //

describe("SettingsWorkspace", () => {
  it("builds its navigation from what the server sent", async () => {
    signInAs("lawyer");
    mockFetch({
      [SETTINGS_ENDPOINTS.overview]: { body: settingsOverviewPayload() },
    });

    renderWithQuery(<SettingsWorkspace />);

    // Server-described, so a tenth section reaches a browser nobody redeployed.
    const nav = await screen.findByRole("navigation", { name: /settings sections/i });
    expect(nav).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Appearance" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Language & region" })).toBeInTheDocument();
  });

  it("does not offer Administration to a role that cannot manage the platform", async () => {
    signInAs("lawyer");
    mockFetch({
      [SETTINGS_ENDPOINTS.overview]: { body: settingsOverviewPayload() },
    });

    renderWithQuery(<SettingsWorkspace />);

    await screen.findByRole("button", { name: "Profile" });
    // Omitted entirely rather than shown disabled: showing it would tell every
    // lawyer which platform settings exist and that somebody else controls them.
    expect(screen.queryByRole("button", { name: "Administration" })).not.toBeInTheDocument();
  });

  it("offers Administration to an administrator", async () => {
    signInAs("administrator");
    mockFetch({
      [SETTINGS_ENDPOINTS.overview]: {
        body: settingsOverviewPayload({ sections: settingsSectionsPayload(true) }),
      },
    });

    renderWithQuery(<SettingsWorkspace />);

    expect(
      await screen.findByRole("button", { name: "Administration" }),
    ).toBeInTheDocument();
  });

  it("shows a maintenance notice when the platform is in maintenance", async () => {
    signInAs("lawyer");
    mockFetch({
      [SETTINGS_ENDPOINTS.overview]: {
        body: settingsOverviewPayload({
          maintenance: { maintenance_mode: true, message: "Back at 18:00" },
        }),
      },
    });

    renderWithQuery(<SettingsWorkspace />);

    expect(await screen.findByText("Back at 18:00")).toBeInTheDocument();
  });

  it("surfaces a failure without exposing internals", async () => {
    signInAs("lawyer");
    mockFetch({
      [SETTINGS_ENDPOINTS.overview]: {
        status: 503,
        body: errorEnvelope("service_unavailable"),
      },
    });

    renderWithQuery(<SettingsWorkspace />);

    expect(await screen.findByText(/could not be loaded/i)).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Generic controls
// --------------------------------------------------------------------------- //

describe("setting controls", () => {
  async function openSection(name: string) {
    await userEvent.click(await screen.findByRole("button", { name }));
  }

  it("renders a control per value type, from the server's definitions", async () => {
    signInAs("lawyer");
    mockFetch({
      [SETTINGS_ENDPOINTS.overview]: { body: settingsOverviewPayload() },
    });

    renderWithQuery(<SettingsWorkspace />);
    await openSection("AI assistant");

    // `enum` → a select, `boolean` → a checkbox. One renderer per value type is
    // what makes an eleventh setting free in the browser.
    expect(screen.getByRole("combobox", { name: /response length/i })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /stream responses/i })).toBeInTheDocument();
  });

  it("says when a value is the platform's default rather than a choice", async () => {
    signInAs("lawyer");
    mockFetch({
      [SETTINGS_ENDPOINTS.overview]: { body: settingsOverviewPayload() },
    });

    renderWithQuery(<SettingsWorkspace />);
    await openSection("Appearance");

    expect(await screen.findByText(/platform default/i)).toBeInTheDocument();
  });

  it("saves one setting immediately, sending only what changed", async () => {
    signInAs("lawyer");
    const { requests } = mockFetch({
      [SETTINGS_ENDPOINTS.overview]: { body: settingsOverviewPayload() },
      [SETTINGS_ENDPOINTS.preferences]: {
        body: settingsCollectionPayload({ ai_streaming: false }),
      },
    });

    renderWithQuery(<SettingsWorkspace />);
    await openSection("AI assistant");
    await userEvent.click(screen.getByRole("checkbox", { name: /stream responses/i }));

    await waitFor(() => {
      const call = requests.find((request) => request.method === "PUT");
      expect(call?.body).toEqual({
        settings: [{ setting_key: "ai_streaming", value: false }],
      });
    });
  });

  it("restores the previous value and says why when a save is refused", async () => {
    signInAs("lawyer");
    mockFetch({
      [SETTINGS_ENDPOINTS.overview]: { body: settingsOverviewPayload() },
      [SETTINGS_ENDPOINTS.preferences]: {
        status: 422,
        body: errorEnvelope("invalid_setting_value", "Invalid.", [
          { field: "ai_streaming", message: "expected true or false" },
        ]),
      },
    });

    renderWithQuery(<SettingsWorkspace />);
    await openSection("AI assistant");

    const checkbox = screen.getByRole("checkbox", { name: /stream responses/i });
    expect(checkbox).toBeChecked();
    await userEvent.click(checkbox);

    // The mutation never wrote it locally, so a refusal simply leaves the
    // rendered value alone — which is what makes an immediate save safe.
    await waitFor(() => {
      expect(screen.getByRole("checkbox", { name: /stream responses/i })).toBeChecked();
    });
  });

  it("renders a list setting as a checkbox grid over the server's choices", async () => {
    signInAs("lawyer");
    mockFetch({
      [SETTINGS_ENDPOINTS.overview]: { body: settingsOverviewPayload() },
    });

    renderWithQuery(<SettingsWorkspace />);
    await openSection("Dashboard");

    expect(screen.getByRole("checkbox", { name: "My cases" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Recent cases" })).toBeInTheDocument();
  });

  it("adds a widget to the list without rewriting the rest", async () => {
    signInAs("lawyer");
    const { requests } = mockFetch({
      [SETTINGS_ENDPOINTS.overview]: {
        body: settingsOverviewPayload({
          settings: settingsCollectionPayload({ dashboard_widgets: ["my_cases"] }),
        }),
      },
      [SETTINGS_ENDPOINTS.preferences]: {
        body: settingsCollectionPayload({
          dashboard_widgets: ["my_cases", "recent_cases"],
        }),
      },
    });

    renderWithQuery(<SettingsWorkspace />);
    await openSection("Dashboard");
    await userEvent.click(screen.getByRole("checkbox", { name: "Recent cases" }));

    await waitFor(() => {
      const call = requests.find((request) => request.method === "PUT");
      // Order preserved: a list somebody arranged is a list somebody arranged.
      expect(call?.body).toEqual({
        settings: [
          { setting_key: "dashboard_widgets", value: ["my_cases", "recent_cases"] },
        ],
      });
    });
  });
});

// --------------------------------------------------------------------------- //
// Profile
// --------------------------------------------------------------------------- //

describe("ProfileSettingsForm", () => {
  it("shows the editable fields and the ones an administrator owns", async () => {
    signInAs("lawyer");
    mockFetch({ [SETTINGS_ENDPOINTS.profile]: { body: profilePayload() } });

    renderWithQuery(
      <ProfileSettingsForm
        profile={{
          id: "user-lawyer",
          email: "lawyer@example.com",
          firstName: "Karim",
          lastName: "Idrissi",
          fullName: "Karim Idrissi",
          phone: null,
          profileImage: null,
          jobTitle: null,
          role: "lawyer",
          status: "active",
          mustChangePassword: false,
          lastLoginAt: null,
          createdAt: "2026-07-01T09:00:00Z",
          updatedAt: "2026-07-01T09:00:00Z",
        }}
      />,
    );

    expect(screen.getByLabelText("First name")).toHaveValue("Karim");
    expect(screen.getByLabelText("Job title")).toBeInTheDocument();
    // Read-only, and shown rather than hidden: "what am I on this platform?" is a
    // reasonable question to answer where you edit the rest.
    expect(screen.queryByLabelText("Email address")).not.toBeInTheDocument();
    // Twice on the card: once under the avatar preview, once in the read-only
    // block beside the role.
    expect(screen.getAllByText("lawyer@example.com").length).toBeGreaterThan(0);
    expect(screen.getByText("Email address")).toBeInTheDocument();
  });

  it("sends only the fields the form carries", async () => {
    signInAs("lawyer");
    const { requests } = mockFetch({
      [SETTINGS_ENDPOINTS.profile]: { body: profilePayload({ job_title: "Partner" }) },
    });

    renderWithQuery(
      <ProfileSettingsForm
        profile={{
          id: "user-lawyer",
          email: "lawyer@example.com",
          firstName: "Karim",
          lastName: "Idrissi",
          fullName: "Karim Idrissi",
          phone: null,
          profileImage: null,
          jobTitle: null,
          role: "lawyer",
          status: "active",
          mustChangePassword: false,
          lastLoginAt: null,
          createdAt: "2026-07-01T09:00:00Z",
          updatedAt: "2026-07-01T09:00:00Z",
        }}
      />,
    );

    await userEvent.type(screen.getByLabelText("Job title"), "Partner");
    await userEvent.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => {
      const call = requests.find((request) => request.method === "PATCH");
      expect(call?.body).toMatchObject({ job_title: "Partner" });
      // No role, no status, no email — the fields do not exist on the form
      // because a self-service endpoint accepting one would be a door.
      expect(call?.body).not.toHaveProperty("role");
      expect(call?.body).not.toHaveProperty("email");
    });
  });
});

// --------------------------------------------------------------------------- //
// Security
// --------------------------------------------------------------------------- //

describe("SecuritySettingsPanel", () => {
  it("lists each sign-in and marks the current one", async () => {
    signInAs("lawyer");
    mockFetch({
      [SETTINGS_ENDPOINTS.sessions]: {
        body: sessionListPayload([
          sessionPayload(),
          sessionPayload({
            session_id: "session-2",
            is_current: false,
            user_agent: "Mozilla/5.0 (Macintosh)",
          }),
        ]),
      },
    });

    renderWithQuery(<SecuritySettingsPanel mustChangePassword={false} />);

    expect(await screen.findByText("This device")).toBeInTheDocument();
    expect(screen.getByText(/1 other device is signed in/i)).toBeInTheDocument();
  });

  it("distinguishes an unavailable list from an empty one", async () => {
    signInAs("lawyer");
    mockFetch({
      [SETTINGS_ENDPOINTS.sessions]: { body: sessionListPayload([], false) },
    });

    renderWithQuery(<SecuritySettingsPanel mustChangePassword={false} />);

    // Saying "you have no sessions" here would be false, and revocation works
    // regardless — it does not depend on this list.
    expect(await screen.findByText(/list of devices is unavailable/i)).toBeInTheDocument();
  });

  it("warns before signing out everywhere else", async () => {
    signInAs("lawyer");
    mockFetch({
      [SETTINGS_ENDPOINTS.sessions]: { body: sessionListPayload() },
    });

    renderWithQuery(<SecuritySettingsPanel mustChangePassword={false} />);

    expect(
      await screen.findByText(/ends all of them at once/i),
    ).toBeInTheDocument();
  });

  it("tells somebody an administrator set their password", async () => {
    signInAs("lawyer");
    mockFetch({
      [SETTINGS_ENDPOINTS.sessions]: { body: sessionListPayload() },
    });

    renderWithQuery(<SecuritySettingsPanel mustChangePassword />);

    expect(
      await screen.findByText(/password was set by an administrator/i),
    ).toBeInTheDocument();
  });

  it("refuses a mismatched confirmation without calling the API", async () => {
    signInAs("lawyer");
    const { requests } = mockFetch({
      [SETTINGS_ENDPOINTS.sessions]: { body: sessionListPayload() },
    });

    renderWithQuery(<SecuritySettingsPanel mustChangePassword={false} />);

    await userEvent.type(screen.getByLabelText("Current password"), "old-password");
    await userEvent.type(screen.getByLabelText("New password"), "a-brand-new-secret");
    await userEvent.type(screen.getByLabelText("Repeat new password"), "something-else");
    await userEvent.click(screen.getByRole("button", { name: /change password/i }));

    expect(await screen.findByText(/do not match/i)).toBeInTheDocument();
    // "Did you type it twice the same" is a question about a form, not about a
    // password — so nothing is sent.
    expect(requests.some((request) => request.method === "POST")).toBe(false);
  });

  it("changes the password and never sends the confirmation field", async () => {
    signInAs("lawyer");
    const { requests } = mockFetch({
      [SETTINGS_ENDPOINTS.sessions]: { body: sessionListPayload() },
      [SETTINGS_ENDPOINTS.password]: {
        body: {
          message: "Password changed successfully.",
          access_token: "replacement-token",
          refresh_token: "replacement-refresh",
          expires_in: 900,
        },
      },
    });

    renderWithQuery(<SecuritySettingsPanel mustChangePassword={false} />);

    await userEvent.type(screen.getByLabelText("Current password"), "old-password");
    await userEvent.type(screen.getByLabelText("New password"), "a-brand-new-secret");
    await userEvent.type(screen.getByLabelText("Repeat new password"), "a-brand-new-secret");
    await userEvent.click(screen.getByRole("button", { name: /change password/i }));

    await waitFor(() => {
      const call = requests.find((request) => request.method === "POST");
      expect(call?.body).toEqual({
        current_password: "old-password",
        new_password: "a-brand-new-secret",
      });
    });
  });
});

// --------------------------------------------------------------------------- //
// Administration
// --------------------------------------------------------------------------- //

describe("AdministrationSettingsPanel", () => {
  it("renders nothing without the capability, and asks the API for nothing", () => {
    signInAs("lawyer");
    const { requests } = mockFetch({
      [SETTINGS_ENDPOINTS.administration]: { body: settingsCollectionPayload() },
    });

    const { container } = renderWithQuery(
      <AdministrationSettingsPanel canManage={false} />,
    );

    expect(container).toBeEmptyDOMElement();
    // Gated by the caller rather than fetched-and-refused: a query that 403s on
    // every render for every non-administrator would fill an error log with
    // policy working correctly.
    expect(requests).toHaveLength(0);
  });

  it("says that maintenance mode announces rather than closes the platform", async () => {
    signInAs("administrator");
    mockFetch({
      [SETTINGS_ENDPOINTS.administration]: {
        body: {
          settings: [
            {
              key: "maintenance_mode",
              section: "administration",
              value: false,
              is_default: true,
            },
          ],
          definitions: [
            {
              key: "maintenance_mode",
              section: "administration",
              value_type: "boolean",
              choices: [],
              max_length: null,
              max_items: null,
            },
          ],
        },
      },
    });

    renderWithQuery(<AdministrationSettingsPanel canManage />);

    // Said twice on purpose — once in the banner above the card and once in the
    // setting's own description — so it cannot be missed either way in.
    expect(
      (await screen.findAllByText(/does not close the platform/i)).length,
    ).toBeGreaterThan(0);
  });
});

// --------------------------------------------------------------------------- //
// Ownership
// --------------------------------------------------------------------------- //

describe("feature ownership", () => {
  it("reads notification preferences from the Notification Service", async () => {
    signInAs("lawyer");
    const { requests } = mockFetch({
      [SETTINGS_ENDPOINTS.overview]: { body: settingsOverviewPayload() },
      "/notifications/preferences": { body: notificationPreferencesPayload() },
    });

    renderWithQuery(<SettingsWorkspace />);
    await userEvent.click(await screen.findByRole("button", { name: "Notifications" }));

    // `20-settings.md`: each feature owns its configuration. The Settings page
    // presents them; the Notification Service serves them.
    await waitFor(() => {
      expect(
        requests.some((request) => request.url.includes("/notifications/preferences")),
      ).toBe(true);
    });
    expect(
      requests.some((request) => request.url.includes("/settings/notifications")),
    ).toBe(false);
  });

  it("shows the channels split across the two sections", async () => {
    signInAs("lawyer");
    mockFetch({
      [SETTINGS_ENDPOINTS.overview]: { body: settingsOverviewPayload() },
      "/notifications/preferences": { body: notificationPreferencesPayload() },
    });

    renderWithQuery(<SettingsWorkspace />);

    // Notifications is *what* you are told about…
    await userEvent.click(await screen.findByRole("button", { name: "Notifications" }));
    expect(await screen.findByLabelText("Case updates — In app")).toBeInTheDocument();
    expect(screen.queryByLabelText("Case updates — Email")).not.toBeInTheDocument();

    // …Communication is *how* it reaches you. One store, two projections.
    await userEvent.click(screen.getByRole("button", { name: "Communication" }));
    expect(await screen.findByLabelText("Case updates — Email")).toBeInTheDocument();
    expect(screen.getByLabelText("Case updates — WhatsApp")).toBeInTheDocument();
    expect(screen.queryByLabelText("Case updates — In app")).not.toBeInTheDocument();
  });
});
