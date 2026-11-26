import type { NextConfig } from "next";

// See apps/web/next.config.ts: the production API is never built into a non-production deployment.
const PRODUCTION_API = ["api", "hyperchat", "space"].join(".");
if (
  (process.env.NEXT_PUBLIC_API_BASE_URL ?? "").includes(PRODUCTION_API) &&
  process.env.VERCEL_ENV !== "production"
) {
  throw new Error(`NEXT_PUBLIC_API_BASE_URL names ${PRODUCTION_API} outside the production environment`);
}
// Security audit 2026-11-26: the production deployment needs its API base URL (no DEV default).
if (process.env.VERCEL_ENV === "production") {
  for (const name of ["NEXT_PUBLIC_API_BASE_URL"]) {
    if (!process.env[name]) throw new Error(`${name} is required in the production deployment`);
  }
}

const config: NextConfig = {
  poweredByHeader: false,
  reactStrictMode: true,
  transpilePackages: ["@hyperchat/ui"],
};

export default config;
