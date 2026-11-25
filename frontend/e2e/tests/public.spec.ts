import { expect, test } from "@playwright/test";

test("the public landing loads, may be indexed, and hides internal paths", async ({ page, request }) => {
  const response = await page.goto("/");
  expect(response?.status()).toBe(200);
  expect(response?.headers()["x-robots-tag"]).toBeUndefined();
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  expect((await request.get("/dashboard")).status()).toBe(404);
});
