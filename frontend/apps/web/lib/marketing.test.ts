import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";
import { proxy } from "../proxy";
import { wwwRedirect } from "./hosts";
import {
  isMarketingName,
  isMarketingPath,
  LOCAL_MARKETING_ORIGIN,
  MARKETING_FOLDERS,
  MARKETING_PATHS,
  marketingOrigin,
  marketingRewrites,
} from "./marketing";

// Addendum §2.1, written out again on purpose: the test fails if the module's list drifts from the decision.
const ADDENDUM_PATHS = [
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
];
const ADDENDUM_FOLDERS = [
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
];

describe("marketing allow-list", () => {
  it("is exactly the addendum's list", () => {
    expect([...MARKETING_PATHS].sort()).toEqual([...ADDENDUM_PATHS].sort());
    expect([...MARKETING_FOLDERS].sort()).toEqual([...ADDENDUM_FOLDERS].sort());
  });

  it.each(ADDENDUM_PATHS)("forwards %s", (path) => {
    expect(isMarketingPath(path)).toBe(true);
  });

  it.each(ADDENDUM_FOLDERS.flatMap((f) => [`/${f}`, `/${f}/a.webp`, `/${f}/deep/nested/file.mp4`]))(
    "forwards %s",
    (path) => expect(isMarketingPath(path)).toBe(true),
  );

  it("matches case-insensitively, as Next.js applies the rewrites (observed with next start)", () => {
    expect(isMarketingPath("/Features")).toBe(true);
    expect(isMarketingPath("/ASSETS/x.js")).toBe(true);
  });

  it.each(["/tsukii", "/captain_vaani", "/features-x", "/creatorsx", "/assetsy/a", "/sitemap", "/supports"])(
    "leaves the creator path %s to the app",
    (path) => expect(isMarketingPath(path)).toBe(false),
  );

  it("never treats a marketing name as a creator slug", () => {
    for (const name of [...ADDENDUM_PATHS.map((p) => p.slice(1)).filter(Boolean), ...ADDENDUM_FOLDERS]) {
      expect(isMarketingName(name)).toBe(true);
    }
    expect(isMarketingName("tsukii")).toBe(false);
  });

  it("builds one rewrite per path and one catch-all per folder, all to the origin", () => {
    const rewrites = marketingRewrites("https://site.example");
    expect(rewrites).toHaveLength(ADDENDUM_PATHS.length + ADDENDUM_FOLDERS.length);
    expect(rewrites).toContainEqual({ source: "/", destination: "https://site.example/" });
    expect(rewrites).toContainEqual({
      source: "/assets/:path*",
      destination: "https://site.example/assets/:path*",
    });
    for (const { destination } of rewrites)
      expect(destination.startsWith("https://site.example/")).toBe(true);
  });
});

describe("MARKETING_ORIGIN", () => {
  it("accepts an https origin, with or without the trailing slash", () => {
    expect(marketingOrigin("https://hyperchat-frontend.vercel.app", "preview")).toBe(
      "https://hyperchat-frontend.vercel.app",
    );
    expect(marketingOrigin("https://site.example/", "production")).toBe("https://site.example");
  });

  it("defaults to the local Vite dev server outside production, and is required in production", () => {
    expect(marketingOrigin(undefined, undefined)).toBe(LOCAL_MARKETING_ORIGIN);
    expect(marketingOrigin("", "preview")).toBe(LOCAL_MARKETING_ORIGIN);
    expect(() => marketingOrigin(undefined, "production")).toThrow("required");
  });

  it.each([
    "http://site.example",
    "https://site.example/path",
    "https://site.example?x=1",
    "https://user:pw@site.example",
    "ftp://site.example",
    "site.example",
  ])("refuses %s", (value) => {
    expect(() => marketingOrigin(value, "preview")).toThrow("MARKETING_ORIGIN");
  });

  it("accepts http only for localhost", () => {
    expect(marketingOrigin("http://localhost:5174", undefined)).toBe("http://localhost:5174");
    expect(marketingOrigin("http://127.0.0.1:4173", undefined)).toBe("http://127.0.0.1:4173");
  });
});

describe("www redirect", () => {
  it.each([
    ["www.hyperchat.space", "https://hyperchat.space/tsukii?ref=x"],
    ["WWW.quicktestspace.site:443", "https://quicktestspace.site/tsukii?ref=x"],
  ])("sends %s to its apex", (host, target) => {
    expect(wwwRedirect(host, new URL("https://h/tsukii?ref=x"))).toBe(target);
  });

  it.each([null, "hyperchat.space", "auth.hyperchat.space", "constructor", "__proto__"])(
    "leaves %s alone",
    (host) => expect(wwwRedirect(host, new URL("https://h/"))).toBeNull(),
  );
});

describe("proxy and the apex split", () => {
  const call = (url: string) => proxy(new NextRequest(url, { headers: { host: new URL(url).host } }));

  it("redirects www permanently before any routing", () => {
    const res = call("https://www.hyperchat.space/features?a=1");
    expect(res.status).toBe(308);
    expect(res.headers.get("location")).toBe("https://hyperchat.space/features?a=1");
  });

  it("passes marketing paths through without the nonce CSP", () => {
    for (const path of ["/", "/features", "/assets/index-abc.js", "/sitemap.xml"]) {
      const res = call(`https://hyperchat.space${path}`);
      expect(res.headers.get("content-security-policy")).toBeNull();
      expect(res.headers.get("x-middleware-rewrite")).toBeNull();
      expect(res.headers.get("x-robots-tag")).toBeNull();
    }
  });

  it("keeps noindex for a marketing path on an unmapped host", () => {
    expect(call("https://x-git-main.vercel.app/features").headers.get("x-robots-tag")).toBe("noindex");
  });

  it("gives creator pages the nonce CSP", () => {
    expect(call("https://hyperchat.space/tsukii").headers.get("content-security-policy")).toMatch(/'nonce-/);
  });

  it("does not exempt other zones: the dashboard host still rewrites /features into its own segment", () => {
    const res = call("https://dashboard.quicktestspace.site/features");
    expect(res.headers.get("x-middleware-rewrite")).toBe(
      "https://dashboard.quicktestspace.site/dashboard/features",
    );
    expect(res.headers.get("content-security-policy")).toMatch(/'nonce-/);
  });
});
