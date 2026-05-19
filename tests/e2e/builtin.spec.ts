import { test, expect } from "@playwright/test";

test("File skill: delete tombstones it (disappears from list)", async ({ page }) => {
  page.on("dialog", d => d.accept());
  await page.goto("/skills");
  // pick a file skill that we know exists
  const slug = "toggle-sweep-ai";
  await expect(page.getByTestId(`skill-${slug}`)).toBeVisible({ timeout: 10_000 });
  await page.getByTestId(`skill-delete-${slug}`).click();
  await expect(page.getByTestId(`skill-${slug}`)).not.toBeVisible({ timeout: 10_000 });

  // restore via direct API so we don't pollute other tests
  await page.request.post(`/api/skills/${slug}/reset`);
});

test("Builtin agent: delete + reset round-trip via UI", async ({ page }) => {
  page.on("dialog", d => d.accept());
  // Use the explorer agent for this test (avoids interfering with other tests that hit coder)
  await page.goto("/agents/explorer");
  await page.getByTestId("agent-delete").click();
  await expect(page).toHaveURL(/\/agents$/);
  await expect(page.getByTestId("agent-card-explorer")).not.toBeVisible({ timeout: 10_000 });

  // Reset via API (no UI button to recreate a deleted builtin from list yet)
  await page.request.post("/api/agents/explorer/reset");
  await page.reload();
  await expect(page.getByTestId("agent-card-explorer")).toBeVisible({ timeout: 10_000 });
});

test("Seeded workflow: edit persists; reset restores defaults", async ({ page }) => {
  page.on("dialog", d => d.accept());
  const slug = "parallel-explore";
  await page.goto(`/workflows/${slug}`);
  await page.getByTestId("workflow-tab-edit").click();
  await expect(page.getByTestId("workflow-edit-name")).toBeVisible();
  await page.getByTestId("workflow-edit-description").fill("edited by playwright");
  await page.getByTestId("workflow-edit-save").click();
  await expect(page.getByTestId("workflow-tab-run")).toBeVisible({ timeout: 10_000 });
  // edit persisted
  const after = await (await page.request.get(`/api/workflows/${slug}`)).json();
  expect(after.description).toBe("edited by playwright");
  // reset restores seed
  const resp = await page.request.post(`/api/workflows/${slug}/reset`);
  expect(resp.ok()).toBeTruthy();
  const restored = await (await page.request.get(`/api/workflows/${slug}`)).json();
  expect(restored.description).not.toBe("edited by playwright");
});
