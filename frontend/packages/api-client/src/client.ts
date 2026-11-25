import type { components, paths } from "./schema";

/** The DEV API. Production builds set NEXT_PUBLIC_API_BASE_URL; nothing here may name the production API host. */
export const DEV_API_BASE_URL = "https://api.quicktestspace.site";
export const DEFAULT_TIMEOUT_MS = 10_000;

export type Schemas = components["schemas"];
type Method = "get" | "post" | "put" | "patch" | "delete";
type PathWith<M extends Method> = {
  [P in keyof paths]: paths[P] extends Record<M, object> ? P : never;
}[keyof paths];
type Op<P extends keyof paths, M extends Method> = paths[P] extends Record<M, infer O> ? O : never;
type Json<R> = R extends { content: { "application/json": infer B } } ? B : unknown;
type Success<O> = O extends { responses: infer R }
  ? Json<R extends { 200: infer S } ? S : R extends { 201: infer S } ? S : never>
  : never;
type PathParams<O> = O extends { parameters: { path: infer Q } } ? Q : never;
type Body<O> = O extends { requestBody?: { content: { "application/json": infer B } } } ? B : never;

/**
 * Why a request did not succeed. The API's error bodies are `{ error, code? }`; anything else (an HTML 502 from
 * the proxy, a 422 `{ detail }`) still yields `kind: "http"` with the status and a generic message.
 */
export type ApiError =
  | { kind: "unauthorized"; status: 401; error: string; code: string | null }
  | { kind: "http"; status: number; error: string; code: string | null }
  | { kind: "timeout"; error: string }
  | { kind: "network"; error: string };

export type ApiResult<T> = { ok: true; status: number; data: T } | { ok: false; error: ApiError };

/**
 * `GET /auth/session` and `POST /auth/login` return untyped dicts in the OpenAPI document (the routers declare
 * `-> dict`), so their shape is written here by hand from backend/app/routers/auth.py. Replace with the generated
 * types once the backend declares response models.
 */
export type SignedIn = { user: Record<string, unknown>; csrf_token: string };

export type ClientOptions = {
  baseUrl: string;
  timeoutMs?: number;
  fetch?: typeof fetch;
  /** Extra fetch options for every request, e.g. `{ cache: "no-store" }` in a Next.js server component. */
  init?: RequestInit;
};

export type RequestOptions = { params?: Record<string, string>; signal?: AbortSignal; init?: RequestInit };

const WRITES = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export function createApiClient({
  baseUrl,
  timeoutMs = DEFAULT_TIMEOUT_MS,
  fetch: fetchImpl,
  init,
}: ClientOptions) {
  const base = baseUrl.replace(/\/+$/, "");
  let csrfToken: string | null = null; // memory only: never localStorage (frontend/README.md)

  /** One attempt, never retried: a repeated POST could duplicate a payment or an action (PDP §19). */
  async function request<T>(method: string, path: string, body?: unknown, opts: RequestOptions = {}) {
    const url =
      base +
      path.replace(/\{(\w+)\}/g, (_, name: string) => {
        const value = opts.params?.[name];
        if (value === undefined) throw new Error(`missing path parameter ${name} for ${path}`);
        return encodeURIComponent(value);
      });
    const headers = new Headers(init?.headers);
    new Headers(opts.init?.headers).forEach((value, key) => headers.set(key, value));
    headers.set("accept", "application/json");
    if (body !== undefined) headers.set("content-type", "application/json");
    if (WRITES.has(method) && csrfToken) headers.set("x-csrf-token", csrfToken);

    const timeout = AbortSignal.timeout(timeoutMs);
    const signal = opts.signal ? AbortSignal.any([opts.signal, timeout]) : timeout;
    let response: Response;
    try {
      response = await (fetchImpl ?? fetch)(url, {
        ...init,
        ...opts.init,
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "include",
        signal,
      });
    } catch (cause) {
      if (timeout.aborted) return fail({ kind: "timeout", error: `No answer within ${timeoutMs} ms` });
      if (opts.signal?.aborted) throw cause; // the caller cancelled: not an API outcome
      return fail({ kind: "network", error: "The API could not be reached" });
    }

    const parsed: unknown = await response.json().catch(() => undefined);
    if (response.ok) return { ok: true, status: response.status, data: parsed as T } as const;
    const fields = typeof parsed === "object" && parsed !== null ? (parsed as Record<string, unknown>) : {};
    const error =
      typeof fields.error === "string" ? fields.error : `Request failed (HTTP ${response.status})`;
    const code = typeof fields.code === "string" ? fields.code : null;
    if (response.status === 401) return fail({ kind: "unauthorized", status: 401, error, code });
    return fail({ kind: "http", status: response.status, error, code });
  }

  function fail(error: ApiError) {
    return { ok: false, error } as const;
  }

  async function remember(result: ApiResult<SignedIn>): Promise<ApiResult<SignedIn>> {
    if (result.ok) csrfToken = result.data.csrf_token;
    else if (result.error.kind === "unauthorized") csrfToken = null;
    return result;
  }

  return {
    get<P extends PathWith<"get">>(path: P, opts?: RequestOptions & { params?: PathParams<Op<P, "get">> }) {
      return request<Success<Op<P, "get">>>("GET", path, undefined, opts) as Promise<
        ApiResult<Success<Op<P, "get">>>
      >;
    },
    post<P extends PathWith<"post">>(path: P, body?: Body<Op<P, "post">>, opts?: RequestOptions) {
      return request<Success<Op<P, "post">>>("POST", path, body, opts) as Promise<
        ApiResult<Success<Op<P, "post">>>
      >;
    },
    put<P extends PathWith<"put">>(path: P, body?: Body<Op<P, "put">>, opts?: RequestOptions) {
      return request<Success<Op<P, "put">>>("PUT", path, body, opts) as Promise<
        ApiResult<Success<Op<P, "put">>>
      >;
    },
    patch<P extends PathWith<"patch">>(path: P, body?: Body<Op<P, "patch">>, opts?: RequestOptions) {
      return request<Success<Op<P, "patch">>>("PATCH", path, body, opts) as Promise<
        ApiResult<Success<Op<P, "patch">>>
      >;
    },
    delete<P extends PathWith<"delete">>(path: P, opts?: RequestOptions) {
      return request<Success<Op<P, "delete">>>("DELETE", path, undefined, opts) as Promise<
        ApiResult<Success<Op<P, "delete">>>
      >;
    },
    /** Signs in; the API sets its own HttpOnly cookie and returns the CSRF token kept for later writes. */
    async login(email: string, password: string) {
      return remember(
        (await request<SignedIn>("POST", "/api/v1/auth/login", { email, password })) as ApiResult<SignedIn>,
      );
    },
    /** The session bootstrap: who is signed in, plus a fresh CSRF token. 401 means signed out. */
    async session() {
      return remember((await request<SignedIn>("GET", "/api/v1/auth/session")) as ApiResult<SignedIn>);
    },
    hasCsrfToken: () => csrfToken !== null,
  };
}

export type ApiClient = ReturnType<typeof createApiClient>;
