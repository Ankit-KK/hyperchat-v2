import { createApiClient, DEV_API_BASE_URL } from "@hyperchat/api-client";

/** Inlined at build time. Every non-production build uses the DEV API (frontend/README.md). */
export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL || DEV_API_BASE_URL;
export const API_ORIGIN = new URL(API_BASE_URL).origin;

/** For server components: public, anonymous reads only (the session cookie never reaches this host). */
export function serverApi() {
  return createApiClient({ baseUrl: API_BASE_URL, timeoutMs: 5_000, init: { cache: "no-store" } });
}
