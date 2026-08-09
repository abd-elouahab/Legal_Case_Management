/**
 * Tests for the notification client.
 *
 * Cover what the user is *shown* and what the client *sends*: the wire mapping,
 * the bell and its badge, the panel, the history feed and its server-side
 * filters, read state, preferences, the announcement form, and what each role
 * gets instead.
 *
 * The API is the real boundary — its 401/403, the per-recipient scope, the
 * event-driven creation, and the authorization that decides who is notified are
 * covered by `tests/integration/test_notifications.py` and
 * `tests/unit/test_notification_events.py`.
 */

import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AnnouncementForm } from "@/components/notifications/announcement-form";
import { NotificationButton } from "@/components/layout/notification-button";
import { NotificationFeed } from "@/components/notifications/notification-feed";
import { NotificationItem } from "@/components/notifications/notification-item";
import { NotificationPreferencesForm } from "@/components/notifications/notification-preferences-form";
import { TooltipProvider } from "@/components/ui/tooltip";
import { NOTIFICATION_ENDPOINTS } from "@/lib/api/config";
import {
  buildNotificationListParams,
  fetchNotificationSummary,
  fetchNotifications,
} from "@/lib/api/notifications";
import { staleKeysFor } from "@/lib/realtime/sync";
import { ROUTES } from "@/lib/routes";
import { useSessionStore } from "@/stores/session-store";
import {
  DEFAULT_NOTIFICATION_QUERY,
  notificationHref,
  type Notification,
} from "@/types/notification";
import type { RealtimeEvent } from "@/types/realtime";
import type { UserRole } from "@/types/user";
import {
  mockFetch,
  notificationPagePayload,
  notificationPayload,
  notificationPreferencesPayload,
  notificationSummaryPayload,
  sessionUserWithRole,
} from "./helpers";

vi.mock("next/navigation", () => ({
  usePathname: () => ROUTES.notifications,
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn() }),
}));

function signInAs(role: UserRole) {
  act(() => {
    useSessionStore.setState({ user: sessionUserWithRole(role), status: "authenticated" });
  });
}

/**
 * Render inside the providers the app shell supplies.
 *
 * `TooltipProvider` is one of them: the bell's tooltip is a Radix primitive and
 * throws outside a provider, which is a real constraint on where the component
 * may be mounted rather than a testing detail.
 */
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

/** The app's `Notification`, built from the wire fixture the API would send. */
function notificationFor(overrides: Record<string, unknown> = {}): Notification {
  const payload = notificationPayload(overrides);

  return {
    id: payload.id,
    category: payload.category,
    notificationType: payload.notification_type as Notification["notificationType"],
    priority: payload.priority as Notification["priority"],
    title: payload.title,
    message: payload.message,
    language: payload.language,
    eventType: payload.event_type,
    ruleKey: payload.rule_key,
    caseId: payload.case_id,
    actor: payload.actor
      ? {
          id: payload.actor.id,
          fullName: payload.actor.full_name,
          role: payload.actor.role,
        }
      : null,
    target: payload.target
      ? { targetType: payload.target.target_type, targetId: payload.target.target_id }
      : null,
    readAt: payload.read_at,
    isRead: payload.is_read,
    createdAt: payload.created_at,
  };
}

// --------------------------------------------------------------------------- //
// The wire
// --------------------------------------------------------------------------- //

