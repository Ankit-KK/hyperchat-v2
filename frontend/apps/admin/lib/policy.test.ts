import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";
import { proxy } from "../proxy";
import { adminHeaders, DEFAULT_ADMIN_HOSTS, hostAllowed, parseHosts } from "./policy";

const hosts = parseHosts(DEFAULT_ADMIN_HOSTS);

describe("admin host only", () => {
  it.each([
    ["admin.quicktestspace.site", true],
    ["ADMIN.hyperchat.space:443", true],
    ["admin.localhost:3001", true],
    ["dashboard.quicktestspace.site", false],
    ["quicktestspace.site", false],
    ["x-git-main.vercel.app", false],
    ["", false],
  ])("%s -> %s", (host, ok) => {
    expect(hostAllowed(host, hosts, undefined)).toBe(ok);
  });

  it("accepts a Vercel preview URL only in a preview deployment", () => {
    expect(hostAllowed("x-git-main.vercel.app", hosts, "preview")).toBe(true);
    expect(hostAllowed("x-git-main.vercel.app", hosts, "production")).toBe(false);
  });
});

describe("admin headers", () => {
  it("is strict: nonce scripts, no inline style attributes, never indexed or framed", () => {
    const h = adminHeaders("n", "https://api.quicktestspace.site", { dev: false, https: true });
    expect(h["Content-Security-Policy"]).toContain("default-src 'self'");
    expect(h["Content-Security-Policy"]).toContain("script-src 'self' 'nonce-n' 'strict-dynamic';");
    expect(h["Content-Security-Policy"]).not.toMatch(/unsafe-inline|unsafe-eval|https:;/);
    expect(h["X-Robots-Tag"]).toBe("noindex, nofollow");
    expect(h["Content-Security-Policy"]).toContain("frame-ancestors 'none'");
  });

  it("refuses other hosts with 404 and still sends the headers", () => {
    const res = proxy(
      new NextRequest("https://quicktestspace.site/", { headers: { host: "quicktestspace.site" } }),
    );
    expect(res.status).toBe(404);
    expect(res.headers.get("x-robots-tag")).toBe("noindex, nofollow");
    const ok = proxy(
      new NextRequest("https://admin.quicktestspace.site/", {
        headers: { host: "admin.quicktestspace.site" },
      }),
    );
    expect(ok.status).toBe(200);
  });
});
