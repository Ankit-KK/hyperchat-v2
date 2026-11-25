import { expect, test } from "@playwright/test";

test("the admin shell loads with a strict CSP and is never indexed", async ({ page }) => {
  const response = await page.goto("/");
  expect(response?.status()).toBe(200);
  expect(response?.headers()["x-robots-tag"]).toBe("noindex, nofollow");
  expect(response?.headers()["content-security-policy"]).toContain("default-src 'self'");
  await expect(page.getByRole("heading", { name: "HyperChat administration" })).toBeVisible();
});
