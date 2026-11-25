import { expect, test } from "@playwright/test";

test("the overlay shell loads with noindex", async ({ page }) => {
  const response = await page.goto("/");
  expect(response?.status()).toBe(200);
  expect(response?.headers()["x-robots-tag"]).toBe("noindex");
});
