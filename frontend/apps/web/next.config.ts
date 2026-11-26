import type { NextConfig } from "next";
import { marketingOrigin, marketingRewrites } from "./lib/marketing";

// The production API may only be built into the production deployment (frontend/README.md). The host name is
// assembled so that the source-tree guard (scripts/guard-prod-api.mjs) can forbid it as a literal everywhere.
const PRODUCTION_API = ["api", "hyperchat", "space"].join(".");
if (
  (process.env.NEXT_PUBLIC_API_BASE_URL ?? "").includes(PRODUCTION_API) &&
  process.env.VERCEL_ENV !== "production"
) {
  throw new Error(`NEXT_PUBLIC_API_BASE_URL names ${PRODUCTION_API} outside the production environment`);
}
// Security audit 2026-11-26: the production deployment needs its API (no DEV default) and media base (share card).
if (process.env.VERCEL_ENV === "production") {
  for (const name of ["NEXT_PUBLIC_API_BASE_URL", "HYPERCHAT_MEDIA_BASE_URL"]) {
    if (!process.env[name]) throw new Error(`${name} is required in the production deployment`);
  }
}

// Build time: the marketing website's deployment that serves the apex allow-list (lib/marketing.ts).
const MARKETING_ORIGIN = marketingOrigin(process.env.MARKETING_ORIGIN, process.env.VERCEL_ENV);

const config: NextConfig = {
  poweredByHeader: false,
  reactStrictMode: true,
  transpilePackages: ["@hyperchat/api-client", "@hyperchat/ui"],
  // beforeFiles: checked before the app's routes, after the proxy (which skips the nonce CSP for these paths).
  async rewrites() {
    return { beforeFiles: marketingRewrites(MARKETING_ORIGIN), afterFiles: [], fallback: [] };
  },
};

export default config;
