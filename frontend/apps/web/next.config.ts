import type { NextConfig } from "next";

// The production API may only be built into the production deployment (frontend/README.md). The host name is
// assembled so that the source-tree guard (scripts/guard-prod-api.mjs) can forbid it as a literal everywhere.
const PRODUCTION_API = ["api", "hyperchat", "space"].join(".");
if (
  (process.env.NEXT_PUBLIC_API_BASE_URL ?? "").includes(PRODUCTION_API) &&
  process.env.VERCEL_ENV !== "production"
) {
  throw new Error(`NEXT_PUBLIC_API_BASE_URL names ${PRODUCTION_API} outside the production environment`);
}

const config: NextConfig = {
  poweredByHeader: false,
  reactStrictMode: true,
  transpilePackages: ["@hyperchat/api-client", "@hyperchat/ui"],
};

export default config;
