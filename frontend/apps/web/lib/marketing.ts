/**
 * The apex split (PDP v2.0 Phase 1 addendum §2.1, owner decision R2): this app owns hyperchat.space, and the
 * marketing website (github.com/Ankit-KK/hyperchat-frontend, a separate Vercel deployment) serves this fixed
 * allow-list, forwarded by the external rewrites in next.config.ts. Every other single-segment path is a creator
 * page. One list feeds the rewrites, the proxy's CSP exclusion and the creator page's reserved-name check.
 * A new marketing route needs a RESERVED_SLUGS entry in the backend first (addendum §2.1 rule 4).
 */

/** Exact paths: the site's routes (src/App.tsx), its redirects (vercel.json) and its root files. */
export const MARKETING_PATHS = [
  "/",
  "/features",
  "/creators",
  "/company",
  "/whats-new",
  "/demo-signup",
  "/viewer-terms",
  "/support",
  "/privacy-policy",
  "/terms-and-conditions",
  "/cancellation-refunds",
  "/feature-showcase",
  "/about",
  "/contact",
  "/shipping",
  "/favicon.svg",
  "/third-party-notices.txt",
  "/robots.txt",
  "/sitemap.xml",
  "/404.html",
] as const;

/** Folders forwarded with everything under them: public/ at 8080e5b, the legal archive and Vite's build output. */
export const MARKETING_FOLDERS = [
  "legal-archive",
  "creators",
  "images",
  "intro",
  "licenses",
  "mascot",
  "partners",
  "sculpture",
  "social",
  "assets",
] as const;

const PATHS = new Set<string>(MARKETING_PATHS);

/** True when the path belongs to the marketing site. Case-insensitive, as Next.js matches the rewrites. */
export function isMarketingPath(pathname: string): boolean {
  const path = pathname.toLowerCase();
  if (PATHS.has(path)) return true;
  const first = path.split("/")[1] ?? "";
  return (MARKETING_FOLDERS as readonly string[]).includes(first);
}

/** True when a creator slug would shadow a marketing page or folder: such a name is never a creator page. */
export function isMarketingName(slug: string): boolean {
  return isMarketingPath(`/${slug}`);
}

/** The external rewrites for next.config.ts, all `beforeFiles` so `/creators` never reaches app/(public)/[slug]. */
export function marketingRewrites(origin: string) {
  return [
    ...MARKETING_PATHS.map((path) => ({ source: path, destination: `${origin}${path}` })),
    ...MARKETING_FOLDERS.map((folder) => ({
      source: `/${folder}/:path*`,
      destination: `${origin}/${folder}/:path*`,
    })),
  ];
}

/** Local runs forward to the website's Vite dev server; see MARKETING_ORIGIN in apps/web/.env.example. */
export const LOCAL_MARKETING_ORIGIN = "http://localhost:5174";

/**
 * MARKETING_ORIGIN, validated: an https origin (no path, query or credentials). Required for the production
 * deployment; elsewhere, unset falls back to the local Vite dev server, and http is accepted only for localhost.
 */
export function marketingOrigin(value: string | undefined, vercelEnv: string | undefined): string {
  if (!value) {
    if (vercelEnv === "production")
      throw new Error("MARKETING_ORIGIN is required in the production deployment");
    return LOCAL_MARKETING_ORIGIN;
  }
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`MARKETING_ORIGIN is not a URL: ${value}`);
  }
  const local = url.protocol === "http:" && ["localhost", "127.0.0.1"].includes(url.hostname);
  if (
    (url.protocol !== "https:" && !local) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    url.pathname !== "/" ||
    value.replace(/\/$/, "") !== url.origin
  ) {
    throw new Error(`MARKETING_ORIGIN must be an https origin without a path: ${value}`);
  }
  return url.origin;
}
