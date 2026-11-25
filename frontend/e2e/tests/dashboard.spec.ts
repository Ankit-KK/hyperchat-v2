import { expect, test } from "@playwright/test";

test("the dashboard shell loads with noindex and runs the session bootstrap", async ({ page }) => {
  const response = await page.goto("/");
  expect(response?.headers()["x-robots-tag"]).toBe("noindex");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expect(page.getByText("Checking your session…")).toBeHidden();
});
