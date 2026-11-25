import type { Zone } from "./hosts";

export type HeaderOptions = { nonce: string; apiOrigin: string; dev: boolean; https: boolean };

/**
 * Response headers per zone. Scripts need the per-request nonce ('strict-dynamic' lets Next's own chunks load).
 * Style attributes are allowed (creator colours, overlay animations); <style> elements need the nonce.
 * img-src allows any https origin until the public asset host is fixed (Phase 4, review §9.2 assets.).
 */
export function securityHeaders(
  zone: Zone,
  { nonce, apiOrigin, dev, https }: HeaderOptions,
): Record<string, string> {
  const csp = [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ""}`,
    `style-src 'self' 'nonce-${nonce}'`,
    "style-src-attr 'unsafe-inline'",
    "img-src 'self' data: blob: https:",
    "font-src 'self'",
    `connect-src 'self' ${apiOrigin}`,
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    ...(https ? ["upgrade-insecure-requests"] : []),
  ].join("; ");
  const headers: Record<string, string> = {
    "Content-Security-Policy": csp,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": zone === "auth" ? "no-referrer" : "strict-origin-when-cross-origin",
  };
  // SEO policy: only the public host (landing and active creator pages) may be indexed.
  if (zone !== "public") headers["X-Robots-Tag"] = "noindex";
  return headers;
}
