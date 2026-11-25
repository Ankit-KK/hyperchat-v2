import { NextResponse, type NextRequest } from "next/server";
import { adminHeaders, DEFAULT_ADMIN_HOSTS, hostAllowed, parseHosts } from "./lib/policy";

// Next.js 16 renamed middleware.ts to proxy.ts (same behaviour; it runs on the Node.js runtime).
const HOSTS = parseHosts(process.env.ADMIN_HOSTS || DEFAULT_ADMIN_HOSTS);
const API_ORIGIN = new URL(process.env.NEXT_PUBLIC_API_BASE_URL || "https://api.quicktestspace.site").origin;

export function proxy(request: NextRequest) {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const headers = adminHeaders(nonce, API_ORIGIN, {
    dev: process.env.NODE_ENV === "development",
    https: request.nextUrl.protocol === "https:",
  });
  let response: NextResponse;
  if (!hostAllowed(request.headers.get("host"), HOSTS, process.env.VERCEL_ENV)) {
    response = new NextResponse("Not Found", { status: 404, headers: { "content-type": "text/plain" } });
  } else {
    const requestHeaders = new Headers(request.headers);
    requestHeaders.set("x-nonce", nonce);
    requestHeaders.set("content-security-policy", headers["Content-Security-Policy"]);
    response = NextResponse.next({ request: { headers: requestHeaders } });
  }
  for (const [name, value] of Object.entries(headers)) response.headers.set(name, value);
  return response;
}

export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"] };
