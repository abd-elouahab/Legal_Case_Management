import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  experimental: {
    // Rewrite barrel imports to direct submodule paths to trim client bundles
    // and speed up compilation for these icon/primitive packages.
    optimizePackageImports: ["radix-ui", "lucide-react"],
  },
  // Note: the root `/` redirect is handled by `proxy.ts`, not a config redirect,
  // because the destination now depends on whether a session exists
  // (`/dashboard` when signed in, `/login` otherwise).
};

export default nextConfig;
