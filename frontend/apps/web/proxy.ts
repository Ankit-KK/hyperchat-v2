import { NextResponse, type NextRequest } from "next/server";
import { API_ORIGIN } from "./lib/api";
import { securityHeaders } from "./lib/headers";
import { DEFAULT_HOST_MAP, parseHostMap, routeFor, zoneFor } from "./lib/hosts";

// Next.js 16 renamed middleware.ts to proxy.ts (same behaviour; it runs on the Node.js runtime).
const HOSTS = parseHostMap(process.env.WEB_HOST_MAP || DEFAULT_HOST_MAP);

export function proxy(request: NextRequest) {
  const zone = zoneFor(request.headers.get("host"), HOSTS);
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const headers = securityHeaders(zone, {
    nonce,
    apiOrigin: API_ORIGIN,
    dev: process.env.NODE_ENV === "development",
    https: request.nextUrl.protocol === "https:",
  });

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
