/**
 * The admin app answers only on its own host (review §3.4, §5.6): admin.quicktestspace.site, admin.hyperchat.space,
 * admin.localhost for local runs, or ADMIN_HOSTS when set. A Vercel preview URL is accepted only in a preview
 * deployment, which the owner keeps behind Vercel deployment protection.
 */
export const DEFAULT_ADMIN_HOSTS = "admin.quicktestspace.site,admin.hyperchat.space,admin.localhost";

export function parseHosts(spec: string): Set<string> {
  return new Set(
    spec
      .split(",")
      .map((h) => h.trim().toLowerCase())
      .filter(Boolean),
  );
}

export function hostAllowed(
  host: string | null,
  allowed: Set<string>,
  vercelEnv: string | undefined,
): boolean {
  if (!host) return false;
  const bare = host.trim().toLowerCase().split(":")[0]!.replace(/\.$/, "");
  return allowed.has(bare) || (vercelEnv === "preview" && bare.endsWith(".vercel.app"));
}

/** Strict CSP: same-origin only, scripts and <style> by nonce, no inline style attributes, no third-party origin. */
export function adminHeaders(
  nonce: string,
  apiOrigin: string,
  { dev, https }: { dev: boolean; https: boolean },
) {
  const csp = [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ""}`,
    // Dev only: Next's dev overlay injects <style> without the nonce (and a nonce would void 'unsafe-inline').
    dev ? "style-src 'self' 'unsafe-inline'" : `style-src 'self' 'nonce-${nonce}'`,
    "img-src 'self' data:",
    "font-src 'self'",
    `connect-src 'self' ${apiOrigin}`,
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    ...(https ? ["upgrade-insecure-requests"] : []),
  ].join("; ");
  return {
    "Content-Security-Policy": csp,
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
  };
}
