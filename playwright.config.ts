import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "tests/e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 1,           // single retry to absorb race flakes (background polling, SSE timing)
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:8765",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: ".venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8765 --log-level warning",
    url: "http://127.0.0.1:8765/api/health",
    reuseExistingServer: true,
    timeout: 30_000,
    stdout: "ignore",
    stderr: "pipe",
  },
});
