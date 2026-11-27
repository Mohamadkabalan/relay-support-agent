// The browser smoke test (PLAN.md Phase 6). It drives the real stack through Caddy, with the
// API on the key-free demo agent (RELAY_PROVIDER=scripted), so it needs no model key:
//
//   RELAY_PROVIDER=scripted make up && make seed && make e2e
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:8080",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    // The narrowest width the plan promises (375 px), with touch.
    {
      name: "phone",
      use: { ...devices["Desktop Chrome"], viewport: { width: 375, height: 812 }, hasTouch: true },
    },
  ],
});
