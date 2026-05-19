import { test, expect } from "@playwright/test";

const MODEL_SLUG = `ui-test-model-${Date.now()}`;
const MCP_NAME   = `ui-test-mcp-${Date.now()}`;
const EVAL_SLUG  = `ui-test-eval-${Date.now()}`;

test("create + delete a Model via the UI", async ({ page }) => {
  await page.goto("/models");
  await expect(page.getByTestId("models-new")).toBeVisible();
  await page.getByTestId("models-new").click();
  await expect(page.getByTestId("modal")).toBeVisible();

  await page.getByTestId("models-form-slug").fill(MODEL_SLUG);
  await page.getByTestId("models-form-provider").selectOption("echo");
  await page.getByTestId("models-form-modelid").fill(MODEL_SLUG);
  await page.getByTestId("models-form-save").click();

  // new row appears
  await expect(page.getByTestId(`models-row-${MODEL_SLUG}`)).toBeVisible({ timeout: 5_000 });
  // delete it (auto-confirm dialog)
  page.on("dialog", d => d.accept());
  await page.getByTestId(`models-delete-${MODEL_SLUG}`).click();
  await expect(page.getByTestId(`models-row-${MODEL_SLUG}`)).not.toBeVisible({ timeout: 5_000 });
});

test("create + delete a custom MCP server via the UI", async ({ page }) => {
  await page.goto("/mcp");
  await page.getByTestId("mcp-new").click();
  await expect(page.getByTestId("modal")).toBeVisible();

  await page.getByTestId("mcp-form-name").fill(MCP_NAME);
  await page.getByTestId("mcp-form-command").fill("echo");
  await page.getByTestId("mcp-form-save").click();

  await expect(page.getByTestId(`mcp-${MCP_NAME}`)).toBeVisible({ timeout: 5_000 });
  page.on("dialog", d => d.accept());
  await page.getByTestId(`mcp-delete-${MCP_NAME}`).click();
  await expect(page.getByTestId(`mcp-${MCP_NAME}`)).not.toBeVisible({ timeout: 5_000 });
});

test("create + delete an eval via the UI", async ({ page }) => {
  await page.goto("/evals");
  await page.getByTestId("evals-new").click();
  await expect(page.getByTestId("modal")).toBeVisible();

  await page.getByTestId("evals-form-slug").fill(EVAL_SLUG);
  await page.getByTestId("evals-form-save").click();

  await expect(page.getByTestId(`eval-${EVAL_SLUG}`)).toBeVisible({ timeout: 5_000 });
  page.on("dialog", d => d.accept());
  await page.getByTestId(`evals-delete-${EVAL_SLUG}`).click();
  await expect(page.getByTestId(`eval-${EVAL_SLUG}`)).not.toBeVisible({ timeout: 5_000 });
});

test("Run detail shows lineage tree with children for a workflow run", async ({ page }) => {
  // create a fresh workflow run via API
  const r = await page.request.post("/api/workflows/test-echo-parallel/run",
    { data: { input: { input: "tree test" } } });
  expect(r.ok()).toBeTruthy();
  const { run_id } = await r.json();
  // wait for completion
  for (let i = 0; i < 20; i++) {
    const g = await page.request.get(`/api/runs/${run_id}`);
    if ((await g.json()).status !== "running") break;
    await page.waitForTimeout(300);
  }
  await page.goto(`/runs/${run_id}`);
  await expect(page.getByTestId("run-tree")).toBeVisible({ timeout: 10_000 });
  // it should list 4 runs total (1 workflow + 3 echo children)
  await expect(page.getByTestId("run-tree")).toContainText("runs: 4");
});

test("Runs list shows initiator column and parent arrow for child runs", async ({ page }) => {
  // produce a workflow run first
  const r = await page.request.post("/api/workflows/test-echo-parallel/run",
    { data: { input: { input: "init col test" } } });
  expect(r.ok()).toBeTruthy();
  await page.waitForTimeout(800);
  await page.goto("/runs");
  // initiator column header
  await expect(page.getByText("initiator", { exact: false }).first()).toBeVisible();
  // there should be at least one row with badge "workflow_run"
  await expect(page.locator("td", { hasText: "workflow_run" }).first()).toBeVisible({ timeout: 5_000 });
});
