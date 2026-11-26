/**
 * Host routing for apps/web (review §3.1, §9.3): one deploy serves the public site, auth., dashboard. and overlay.
 * The Host header picks a zone; every zone except public lives under an internal path segment that the proxy
 * rewrites to. The segments are reserved creator slugs (backend/app/admin/service.py RESERVED_SLUGS), so no creator
 * page can ever collide with them, and the proxy answers 404 when one is requested directly on any host.
 */
export type Zone = "public" | "auth" | "dashboard" | "overlay";

export const ZONE_SEGMENT: Record<Exclude<Zone, "public">, string> = {
  auth: "auth",
  dashboard: "dashboard",
  overlay: "obs",
};

/** Used when WEB_HOST_MAP is unset: the dev hosts (review §9.3), the production hosts, and *.localhost for local runs. */
export const DEFAULT_HOST_MAP = [
  "quicktestspace.site=public",
  "app.quicktestspace.site=public",
  "auth.quicktestspace.site=auth",
  "dashboard.quicktestspace.site=dashboard",
  "overlay.quicktestspace.site=overlay",
  "hyperchat.space=public",
  "auth.hyperchat.space=auth",
  "dashboard.hyperchat.space=dashboard",
  "overlay.hyperchat.space=overlay",
  "localhost=public",
  "auth.localhost=auth",
  "dashboard.localhost=dashboard",
  "overlay.localhost=overlay",
].join(",");

/** www hosts are not served: they redirect permanently to their apex, the canonical host (owner decision R3). */
export const WWW_REDIRECTS = new Map([
  ["www.hyperchat.space", "hyperchat.space"],
  ["www.quicktestspace.site", "quicktestspace.site"],
]);

/** The https apex URL a www request redirects to (path and query kept), or null for any other host. */
export function wwwRedirect(host: string | null, url: URL): string | null {
  const apex = host && WWW_REDIRECTS.get(normalizeHost(host));
  return apex ? `https://${apex}${url.pathname}${url.search}` : null;
}

const ZONES = new Set<Zone>(["public", "auth", "dashboard", "overlay"]);
const INTERNAL = new RegExp(`^/(${Object.values(ZONE_SEGMENT).join("|")})(/|$)`, "i");

/** Parses "host=zone,host=zone"; a malformed entry or unknown zone throws, so a bad env var fails loudly. */
export function parseHostMap(spec: string): Map<string, Zone> {
  const map = new Map<string, Zone>();
  for (const entry of spec
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)) {
    const [host, zone, ...rest] = entry.split("=").map((s) => s.trim());
    if (!host || !zone || rest.length || !ZONES.has(zone as Zone))
      throw new Error(`bad WEB_HOST_MAP entry: ${entry}`);
    map.set(normalizeHost(host), zone as Zone);
  }
  return map;
}

/** Lower case, no port, no trailing dot. */
export function normalizeHost(host: string): string {
  const h = host.trim().toLowerCase();
  const bare = h.startsWith("[") ? h.slice(0, h.indexOf("]") + 1) : h.split(":")[0]!;
  return bare.replace(/\.$/, "");
}

/** Unknown or missing hosts (Vercel preview URLs included) get the public site: the zone with the least privilege. */
export function zoneFor(host: string | null, map: Map<string, Zone>): Zone {
  return (host && map.get(normalizeHost(host))) || "public";
}

export type Route = { kind: "notFound" } | { kind: "next" } | { kind: "rewrite"; pathname: string };

export function routeFor(zone: Zone, pathname: string): Route {
  if (INTERNAL.test(pathname)) return { kind: "notFound" }; // never reachable by its internal path, on any host
  if (zone === "public") return { kind: "next" };
  return { kind: "rewrite", pathname: `/${ZONE_SEGMENT[zone]}${pathname === "/" ? "" : pathname}` };
}
