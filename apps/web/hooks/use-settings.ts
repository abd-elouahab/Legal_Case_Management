"use client";

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { NetworkError } from "@/lib/api/errors";
import { useErrorMessage, type ErrorCodeMap } from "@/hooks/use-error-message";
import {
  changeSettingsPassword,
  fetchPlatformSettings,
  fetchSessions,
  fetchSettings,
  fetchSettingsMetrics,
  fetchSettingsOverview,
  revokeOtherSessions,
  updatePlatformSettings,
  updateProfile,
  updateSettings,
} from "@/lib/api/settings";
import { useSessionStore } from "@/stores/session-store";
import type {
  Profile,
  ProfileUpdate,
  SessionListing,
  SettingChange,
  SettingsCollection,
  SettingsMetrics,
  SettingsOverview,
} from "@/types/settings";

/**
 * Server state for settings.
 *
 * TanStack Query per `architecture.md`: a setting is server state, so it is
 * cached and invalidated rather than mirrored into a client store. No business
 * logic lives in components — these hooks are the only place the UI talks to the
 * settings API.
 *
 * **Nothing here polls, and that is the one interesting thing about the caching.**
 * Every other feature on this platform polls because somebody *else* can change
 * what it shows: a case is updated by a colleague, a report finishes on a worker,
 * a notification arrives from an event. A setting is changed by exactly one
 * person — the one looking at the page — so a background refetch would only ever
 * confirm what this tab already did. The one exception is the platform's
 * maintenance posture, which an administrator changes for everybody; it rides on
 * the overview query and is re-read when the page is opened.
 *
 * **Two mutations swap the caller's access token**, which is why they invalidate
 * more than they change: a password change and "sign out everywhere else" both
 * end every other session, so the sessions list is stale the moment either
 * returns. `lib/api/settings.ts` puts the replacement token in place before the
 * promise resolves, so nothing here has to know that happened.
 */

/** Query keys. */
export const settingsKeys = {
  all: ["settings"] as const,
  overview: () => [...settingsKeys.all, "overview"] as const,
  preferences: () => [...settingsKeys.all, "preferences"] as const,
  sessions: () => [...settingsKeys.all, "sessions"] as const,
  platform: () => [...settingsKeys.all, "platform"] as const,
  metrics: () => [...settingsKeys.all, "metrics"] as const,
};

/**
 * Translate a failure into a sentence in the reader's language.
 *
 * Branches on the API's machine-readable `code` rather than on message text —
 * which the server writes in English, with no knowledge of who is reading it.
 * `hooks/use-error-message.ts` records why that matters; the short version is
 * that an interface which is Arabic everywhere except when something goes wrong
 * is not localized. Codes with no entry here fall through to the shared
 * `errors.*` sentences and then to a generic one.
 */
const SETTING_ERRORS: ErrorCodeMap = {
  invalid_setting_value: "invalidValue",
  unknown_setting: "invalidValue",
  validation_error: "invalidValue",
  invalid_password: "wrongPassword",
  authorization_error: "forbidden",
  missing_token: "sessionExpired",
  service_unavailable: "unavailable",
};

export function useSettingsErrorMessage(): (error: unknown) => string {
  return useErrorMessage("settings.errors", SETTING_ERRORS);
}

/** Everything the Settings page needs on first load, in one request. */
export function useSettingsOverview(): UseQueryResult<SettingsOverview, unknown> {
  return useQuery({
    queryKey: settingsKeys.overview(),
    queryFn: fetchSettingsOverview,
  });
}

/**
 * Change the caller's own profile.
 *
 * Invalidates the whole settings tree rather than only the profile: the display
 * name appears in the overview response too, and two copies of it disagreeing is
 * exactly the sort of thing nobody notices until they do.
 */
export function useUpdateProfile(): UseMutationResult<Profile, unknown, ProfileUpdate> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: updateProfile,
    onSuccess: (profile) => {
      void queryClient.invalidateQueries({ queryKey: settingsKeys.all });
      // The session store carries the display name into the header and the
      // sidebar, so a rename that only refreshed this page would leave the old
      // name in the corner of every other one. The store is zustand rather than
      // a query — the access token deliberately never enters React state — so it
      // is written directly rather than invalidated.
      const { user, setSession } = useSessionStore.getState();
      if (user && user.id === profile.id) {
        setSession({ ...user, name: profile.fullName });
      }
    },
  });
}

