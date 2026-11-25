import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";
import { proxy } from "../proxy";
import { securityHeaders } from "./headers";
import { DEFAULT_HOST_MAP, normalizeHost, parseHostMap, routeFor, zoneFor } from "./hosts";

const map = parseHostMap(DEFAULT_HOST_MAP);

describe("host resolution", () => {
  it.each([
    ["quicktestspace.site", "public"],
    ["auth.quicktestspace.site", "auth"],
    ["DASHBOARD.quicktestspace.site:443", "dashboard"],
    ["overlay.quicktestspace.site.", "overlay"],
    ["auth.hyperchat.space", "auth"],
    ["dashboard.localhost:3000", "dashboard"],
    ["admin.quicktestspace.site", "public"], // the admin app is a separate deploy; here it is just unknown
    ["my-preview-abc.vercel.app", "public"],
    ["evil.example", "public"],
    ["[::1]:3000", "public"],
  ])("%s -> %s", (host, zone) => {
    expect(zoneFor(host, map)).toBe(zone);
  });

  it("defaults to public without a Host header", () => {
    expect(zoneFor(null, map)).toBe("public");
  });

  it("normalizes case, port, IPv6 brackets and the trailing dot", () => {
    expect(normalizeHost(" Auth.Example.COM.:8443 ")).toBe("auth.example.com");
    expect(normalizeHost("[::1]:3000")).toBe("[::1]");
  });

  it("refuses a malformed or unknown-zone map so a bad env var fails the deploy", () => {
    expect(() => parseHostMap("a.example=admin")).toThrow("bad WEB_HOST_MAP entry");
    expect(() => parseHostMap("a.example")).toThrow();
    expect(parseHostMap(" a.example = auth , ").get("a.example")).toBe("auth");
  });
});

describe("routes", () => {
  it("rewrites non-public zones under their internal segment", () => {
    expect(routeFor("dashboard", "/")).toEqual({ kind: "rewrite", pathname: "/dashboard" });
    expect(routeFor("overlay", "/alerts")).toEqual({ kind: "rewrite", pathname: "/obs/alerts" });
    expect(routeFor("public", "/tsukii")).toEqual({ kind: "next" });
  });

  it.each(["/auth", "/dashboard/x", "/OBS", "/obs/alerts"])(
    "blocks the internal path %s on every zone",
    (path) => {
      for (const zone of ["public", "auth", "dashboard", "overlay"] as const) {
        expect(routeFor(zone, path)).toEqual({ kind: "notFound" });
      }
    },
  );

  it("does not block slugs that merely start like an internal segment", () => {
    expect(routeFor("public", "/authentic")).toEqual({ kind: "next" });
    expect(routeFor("public", "/obsidian")).toEqual({ kind: "next" });
  });
});

describe("headers", () => {
  const opts = { nonce: "n0nce", apiOrigin: "https://api.quicktestspace.site", dev: false, https: true };

  it("marks every host but public noindex, and auth no-referrer", () => {
    expect(securityHeaders("public", opts)["X-Robots-Tag"]).toBeUndefined();
    for (const zone of ["auth", "dashboard", "overlay"] as const) {
      expect(securityHeaders(zone, opts)["X-Robots-Tag"]).toBe("noindex");
    }
    expect(securityHeaders("auth", opts)["Referrer-Policy"]).toBe("no-referrer");
    expect(securityHeaders("dashboard", opts)["Referrer-Policy"]).toBe("strict-origin-when-cross-origin");
  });

  it("allows scripts only by nonce, connects only to the API, and is never framed", () => {
    const csp = securityHeaders("dashboard", opts)["Content-Security-Policy"]!;
    expect(csp).toContain("script-src 'self' 'nonce-n0nce' 'strict-dynamic';");
    expect(csp).not.toContain("unsafe-eval");
    expect(csp).toContain("connect-src 'self' https://api.quicktestspace.site;");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(securityHeaders("public", { ...opts, dev: true })["Content-Security-Policy"]).toContain(
      "'unsafe-eval'",
    );
  });
});

describe("proxy", () => {
  const call = (url: string) => proxy(new NextRequest(url, { headers: { host: new URL(url).host } }));

  it("rewrites the dashboard host and sets its headers", () => {
    const res = call("https://dashboard.quicktestspace.site/");
    expect(res.headers.get("x-middleware-rewrite")).toBe("https://dashboard.quicktestspace.site/dashboard");
    expect(res.headers.get("x-robots-tag")).toBe("noindex");
    expect(res.headers.get("content-security-policy")).toMatch(/'nonce-[A-Za-z0-9+/=]+'/);
  });

  it("answers 404 for an internal path on the public host", () => {
    const res = call("https://quicktestspace.site/dashboard");
    expect(res.status).toBe(404);
    expect(res.headers.get("x-middleware-rewrite")).toBeNull();
  });

  it("marks an unmapped host noindex even though it serves the public site", () => {
    expect(call("https://my-app-git-main.vercel.app/").headers.get("x-robots-tag")).toBe("noindex");
    expect(call("https://quicktestspace.site/").headers.get("x-robots-tag")).toBeNull();
  });

  it("passes the public host through with a fresh nonce per request", () => {
    const a = call("https://quicktestspace.site/tsukii").headers.get("content-security-policy");
    const b = call("https://quicktestspace.site/tsukii").headers.get("content-security-policy");
    expect(a).not.toBe(b);
  });
});
