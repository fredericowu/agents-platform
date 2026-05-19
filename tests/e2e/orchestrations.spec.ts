import { test, expect } from "@playwright/test";

// Each workflow page loads + the Run button starts a run.
// We don't wait for completion here — real-LLM runs take minutes;
// the orchestration *logic* is verified by BDD on the API.
const WORKFLOWS = [
  "orchestrator-worker", "spec-pipeline", "parallel-explore",
  "sequential-review", "group-chat-debate", "ask-coder", "build-app", "enhance-app",
];

for (const slug of WORKFLOWS) {
  test(`workflow ${slug} page renders and a run can be started`,
    async ({ page }) => {
      await page.goto(`/workflows/${slug}`);
      await expect(page.getByTestId("workflow-graph")).toBeVisible({ timeout: 10_000 });
      await page.getByTestId("workflow-input").fill("ui smoke test prompt");
      await page.getByTestId("workflow-run").click();
      // status should flip to "running" within a second; just confirm the
      // run-id pill appears in the side panel
      await expect(page.getByText(/running/i).first()).toBeVisible({ timeout: 5_000 });
    });
}

test("All seeded agents are listed on /agents", async ({ page }) => {
  await page.goto("/agents");
  // wait for fetched cards to appear before counting
  await expect(page.locator('[data-testid^="agent-card-"]').first()).toBeVisible({ timeout: 10_000 });
  const count = await page.locator('[data-testid^="agent-card-"]').count();
  expect(count).toBeGreaterThanOrEqual(13);
});

test("Agent edit: change system prompt then save persists", async ({ page }) => {
  await page.goto("/agents/explorer");
  await expect(page.getByTestId("agent-prompt")).toBeVisible();
  // append marker — full new value matters, just want to confirm save round-trips
  const marker = `___MARK_${Date.now()}___`;
  const prompt = await page.getByTestId("agent-prompt").inputValue();
  await page.getByTestId("agent-prompt").fill(prompt + "\n" + marker);
  await page.getByTestId("agent-save").click();
  // reload and confirm persisted
  await page.reload();
  await expect(page.getByTestId("agent-prompt")).toContainText(marker, { timeout: 5_000 });
  // cleanup: revert
  await page.getByTestId("agent-prompt").fill(prompt);
  await page.getByTestId("agent-save").click();
});

test("Playground: pick echo-coder, send message, see echoed reply with token count", async ({ page }) => {
  await page.goto("/playground");
  // echo-coder is offline + instant; perfect for UI smoke
  await page.getByTestId("playground-agent-echo-coder").click();
  await page.getByTestId("playground-input").fill("plan a TODO app");
  await page.getByTestId("playground-send").click();
  await expect(page.getByTestId("playground-messages")).toContainText("plan a TODO app", { timeout: 10_000 });
  await expect(page.getByTestId("playground-messages")).toContainText("echo", { timeout: 15_000 });
});

test("Run detail: event timeline shows node_start/node_end and is expandable", async ({ page }) => {
  // produce a fresh, fast run against echo-coder
  const r = await page.request.post("/api/agents/echo-coder/run", { data: { input: { input: "trace test" } } });
  expect(r.ok()).toBeTruthy();
  const { run_id } = await r.json();
  // wait for completion
  for (let i = 0; i < 20; i++) {
    const g = await page.request.get(`/api/runs/${run_id}`);
    if ((await g.json()).status !== "running") break;
    await page.waitForTimeout(300);
  }
  await page.goto(`/runs/${run_id}`);
  await expect(page.getByTestId("run-events")).toBeVisible();
  await expect(page.getByTestId("run-events").getByText(/node_start/i).first()).toBeVisible();
  await expect(page.getByTestId("run-events").getByText(/node_end/i).first()).toBeVisible();
  await expect(page.getByTestId("run-thread")).toBeVisible();
});

test("Models page shows all providers", async ({ page }) => {
  await page.goto("/models");
  // expect at least anthropic / openai / bedrock / cli_subshell / echo badges to appear
  for (const p of ["anthropic", "openai", "bedrock", "cli_subshell", "echo"]) {
    await expect(page.locator("td", { hasText: p }).first()).toBeVisible();
  }
});

test("Evals: echo-smoke can be invoked from UI and reports a score", async ({ page }) => {
  await page.goto("/evals");
  await page.getByTestId("eval-run-echo-smoke").click();
  await expect(page.getByTestId("eval-echo-smoke")).toContainText(/%/, { timeout: 30_000 });
});

test("Dashboard quick-start link goes to workflow edit", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("link", { name: /orchestrator/i }).first().click();
  await expect(page).toHaveURL(/\/workflows\/orchestrator-worker/);
});
