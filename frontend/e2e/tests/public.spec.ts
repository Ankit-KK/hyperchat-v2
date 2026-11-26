import { expect, test } from "@playwright/test";

/**
 * The apex split (apps/web/lib/marketing.ts). Needs both deployments, or this app with MARKETING_ORIGIN pointing
 * at a running copy of the marketing website. E2E_CREATOR_SLUG names an active creator on the API in use.
 */
const creator = process.env.E2E_CREATOR_SLUG ?? "tsukii";

test("marketing pages come from the website, without this app's nonce CSP", async ({ request }) => {
  for (const path of [
    "/",
    "/features",
    "/creators",
    "/support",
    "/privacy-policy",
    "/robots.txt",
    "/sitemap.xml",
  ]) {
    const response = await request.get(path);
    expect(response.status(), path).toBe(200);
    expect(response.headers()["content-security-policy"] ?? "", path).not.toContain("'nonce-");
  }
});

test("an active creator gets this app's page, which may be indexed", async ({ page }) => {
  const response = await page.goto(`/${creator}`);
  expect(response?.status()).toBe(200);
  expect(response?.headers()["x-robots-tag"]).toBeUndefined();
  expect(response?.headers()["content-security-policy"]).toContain("'nonce-");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
});

test("an unknown slug is a 404 and internal paths stay hidden", async ({ request }) => {
  expect((await request.get("/no-such-creator-e2e-404")).status()).toBe(404);
  expect((await request.get("/dashboard")).status()).toBe(404);
});

test("www redirects permanently to the apex", async ({ request, baseURL }) => {
  const www = new URL(baseURL!);
  test.skip(www.hostname === "localhost", "no www host on a local run");
  www.hostname = `www.${www.hostname}`;
  const response = await request.get(`${www.origin}/features?x=1`, { maxRedirects: 0 });
  expect(response.status()).toBe(308);
  expect(response.headers()["location"]).toBe(`${baseURL!.replace(/\/$/, "")}/features?x=1`);
});