describe("the notification wire", () => {
  it("maps a notification onto the app's shape", async () => {
    mockFetch({
      [NOTIFICATION_ENDPOINTS.list]: { body: notificationPagePayload() },
    });

    const page = await fetchNotifications(DEFAULT_NOTIFICATION_QUERY);
    const item = page.items[0]!;

    expect(item.notificationType).toBe("information");
    expect(item.isRead).toBe(false);
    expect(item.actor?.fullName).toBe("Amina Benali");
    expect(item.target).toEqual({
      targetType: "case",
      targetId: "22222222-2222-4222-8222-222222222222",
    });
  });

  it("accepts a category this build has never heard of", async () => {
    // The registry is open on the server by design, so a ninth category must
    // render rather than turning somebody's feed into a parse error.
    mockFetch({
      [NOTIFICATION_ENDPOINTS.list]: {
        body: notificationPagePayload({
          items: [notificationPayload({ category: "deposition" })],
        }),
      },
    });

    const page = await fetchNotifications(DEFAULT_NOTIFICATION_QUERY);
    expect(page.items[0]!.category).toBe("deposition");
  });

  it("refuses a priority the platform does not define", async () => {
    // The opposite rule, and for the opposite reason: priority is a PostgreSQL
    // enum on the server, so an unknown value is a contract break.
    mockFetch({
      [NOTIFICATION_ENDPOINTS.list]: {
        body: notificationPagePayload({
          items: [notificationPayload({ priority: "apocalyptic" })],
        }),
      },
    });

    await expect(fetchNotifications(DEFAULT_NOTIFICATION_QUERY)).rejects.toThrow();
  });

  it("omits filters that are not set", () => {
    const params = buildNotificationListParams(DEFAULT_NOTIFICATION_QUERY);

    expect(params).toContain("page=1");
    expect(params).not.toContain("category=");
    expect(params).not.toContain("unread_only");
  });

  it("sends every filter the user chose", () => {
    const params = buildNotificationListParams({
      ...DEFAULT_NOTIFICATION_QUERY,
      unreadOnly: true,
      category: "hearing",
      priority: "critical",
      language: "ar",
    });

    expect(params).toContain("unread_only=true");
    expect(params).toContain("category=hearing");
    expect(params).toContain("priority=critical");
    expect(params).toContain("language=ar");
  });

  it("reads the badge from its own endpoint", async () => {
    const { requests } = mockFetch({
      [NOTIFICATION_ENDPOINTS.summary]: { body: notificationSummaryPayload() },
    });

    const summary = await fetchNotificationSummary();

    expect(summary.unreadCount).toBe(3);
    expect(summary.highestUnreadPriority).toBe("high");
    // The badge must never cost a page of notifications.
    expect(requests[0]!.url).toContain("/notifications/summary");
  });
});

// --------------------------------------------------------------------------- //
// Navigation
// --------------------------------------------------------------------------- //

describe("notification navigation", () => {
  it("opens the case a case notification names", () => {
    expect(notificationHref(notificationFor())).toBe(
      `/cases/${encodeURIComponent("22222222-2222-4222-8222-222222222222")}`,
    );
  });

  it("opens a document's case, because this client has no document route", () => {
    const notification = notificationFor({
      target: { target_type: "document", target_id: "33333333-3333-4333-8333-333333333333" },
    });

    expect(notificationHref(notification)).toBe(
      `/cases/${encodeURIComponent("22222222-2222-4222-8222-222222222222")}`,
    );
  });

  it("leads nowhere when the notification names no target", () => {
    // A withdrawn document and a case somebody was removed from both arrive with
    // no target, because offering to open either would offer a refusal.
    expect(notificationHref(notificationFor({ target: null }))).toBeNull();
  });

  it("leads nowhere for a target type this build does not know", () => {
    const notification = notificationFor({
      target: { target_type: "hearing", target_id: "44444444-4444-4444-8444-444444444444" },
    });

    expect(notificationHref(notification)).toBeNull();
  });
});

// --------------------------------------------------------------------------- //
// One row
// --------------------------------------------------------------------------- //

