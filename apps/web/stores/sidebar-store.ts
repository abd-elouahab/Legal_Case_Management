import { create } from "zustand";

/**
 * Sidebar UI state.
 *
 * Client-side UI state only (no server/business data — that belongs to
 * TanStack Query). Two independent concerns:
 *
 * - `collapsed`  — desktop rail collapse (icon-only sidebar).
 * - `mobileOpen` — mobile drawer visibility (Sheet overlay).
 *
 * Zustand is used for cross-component UI state so the header trigger and the
 * sidebar can share it without prop drilling or a context wrapper.
 */
interface SidebarState {
  collapsed: boolean;
  mobileOpen: boolean;
  toggleCollapsed: () => void;
  setCollapsed: (value: boolean) => void;
  toggleMobile: () => void;
  setMobileOpen: (value: boolean) => void;
}

export const useSidebarStore = create<SidebarState>((set) => ({
  collapsed: false,
  mobileOpen: false,
  toggleCollapsed: () => set((state) => ({ collapsed: !state.collapsed })),
  setCollapsed: (value) => set({ collapsed: value }),
  toggleMobile: () => set((state) => ({ mobileOpen: !state.mobileOpen })),
  setMobileOpen: (value) => set({ mobileOpen: value }),
}));
