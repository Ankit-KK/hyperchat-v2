import { delay, http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createApiClient } from "./client";

const BASE = "https://api.test.invalid";
const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const client = (timeoutMs?: number) => createApiClient({ baseUrl: BASE + "/", timeoutMs });

describe("CSRF", () => {
  it("sends the token from GET /auth/session as x-csrf-token on writes, and never on reads", async () => {
    const seen: Record<string, string | null> = {};
    server.use(
      http.get(`${BASE}/api/v1/auth/session`, () =>
        HttpResponse.json({ user: { id: "u" }, csrf_token: "tok-1" }),
      ),
      http.get(`${BASE}/api/v1/dashboard/streamers/s/donations/stats`, ({ request }) => {
        seen.get = request.headers.get("x-csrf-token");
        return HttpResponse.json({});
      }),
      http.post(`${BASE}/api/v1/auth/logout`, ({ request }) => {
        seen.post = request.headers.get("x-csrf-token");
        return HttpResponse.json({ success: true });
      }),
    );
    const api = client();
    expect(api.hasCsrfToken()).toBe(false);
    expect((await api.session()).ok).toBe(true);
    await api.get("/api/v1/dashboard/streamers/{streamer}/donations/stats", { params: { streamer: "s" } });
    await api.post("/api/v1/auth/logout");
    expect(seen).toEqual({ get: null, post: "tok-1" });
  });

  it("takes the token from POST /auth/login and sends the credentials as JSON", async () => {
    let body: unknown;
    let csrf: string | null = null;
    server.use(
      http.post(`${BASE}/api/v1/auth/login`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ user: {}, csrf_token: "tok-2" });
      }),
      http.patch(`${BASE}/api/v1/dashboard/streamers/a%20b/settings`, ({ request }) => {
        csrf = request.headers.get("x-csrf-token");
        return HttpResponse.json({});
      }),
    );
    const api = client();
    await api.login("a@example.invalid", "pw");
    expect(body).toEqual({ email: "a@example.invalid", password: "pw" });
    await api.patch("/api/v1/dashboard/streamers/{streamer}/settings", {}, { params: { streamer: "a b" } });
    expect(csrf).toBe("tok-2");
  });

  it("forgets the token when the session answers 401", async () => {
    server.use(
      http.post(`${BASE}/api/v1/auth/login`, () => HttpResponse.json({ user: {}, csrf_token: "t" })),
      http.get(`${BASE}/api/v1/auth/session`, () =>
        HttpResponse.json({ error: "Not signed in" }, { status: 401 }),
      ),
    );
    const api = client();
    await api.login("a@example.invalid", "pw");
    expect(api.hasCsrfToken()).toBe(true);
    await api.session();
    expect(api.hasCsrfToken()).toBe(false);
  });
});

describe("results", () => {
  it("surfaces 401 as a typed result, not an exception or a redirect", async () => {
    server.use(
      http.get(`${BASE}/api/v1/auth/session`, () =>
        HttpResponse.json({ error: "Not signed in" }, { status: 401 }),
      ),
    );
    expect(await client().session()).toEqual({
      ok: false,
      error: { kind: "unauthorized", status: 401, error: "Not signed in", code: null },
    });
  });

  it("maps the API's { error, code } body and the HTTP status", async () => {
    server.use(
      http.get(`${BASE}/api/v1/public/streamers/x`, () =>
        HttpResponse.json({ error: "Creator not found", code: "not_found" }, { status: 404 }),
      ),
    );
    const result = await client().get("/api/v1/public/streamers/{slug}", { params: { slug: "x" } });
    expect(result).toEqual({
      ok: false,
      error: { kind: "http", status: 404, error: "Creator not found", code: "not_found" },
    });
  });

  it("maps a non-JSON error page (proxy 502) by status", async () => {
    server.use(
      http.get(
        `${BASE}/api/v1/public/config`,
        () => new HttpResponse("<html>Bad Gateway</html>", { status: 502 }),
      ),
    );
    expect(await client().get("/api/v1/public/config")).toEqual({
      ok: false,
      error: { kind: "http", status: 502, error: "Request failed (HTTP 502)", code: null },
    });
  });

  it("returns typed data on success", async () => {
    server.use(
      http.get(`${BASE}/api/v1/metrics/showcase`, () =>
        HttpResponse.json({ streamers_live: 3, alerts_last_24h: 10, last_sync: "2026-11-25T00:00:00Z" }),
      ),
    );
    const result = await client().get("/api/v1/metrics/showcase");
    expect(result.ok && result.data.streamers_live).toBe(3);
  });

  it("gives up after the timeout with kind: timeout", async () => {
    server.use(
      http.get(`${BASE}/api/v1/public/config`, async () => {
        await delay("infinite");
        return HttpResponse.json({});
      }),
    );
    const result = await client(50).get("/api/v1/public/config");
    expect(result).toEqual({ ok: false, error: { kind: "timeout", error: "No answer within 50 ms" } });
  });

  it("reports an unreachable API as kind: network", async () => {
    server.use(http.get(`${BASE}/api/v1/public/config`, () => HttpResponse.error()));
    const result = await client().get("/api/v1/public/config");
    expect(result).toEqual({ ok: false, error: { kind: "network", error: "The API could not be reached" } });
  });
});

describe("no automatic retry", () => {
  for (const method of ["post", "put", "patch", "delete"] as const) {
    it(`sends a failing ${method.toUpperCase()} exactly once (server error, then network error)`, async () => {
      let calls = 0;
      let fail: () => Response = () => HttpResponse.json({ error: "busy" }, { status: 503 });
      server.use(
        http[method](`${BASE}/api/v1/x`, () => {
          calls += 1;
          return fail();
        }),
      );
      const api = client() as unknown as Record<
        string,
        (path: string, body?: unknown) => Promise<{ ok: boolean }>
      >;
      const send = () => (method === "delete" ? api.delete!("/api/v1/x") : api[method]!("/api/v1/x", {}));
      expect((await send()).ok).toBe(false);
      fail = () => HttpResponse.error();
      expect((await send()).ok).toBe(false);
      expect(calls).toBe(2); // one call per send: neither failure was retried
    });
  }
});

describe("requests", () => {
  it("always sends credentials: include and refuses a missing path parameter", async () => {
    let credentials: RequestCredentials | undefined;
    const api = createApiClient({
      baseUrl: BASE,
      fetch: async (_url, init) => {
        credentials = init?.credentials;
        return new Response("{}", { status: 200 });
      },
    });
    await api.get("/api/v1/public/config");
    expect(credentials).toBe("include");
    await expect(api.get("/api/v1/public/streamers/{slug}")).rejects.toThrow("missing path parameter slug");
  });
});