/** Every setting the platform offers, with the caller's answer to each. */
export function useSettings(): UseQueryResult<SettingsCollection, unknown> {
  return useQuery({
    queryKey: settingsKeys.preferences(),
    queryFn: fetchSettings,
  });
}

/**
 * Set some of the caller's settings.
 *
 * The response is the **complete** set, so it is written straight into both
 * caches rather than triggering a refetch: the server has already told us the
 * answer, and asking again would be a round trip to learn what we are holding.
 */
export function useUpdateSettings(): UseMutationResult<
  SettingsCollection,
  unknown,
  SettingChange[]
> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: updateSettings,
    onSuccess: (collection) => {
      queryClient.setQueryData(settingsKeys.preferences(), collection);
      queryClient.setQueryData(
        settingsKeys.overview(),
        (previous: SettingsOverview | undefined) =>
          previous ? { ...previous, settings: collection } : previous,
      );
    },
  });
}

/** The caller's live sign-ins. */
export function useSessions(): UseQueryResult<SessionListing, unknown> {
  return useQuery({
    queryKey: settingsKeys.sessions(),
    queryFn: fetchSessions,
  });
}

/** Sign out of every session except this one. */
export function useRevokeOtherSessions(): UseMutationResult<string, unknown, void> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: () => revokeOtherSessions(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: settingsKeys.sessions() });
    },
  });
}

/**
 * Change the caller's password.
 *
 * Invalidates the sessions list, because a password change signs every other
 * device out — the list on screen is stale the moment this returns, and a
 * security page showing sessions that no longer exist is worse than one that is a
 * second late.
 *
 * Named apart from `hooks/use-change-password.ts`, which is the **authentication**
 * feature's hook: that one drives the forced-change screen an administrator reset
 * sends somebody to, before the application shell has loaded, and it holds its own
 * local state because there is no query cache around it yet. This one is the
 * Settings surface — same service method on the API, different page.
 */
export function useChangeSettingsPassword(): UseMutationResult<
  string,
  unknown,
  { currentPassword: string; newPassword: string }
> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: changeSettingsPassword,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: settingsKeys.sessions() });
      void queryClient.invalidateQueries({ queryKey: settingsKeys.overview() });
      // `must_change_password` is cleared by this call and the session store
      // carries it — an account that arrived through an administrator reset would
      // otherwise keep being told to change a password it just changed.
      const { user, setSession } = useSessionStore.getState();
      if (user?.mustChangePassword) {
        setSession({ ...user, mustChangePassword: false });
      }
    },
  });
}

/** The deployment's configuration. Requires `settings:manage`. */
export function usePlatformSettings(
  enabled: boolean,
): UseQueryResult<SettingsCollection, unknown> {
  return useQuery({
    queryKey: settingsKeys.platform(),
    queryFn: fetchPlatformSettings,
    // Gated by the caller rather than fetched-and-refused: a query that 403s on
    // every render for every non-administrator would fill an error log with
    // policy working correctly.
    enabled,
  });
}

/**
 * Set some of the deployment's configuration.
 *
 * Also invalidates the **overview**, because platform settings are the defaults
 * every account that has expressed no opinion follows — an administrator changing
 * the default language has changed what their own settings page should show for
 * every value they have not personally chosen.
 */
export function useUpdatePlatformSettings(): UseMutationResult<
  SettingsCollection,
  unknown,
  SettingChange[]
> {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: updatePlatformSettings,
    onSuccess: (collection) => {
      queryClient.setQueryData(settingsKeys.platform(), collection);
      void queryClient.invalidateQueries({ queryKey: settingsKeys.overview() });
    },
  });
}

/** Platform-wide settings health. Requires `settings:monitor`. */
export function useSettingsMetrics(
  enabled: boolean,
): UseQueryResult<SettingsMetrics, unknown> {
  return useQuery({
    queryKey: settingsKeys.metrics(),
    queryFn: fetchSettingsMetrics,
    enabled,
  });
}
