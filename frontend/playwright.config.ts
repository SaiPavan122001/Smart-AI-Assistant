import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

// The UI is exercised against the REAL backend (see backend/evals/e2e_server.py: scripted model,
// throw-away sandbox). Chrome/Edge installed on the machine is used so no browser download is needed.
const venvPython = resolve(here, "../backend/.venv/Scripts/python.exe");
const python = existsSync(venvPython) ? venvPython : "python";
const channel = (process.env.PW_CHANNEL ?? "chrome") as "chrome" | "msedge";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"]],
  use: { baseURL: "http://localhost:5174", channel, trace: "retain-on-failure", screenshot: "only-on-failure" },
  webServer: [
    {
      command: `"${python}" -m evals.e2e_server --port 8765`,
      cwd: resolve(here, "../backend"),
      url: "http://127.0.0.1:8765/api/v1/health",
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: "npx vite --port 5174 --strictPort",
      env: { SAA_BACKEND_URL: "http://127.0.0.1:8765" },
      url: "http://localhost:5174",
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