describe("NotificationItem", () => {
  it("states its content as text, never by colour alone", () => {
    render(<NotificationItem notification={notificationFor()} />);

    expect(screen.getByText("Nouveau dossier")).toBeInTheDocument();
    expect(screen.getByText("Le dossier CASE-2026-0001 a été créé.")).toBeInTheDocument();
  });

  it("offers Mark read only while a notification is unread", async () => {
    const onMarkRead = vi.fn();
    const { rerender } = render(
      <NotificationItem notification={notificationFor()} onMarkRead={onMarkRead} />,
    );

    await userEvent.click(screen.getByRole("button", { name: /mark .* as read/i }));
    expect(onMarkRead).toHaveBeenCalledWith(notificationFor().id);

    rerender(
      <NotificationItem
        notification={notificationFor({ is_read: true, read_at: "2026-08-08T10:00:00Z" })}
        onMarkRead={onMarkRead}
      />,
    );
    expect(screen.queryByRole("button", { name: /mark .* as read/i })).not.toBeInTheDocument();
  });

  it("shows a priority badge only when it is urgent", () => {
    const { rerender } = render(<NotificationItem notification={notificationFor()} />);
    expect(screen.queryByText("Normal")).not.toBeInTheDocument();

    rerender(<NotificationItem notification={notificationFor({ priority: "critical" })} />);
    expect(screen.getByText("Critical")).toBeInTheDocument();
  });

  it("renders a targetless notification as text rather than a broken link", () => {
    render(<NotificationItem notification={notificationFor({ target: null })} />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// The bell
// --------------------------------------------------------------------------- //

describe("NotificationButton", () => {
  it("shows the unread count and states it in words", async () => {
    signInAs("lawyer");
    mockFetch({
      [NOTIFICATION_ENDPOINTS.summary]: { body: notificationSummaryPayload() },
    });

    renderWithQuery(<NotificationButton />);

    const button = await screen.findByRole("button", {
      name: /notifications, 3 unread/i,
    });
    expect(within(button).getByText("3")).toBeInTheDocument();
  });

  it("says 'more than' when the count is capped", async () => {
    signInAs("lawyer");
    mockFetch({
      [NOTIFICATION_ENDPOINTS.summary]: {
        body: notificationSummaryPayload({
          unread_count: 999,
          unread_count_capped: true,
        }),
      },
    });

    renderWithQuery(<NotificationButton />);

    await screen.findByRole("button", { name: /more than 999 unread/i });
    expect(screen.getByText("999+")).toBeInTheDocument();
  });

  it("renders no badge when everything is read", async () => {
    signInAs("lawyer");
    mockFetch({
      [NOTIFICATION_ENDPOINTS.summary]: {
        body: notificationSummaryPayload({ unread_count: 0, highest_unread_priority: null }),
      },
    });

    renderWithQuery(<NotificationButton />);

    const button = await screen.findByRole("button", { name: "Notifications" });
    expect(within(button).queryByText("0")).not.toBeInTheDocument();
  });

  it("opens the panel and lists the most recent notifications", async () => {
    signInAs("lawyer");
    mockFetch({
      [NOTIFICATION_ENDPOINTS.summary]: { body: notificationSummaryPayload() },
      [NOTIFICATION_ENDPOINTS.list]: { body: notificationPagePayload() },
    });

    renderWithQuery(<NotificationButton />);

    await userEvent.click(await screen.findByRole("button", { name: /notifications/i }));

    expect(await screen.findByText("Nouveau dossier")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /view all/i })).toHaveAttribute(
      "href",
      ROUTES.notifications,
    );
  });
});

// --------------------------------------------------------------------------- //
// The feed
// --------------------------------------------------------------------------- //

describe("NotificationFeed", () => {
  it("lists the caller's notifications", async () => {
    signInAs("lawyer");
    mockFetch({
      [NOTIFICATION_ENDPOINTS.list]: { body: notificationPagePayload() },
    });

    renderWithQuery(<NotificationFeed />);

    expect(await screen.findByText("Nouveau dossier")).toBeInTheDocument();
  });

  it("shows a distinct empty state when the filters match nothing", async () => {
    signInAs("lawyer");
    mockFetch({
      [NOTIFICATION_ENDPOINTS.list]: { body: notificationPagePayload({ items: [] }) },
    });

    renderWithQuery(<NotificationFeed />);

    await screen.findByText(/case updates, document activity/i);

    await userEvent.click(screen.getByRole("button", { name: /unread only/i }));

    expect(
      await screen.findByText(/no notifications match these filters/i),
    ).toBeInTheDocument();
  });

  it("sends every filter to the server rather than filtering the page", async () => {
    signInAs("lawyer");
    const { requests } = mockFetch({
      [NOTIFICATION_ENDPOINTS.list]: { body: notificationPagePayload() },
    });

    renderWithQuery(<NotificationFeed />);
    await screen.findByText("Nouveau dossier");

    await userEvent.click(screen.getByRole("button", { name: /unread only/i }));

    await waitFor(() => {
      expect(requests.some((request) => request.url.includes("unread_only=true"))).toBe(true);
    });
  });

  it("scopes 'mark all as read' to the category on screen", async () => {
    signInAs("lawyer");
    const { requests } = mockFetch({
      [NOTIFICATION_ENDPOINTS.readAll]: { body: notificationSummaryPayload() },
      [NOTIFICATION_ENDPOINTS.list]: { body: notificationPagePayload() },
    });

    renderWithQuery(<NotificationFeed />);
    await screen.findByText("Nouveau dossier");

    await userEvent.click(screen.getByRole("combobox", { name: /filter by category/i }));
    await userEvent.click(await screen.findByRole("option", { name: "Hearings" }));

    const button = await screen.findByRole("button", {
      name: /mark this category as read/i,
    });
    await userEvent.click(button);

    await waitFor(() => {
      const call = requests.find((request) => request.url.includes("/notifications/read-all"));
      expect(call?.body).toMatchObject({ category: "hearing" });
    });
  });

  it("marks one notification as read", async () => {
    signInAs("lawyer");
    const { requests } = mockFetch({
      [NOTIFICATION_ENDPOINTS.read]: { body: notificationSummaryPayload({ unread_count: 0 }) },
      [NOTIFICATION_ENDPOINTS.list]: { body: notificationPagePayload() },
    });

    renderWithQuery(<NotificationFeed />);
    // By the notification's own title: a feed of ten rows has ten of these
    // buttons, and the title is the only thing that tells them apart — which is
    // why the component puts it in the accessible name.
    await userEvent.click(
      await screen.findByRole("button", { name: /mark .*Nouveau dossier.* as read/i }),
    );

    await waitFor(() => {
      const call = requests.find(
        (request) => request.method === "PATCH" && request.url.endsWith("/notifications/read"),
      );
      expect(call?.body).toMatchObject({
        notification_ids: ["cccccccc-cccc-4ccc-8ccc-cccccccccccc"],
      });
    });
  });

  it("surfaces a failure without exposing internals", async () => {
    signInAs("lawyer");
    mockFetch({
      [NOTIFICATION_ENDPOINTS.list]: { networkError: true },
    });

    renderWithQuery(<NotificationFeed />);

    expect(
      await screen.findByText(/could not load your notifications/i),
    ).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Preferences
// --------------------------------------------------------------------------- //

describe("NotificationPreferencesForm", () => {
  it("renders every preference the server offers", async () => {
    signInAs("lawyer");
    mockFetch({
      [NOTIFICATION_ENDPOINTS.preferences]: { body: notificationPreferencesPayload() },
    });

    renderWithQuery(<NotificationPreferencesForm />);

    expect(await screen.findByLabelText("Case updates")).toBeChecked();
    expect(screen.getByLabelText("Text extraction")).toBeChecked();
    expect(screen.getByLabelText("Platform announcements")).toBeChecked();
  });

  it("says when a value is the platform's default rather than a choice", async () => {
    signInAs("lawyer");
    mockFetch({
      [NOTIFICATION_ENDPOINTS.preferences]: { body: notificationPreferencesPayload() },
    });

    renderWithQuery(<NotificationPreferencesForm />);

    const descriptions = await screen.findAllByText(/platform default/i);
    expect(descriptions.length).toBeGreaterThan(0);
  });

  it("saves one preference immediately, sending only what changed", async () => {
    signInAs("lawyer");
    const { requests } = mockFetch({
      [NOTIFICATION_ENDPOINTS.preferences]: [
        { body: notificationPreferencesPayload() },
        { body: notificationPreferencesPayload({ ocr_completion: false }) },
      ],
    });

    renderWithQuery(<NotificationPreferencesForm />);
    await userEvent.click(await screen.findByLabelText("Text extraction"));

    await waitFor(() => {
      const call = requests.find((request) => request.method === "PUT");
      expect(call?.body).toMatchObject({
        preferences: [{ preference_key: "ocr_completion", in_app: false }],
      });
    });
  });
});

// --------------------------------------------------------------------------- //
// Announcements
// --------------------------------------------------------------------------- //

describe("AnnouncementForm", () => {
  it("publishes an announcement and reports what it reached", async () => {
    signInAs("administrator");
    const { requests } = mockFetch({
      [NOTIFICATION_ENDPOINTS.announcements]: {
        status: 201,
        body: { recipients: 12, skipped: 2, kind: "maintenance" },
      },
    });

    renderWithQuery(<AnnouncementForm />);

    await userEvent.type(
      screen.getByLabelText("Message"),
      "Read-only on Sunday 08:00-10:00.",
    );
    await userEvent.click(screen.getByRole("button", { name: /send announcement/i }));

    await waitFor(() => {
      const call = requests.find((request) => request.method === "POST");
      expect(call?.body).toMatchObject({
        kind: "announcement",
        message: "Read-only on Sunday 08:00-10:00.",
      });
    });
  });

  it("will not send a blank announcement", async () => {
    signInAs("administrator");
    mockFetch({ [NOTIFICATION_ENDPOINTS.announcements]: { body: {} } });

    renderWithQuery(<AnnouncementForm />);

    expect(screen.getByRole("button", { name: /send announcement/i })).toBeDisabled();
  });

  it("counts characters against the API's own limit", async () => {
    signInAs("administrator");
    mockFetch({ [NOTIFICATION_ENDPOINTS.announcements]: { body: {} } });

    renderWithQuery(<AnnouncementForm />);
    await userEvent.type(screen.getByLabelText("Message"), "Sunday.");

    expect(screen.getByText("7 / 500 characters")).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Real-time
// --------------------------------------------------------------------------- //

describe("real-time notification delivery", () => {
  function event(overrides: Partial<RealtimeEvent> = {}): RealtimeEvent {
    return {
      id: "event-1",
      sequence: 1,
      event: "notification.created",
      topic: "user:11111111-1111-4111-8111-111111111111",
      scope: "user",
      caseId: null,
      actorId: null,
      occurredAt: "2026-08-08T09:30:00Z",
      payload: {},
      ...overrides,
    };
  }

  it("a created notification makes the badge and the feed stale", () => {
    const keys = staleKeysFor(event()).map((key) => JSON.stringify(key));

    expect(keys).toContain(JSON.stringify(["notifications", "summary"]));
    expect(keys).toContain(JSON.stringify(["notifications", "list"]));
  });

  it("reading in one tab makes the badge stale in another", () => {
    const keys = staleKeysFor(event({ event: "notification.read" })).map((key) =>
      JSON.stringify(key),
    );

    expect(keys).toContain(JSON.stringify(["notifications", "summary"]));
  });

  it("an account event invalidates nothing, deliberately", () => {
    // These exist so Notifications can tell the account holder what happened —
    // which the notification events above already invalidate. Revocation is the
    // server's job and is handled by the session, not by a second path here.
    expect(staleKeysFor(event({ event: "user.role_changed" }))).toEqual([]);
    expect(staleKeysFor(event({ event: "user.password_reset" }))).toEqual([]);
  });
});
