import { defineConfig } from "@playwright/test";

/**
 * One project per host (review §8.2). Not run in CI yet: it needs deployed dev hosts (Phase 3) or both apps running
 * locally. Base URLs default to the dev hosts; override each with E2E_<PROJECT>_URL, e.g. for a local run:
 *   E2E_PUBLIC_URL=http://localhost:3000 E2E_DASHBOARD_URL=http://dashboard.localhost:3000 pnpm --filter @hyperchat/e2e e2e
 */
const host = (name: string, fallback: string) => process.env[`E2E_${name.toUpperCase()}_URL`] ?? fallback;

export default defineConfig({
  testDir: "./tests",
  retries: 0,
  reporter: "list",
  projects: [
    {
      name: "public",
      testMatch: "public.spec.ts",
      use: { baseURL: host("public", "https://quicktestspace.site") },
    },
    {
      name: "auth",
      testMatch: "auth.spec.ts",
      use: { baseURL: host("auth", "https://auth.quicktestspace.site") },
    },
    {
      name: "dashboard",
      testMatch: "dashboard.spec.ts",
      use: { baseURL: host("dashboard", "https://dashboard.quicktestspace.site") },
    },
    {
      name: "overlay",
      testMatch: "overlay.spec.ts",
      use: { baseURL: host("overlay", "https://overlay.quicktestspace.site") },
    },
    {
      name: "admin",
      testMatch: "admin.spec.ts",
      use: { baseURL: host("admin", "https://admin.quicktestspace.site") },
    },
  ],
});
