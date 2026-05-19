import { test, expect } from "@playwright/test";

const SLUG = `lc-tool-${Date.now()}`;

test("LangChain ReAct path: fake provider can call code.write_file end-to-end", async ({ page }) => {
  // create agent backed by the offline fake-tool-chat model
  const create = await page.request.post("/api/agents", {
    data: {
      slug: SLUG, name: "LC fake", description: "test",
      system_prompt: "You are a test agent.",
      model_slug: "fake-tool-chat",
      tool_specs: ["code.write_file"],
      skill_slugs: [], params: {},
      color: "#58a6ff", icon: "bot",
    },
  });
  expect(create.ok()).toBeTruthy();

  // run it
  const r = await page.request.post(`/api/agents/${SLUG}/run`,
                                    { data: { input: { input: "do the write" } } });
  expect(r.ok()).toBeTruthy();
  const { run_id } = await r.json();

  // wait for completion
  for (let i = 0; i < 30; i++) {
    const got = (await (await page.request.get(`/api/runs/${run_id}`)).json());
    if (got.status !== "running") break;
    await page.waitForTimeout(250);
  }
  const run = await (await page.request.get(`/api/runs/${run_id}`)).json();
  expect(run.status).toBe("success");

  // events should include a tool_call + tool_result for write_file
  const events = await (await page.request.get(`/api/runs/${run_id}/events`)).json();
  const kinds = events.map((e: any) => e.kind);
  expect(kinds).toContain("tool_call");
  expect(kinds).toContain("tool_result");
  // tool_call payload should mention write_file
  const tcEvents = events.filter((e: any) => e.kind === "tool_call");
  expect(tcEvents.length).toBeGreaterThan(0);
  expect(tcEvents[0].payload.name).toBe("write_file");

  // cleanup
  page.on("dialog", d => d.accept());
  await page.request.delete(`/api/agents/${SLUG}`);
});
