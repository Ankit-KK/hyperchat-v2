"use client";

import { createApiClient } from "@hyperchat/api-client";
import { API_BASE_URL } from "./api";

/** One client per browser tab, so the CSRF token from GET /auth/session stays in memory for later writes. */
export const browserApi = createApiClient({ baseUrl: API_BASE_URL });
