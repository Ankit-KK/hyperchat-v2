import { expect, test } from "@playwright/test";

test("the auth host shell loads with noindex and no referrer", async ({ page }) => {
  const response = await page.goto("/");
  expect(response?.status()).toBe(200);
  expect(response?.headers()["x-robots-tag"]).toBe("noindex");
  expect(response?.headers()["referrer-policy"]).toBe("no-referrer");
});
