import { NextResponse, type NextRequest } from "next/server";
import { API_ORIGIN } from "./lib/api";
import { securityHeaders } from "./lib/headers";
import { DEFAULT_HOST_MAP, normalizeHost, parseHostMap, routeFor, wwwRedirect, zoneFor } from "./lib/hosts";
import { isMarketingPath } from "./lib/marketing";

// Next.js 16 renamed middleware.ts to proxy.ts (same behaviour; it runs on the Node.js runtime).
const HOSTS = parseHostMap(process.env.WEB_HOST_MAP || DEFAULT_HOST_MAP);

export function proxy(request: NextRequest) {
  const host = request.headers.get("host");
  const www = wwwRedirect(host, request.nextUrl);
  if (www) return NextResponse.redirect(www, 308);

  const zone = zoneFor(host, HOSTS);
  const known = !!host && HOSTS.has(normalizeHost(host));
  // The marketing allow-list is forwarded to the website (next.config.ts rewrites), which sets its own headers:
  // a nonce CSP would block its inline boot script. Only the unmapped-host noindex is set, and `next start` drops
  // even that on an external rewrite (checked 2026-11-26), so the website's own noindex covers preview hosts.
  if (zone === "public" && isMarketingPath(request.nextUrl.pathname)) {
    const response = NextResponse.next();
    if (!known) response.headers.set("X-Robots-Tag", "noindex");
    return response;
  }
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const headers = securityHeaders(zone, {
    nonce,
    apiOrigin: API_ORIGIN,
    dev: process.env.NODE_ENV === "development",
    https: request.nextUrl.protocol === "https:",
  });
  // An unmapped host (a *.vercel.app URL, a stray DNS name) serves the public site but must never be indexed.
  if (!known) headers["X-Robots-Tag"] = "noindex";

  const route = routeFor(zone, request.nextUrl.pathname);
  let response: NextResponse;
  if (route.kind === "notFound") {
    response = new NextResponse("Not Found", { status: 404, headers: { "content-type": "text/plain" } });
  } else {
    // Next.js reads the nonce from the request's CSP header and stamps it on the scripts it renders.
    const requestHeaders = new Headers(request.headers);
    requestHeaders.set("x-nonce", nonce);
    requestHeaders.set("content-security-policy", headers["Content-Security-Policy"]!);
    const init = { request: { headers: requestHeaders } };
    if (route.kind === "rewrite") {
      const url = request.nextUrl.clone();
      url.pathname = route.pathname;
      response = NextResponse.rewrite(url, init);
    } else {
      response = NextResponse.next(init);
    }
  }
  for (const [name, value] of Object.entries(headers)) response.headers.set(name, value);
  return response;
}

export const config = {
  // Every request but build assets: prefetches and RSC requests must be rewritten too, or client navigation on
  // auth., dashboard. or overlay. would load the public site's routes.
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
