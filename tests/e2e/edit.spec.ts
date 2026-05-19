import { test, expect } from "@playwright/test";

const ts = Date.now();
const WF_SLUG    = `wf-ui-${ts}`;
const AGENT_SLUG = `agent-ui-${ts}`;
const MODEL_SLUG = `model-edit-${ts}`;
const MCP_NAME   = `mcp-edit-${ts}`;
const EVAL_SLUG  = `eval-edit-${ts}`;

test("Workflow: create via UI, edit graph, run, delete", async ({ page }) => {
  page.on("dialog", d => d.accept());

  // Create
  await page.goto("/workflows");
  await page.getByTestId("workflows-new").click();
  await expect(page).toHaveURL(/\/workflows\/new$/);

  await page.getByTestId("workflow-edit-slug").fill(WF_SLUG);
  await page.getByTestId("workflow-edit-name").fill("UI test workflow");
  await page.getByTestId("workflow-edit-description").fill("created by playwright");
  // pick "nodes" topology (concurrency set inside the JSON below)
  await page.getByTestId("workflow-edit-topology").selectOption("nodes");
  // switch to JSON mode then tweak the graph — include concurrency=parallel
  await page.getByTestId("workflow-editor-json").click();
  await page.getByTestId("workflow-edit-graph").fill(JSON.stringify({
    concurrency: "parallel",
    nodes: [
      { id: "x1", agent: "echo-coder", label: "A", input_template: "{input}" },
      { id: "x2", agent: "echo-coder", label: "B", input_template: "{input}" },
    ]
  }, null, 2));
  await page.getByTestId("workflow-edit-save").click();
  await expect(page).toHaveURL(new RegExp(`/workflows/${WF_SLUG}$`));

  // Run from the run tab
  await expect(page.getByTestId("workflow-graph")).toBeVisible({ timeout: 10_000 });
  await page.getByTestId("workflow-input").fill("ui-edit-test");
  await page.getByTestId("workflow-run").click();
  await expect(page.getByTestId("workflow-output")).toContainText("outputs", { timeout: 30_000 });

  // Edit it again
  await page.getByTestId("workflow-tab-edit").click();
  await page.getByTestId("workflow-editor-json").click();
  await expect(page.getByTestId("workflow-edit-graph")).toBeVisible();
  await page.getByTestId("workflow-edit-description").fill("updated by playwright");
  await page.getByTestId("workflow-edit-save").click();
  await page.waitForTimeout(500);

  // Delete
  await page.getByTestId("workflow-tab-edit").click();
  await page.getByTestId("workflow-delete").click();
  await expect(page).toHaveURL(/\/workflows$/);
});

test("Agent: create via UI, edit, delete", async ({ page }) => {
  page.on("dialog", d => d.accept());
  await page.goto("/agents");
  await page.getByTestId("agents-new").click();
  await expect(page).toHaveURL(/\/agents\/new$/);

  await page.getByTestId("agent-slug").fill(AGENT_SLUG);
  await page.getByTestId("agent-name").fill("UI Test Agent");
  await page.getByTestId("agent-prompt").fill("You are a test agent.");
  await page.getByTestId("agent-save").click();
  await expect(page).toHaveURL(new RegExp(`/agents/${AGENT_SLUG}$`));

  // Edit
  await page.getByTestId("agent-prompt").fill("You are an UPDATED test agent.");
  await page.getByTestId("agent-save").click();
  // wait for save round-trip: button label flips back from "saving..." to "save"
  await expect(page.getByTestId("agent-save")).toHaveText("save", { timeout: 10_000 });
  await page.reload();
  await expect(page.getByTestId("agent-prompt")).toContainText("UPDATED", { timeout: 5_000 });

  // Delete from edit page
  await page.getByTestId("agent-delete").click();
  await expect(page).toHaveURL(/\/agents$/);
});

test("Model: edit via the row's edit button", async ({ page }) => {
  page.on("dialog", d => d.accept());

  // create directly via API
  await page.request.post("/api/models", { data: {
    slug: MODEL_SLUG, provider: "echo", model_id: MODEL_SLUG,
    display_name: "before edit", params: {}, enabled: true,
  }});

  await page.goto("/models");
  await expect(page.getByTestId(`models-row-${MODEL_SLUG}`)).toBeVisible();
  await page.getByTestId(`models-edit-${MODEL_SLUG}`).click();
  // slug should be disabled (identity locked)
  await expect(page.getByTestId("models-form-slug")).toBeDisabled();
  await page.getByTestId("models-form-displayname").fill("after edit");
  await page.getByTestId("models-form-save").click();
  await expect(page.getByTestId(`models-row-${MODEL_SLUG}`)).toContainText("after edit", { timeout: 5_000 });

  // cleanup
  await page.getByTestId(`models-delete-${MODEL_SLUG}`).click();
});

test("MCP server: edit via the row's edit button", async ({ page }) => {
  page.on("dialog", d => d.accept());

  await page.request.post("/api/mcp/servers", { data: {
    name: MCP_NAME, command: "echo", args: ["before"], env: {}, enabled: true,
  }});

  await page.goto("/mcp");
  await expect(page.getByTestId(`mcp-${MCP_NAME}`)).toBeVisible();
  await page.getByTestId(`mcp-edit-${MCP_NAME}`).click();
  await page.getByTestId("mcp-form-command").fill("printf");
  await page.getByTestId("mcp-form-save").click();
  await expect(page.getByTestId(`mcp-${MCP_NAME}`)).toContainText("printf", { timeout: 5_000 });

  await page.getByTestId(`mcp-delete-${MCP_NAME}`).click();
});

test("Eval: edit via the row's edit button", async ({ page }) => {
  page.on("dialog", d => d.accept());

  await page.request.post("/api/evals", { data: {
    slug: EVAL_SLUG, name: "before", description: "before",
    target_kind: "agent", target_slug: "echo-coder",
    dataset: [{ input: "x", expected: "x" }],
    metric: "assert_contains", metric_args: {},
  }});

  await page.goto("/evals");
  await expect(page.getByTestId(`eval-${EVAL_SLUG}`)).toBeVisible();
  await page.getByTestId(`evals-edit-${EVAL_SLUG}`).click();
  // change description
  const desc = page.locator('input').nth(2);
  await desc.fill("after edit");
  await page.getByTestId("evals-form-save").click();
  await expect(page.getByTestId(`eval-${EVAL_SLUG}`)).toContainText("after edit", { timeout: 5_000 });

  await page.getByTestId(`evals-delete-${EVAL_SLUG}`).click();
});
