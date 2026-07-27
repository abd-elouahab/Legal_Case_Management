import path from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/**
 * Vitest configuration for frontend tests.
 *
 * Uses jsdom so React components can be rendered and interacted with, and mirrors
 * the `@/*` path alias from `tsconfig.json` so imports match application code.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, ".") },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
    include: ["tests/**/*.test.{ts,tsx}"],
    // Next.js build output and dependencies are never test targets.
    exclude: ["node_modules/**", ".next/**"],
    restoreMocks: true,
    clearMocks: true,
  },
});
