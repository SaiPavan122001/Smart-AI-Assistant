import { expect, test, type Page } from "@playwright/test";

const TOKEN = "e2e-token";

async function login(page: Page) {
  await page.goto("/");
  await page.getByLabel("API token").fill(TOKEN);
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
}

async function run(page: Page, instruction: string) {
  await page.getByLabel(/what should i do/i).fill(instruction);
  await page.getByRole("button", { name: "Execute" }).click();
}

const status = (page: Page) => page.getByTestId("status-label");

test.describe("authentication", () => {
  test("rejects a wrong token and accepts the right one", async ({ page }) => {
    await page.goto("/");
    await page.getByLabel("API token").fill("nope");
    await page.getByRole("button", { name: "Connect" }).click();
    await expect(page.getByRole("alert")).toContainText(/token/i);
    await page.getByLabel("API token").fill(TOKEN);
    await page.getByRole("button", { name: "Connect" }).click();
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
    await expect(page.getByTestId("stop-agent")).toBeVisible(); // emergency stop is always on screen
  });
});

test("runs a task end to end with a live timeline and a verified result", async ({ page }) => {
  await login(page);
  await run(page, "Create a folder called UI Test Folder on my desktop");
  await expect(status(page)).toContainText("COMPLETED");
  const timeline = page.getByRole("list", { name: "Activity timeline" });
  await expect(timeline).toContainText("Understanding request");
  await expect(timeline).toContainText("Created execution plan");
  await expect(timeline).toContainText("Create UI Test Folder on the desktop");
  await expect(timeline).toContainText("Verified final state");
  const result = page.getByRole("region", { name: "Final result" });
  await expect(result).toContainText("COMPLETED");
  await expect(result.getByText("verified", { exact: true })).toBeVisible();
  await expect(result).toContainText("1 LLM calls");

  // history + details
  await page.getByRole("link", { name: "Task history" }).click();
  await expect(page.getByRole("table", { name: "Tasks" })).toContainText("Create a folder called UI Test Folder");
  await page.getByRole("row", { name: /Create a folder/ }).first().click();
  await page.getByRole("tab", { name: "Plan" }).click();
  await expect(page.getByRole("table", { name: "Plan steps" })).toContainText("fs_create");
  await page.getByRole("tab", { name: "Tool calls" }).click();
  await expect(page.getByRole("table", { name: "Tool calls" })).toContainText("fs_create");
  await page.getByRole("tab", { name: "Verification" }).click();
  await expect(page.getByRole("table", { name: "Verification" })).toContainText("pass");
  await page.getByRole("tab", { name: "Logs" }).click();
  await expect(page.getByLabel("Level")).toBeVisible();
});

test("asks for a specific approval, and a denial prevents the action", async ({ page }) => {
  await login(page);
  await run(page, "Delete the old backup from my documents");
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("old-backup.zip");
  await expect(dialog).toContainText("You asked me to delete the old backup");
  await expect(dialog).toContainText("high risk");
  await expect(status(page)).toContainText("WAITING APPROVAL");
  await expect(page.getByRole("button", { name: "Deny" })).toBeFocused();
  await page.getByRole("button", { name: "Deny" }).click();
  await expect(dialog).toBeHidden();
  await expect(status(page)).toContainText("FAILED");
  await expect(page.getByRole("list", { name: "Activity timeline" })).toContainText("Approval denied");
  await expect(page.getByRole("region", { name: "Final result" })).toContainText(/approval_denied|not approve|Failed/i);

  // the approval is part of the audit trail
  await page.getByRole("link", { name: "Open full task details" }).click();
  await page.getByRole("tab", { name: "Approvals" }).click();
  await expect(page.getByRole("table", { name: "Approvals" })).toContainText("DENIED");
});

test("approving lets the action run and complete", async ({ page }) => {
  await login(page);
  await run(page, "Delete the old backup from my documents");
  await page.getByRole("button", { name: "Approve once" }).click();
  await expect(status(page)).toContainText("COMPLETED");
  await expect(page.getByRole("list", { name: "Activity timeline" })).toContainText("Approval received");
});

test("STOP AGENT halts a running task immediately and can be released", async ({ page }) => {
  await login(page);
  await run(page, "Run the slow script");
  await page.getByRole("button", { name: "Approve once" }).click(); // python -c needs approval
  await expect(status(page)).toContainText("EXECUTING");
  await expect(page.getByTestId("stop-agent")).toHaveClass(/armed/);
  await page.getByTestId("stop-agent").click();
  await expect(status(page)).toContainText("CANCELLED");
  await expect(page.getByTestId("kill-engaged")).toBeVisible();
  await expect(page.getByRole("list", { name: "Activity timeline" })).toContainText("EMERGENCY STOP");

  // new tasks are refused until the stop is released
  await run(page, "Create a folder called Blocked");
  await expect(page.getByRole("alert").first()).toContainText(/emergency stop/i);
  await page.getByRole("button", { name: "Release stop" }).click();
  await expect(page.getByTestId("stop-agent")).toBeVisible();
});

test("prompt injection from a file is flagged and never executed", async ({ page }) => {
  await login(page);
  await run(page, "Summarise notes.txt in my documents");
  await expect(status(page)).toContainText("COMPLETED");
  const timeline = page.getByRole("list", { name: "Activity timeline" });
  await expect(timeline).toContainText("Untrusted content contained instructions — ignored");
  await expect(page.getByRole("alertdialog")).toHaveCount(0); // the attacker's deletion never even reached a human
  await expect(page.getByRole("region", { name: "Final result" })).toContainText("Short notes");
  await page.getByRole("link", { name: "Open full task details" }).click();
  await page.getByRole("tab", { name: "Tool calls" }).click();
  await expect(page.getByRole("table", { name: "Tool calls" })).toContainText("prompt_injection_blocked");
});

test("permissions can be edited; critical can never be auto-allowed", async ({ page }) => {
  await login(page);
  await page.getByRole("link", { name: "Permissions" }).click();
  const critical = page.getByLabel("critical risk action");
  await expect(critical.locator("option")).toHaveText(["ask me first", "deny"]); // no "allow"
  await page.getByLabel("medium risk action").selectOption("ask");
  await page.getByRole("button", { name: "Save permissions" }).click();
  await expect(page.getByRole("status")).toContainText("Saved");
  await page.reload();
  await expect(page.getByLabel("medium risk action")).toHaveValue("ask");

  // with MEDIUM now requiring approval, creating a folder asks first
  await page.getByRole("link", { name: "Dashboard" }).click();
  await run(page, "Create a folder called UI Test Folder on my desktop");
  await expect(page.getByRole("alertdialog")).toContainText("Create folder");
  await page.getByRole("button", { name: "Deny" }).click();
  await expect(status(page)).toContainText("FAILED");
  // restore the default so later runs are unaffected
  await page.getByRole("link", { name: "Permissions" }).click();
  await page.getByLabel("medium risk action").selectOption("allow");
  await page.getByRole("button", { name: "Save permissions" }).click();
  await expect(page.getByRole("status")).toContainText("Saved");
});

test("settings show effective configuration without secrets", async ({ page }) => {
  await login(page);
  await page.getByRole("link", { name: "Settings" }).click();
  await expect(page.getByRole("heading", { name: "Readiness" })).toBeVisible();
  await expect(page.locator("body")).not.toContainText(TOKEN);
  await expect(page.locator("body")).toContainText("allowed roots");
});
