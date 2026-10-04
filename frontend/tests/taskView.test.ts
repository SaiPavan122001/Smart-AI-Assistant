import { describe, expect, it } from "vitest";
import { buildTaskView, emptyView, reduceEvent } from "../src/features/tasks/taskView";
import { approval, ev, happyPath, resetSeq, result } from "./fixtures";

describe("taskView reducer", () => {
  it("reconstructs a complete timeline from events alone", () => {
    const events = happyPath();
    const v = buildTaskView("task_1", events);
    expect(v.instruction).toBe("make a folder");
    expect(v.status).toBe("COMPLETED");
    expect(v.plan?.steps).toHaveLength(1);
    expect(v.lastSeq).toBe(events.at(-1)!.seq);
    const titles = v.timeline.map((t) => t.title);
    expect(titles[0]).toBe("Understanding request");
    expect(titles).toContain("Created execution plan");
    expect(titles).toContain("Create folder");
    expect(titles.at(-1)).toMatch(/task completed/i);
    // steps own their tool/observation lines
    const step = v.timeline.find((t) => t.key === "step:s1")!;
    expect(step.icon).toBe("done");
    expect(step.children.map((c) => c.title)).toEqual(expect.arrayContaining(["Using fs_create"]));
    expect(step.children.find((c) => c.key === "tool:tc_1")?.icon).toBe("done");
    // nothing is left spinning once the task is finished
    const walk = (items: typeof v.timeline): boolean => items.every((i) => i.icon !== "running" && walk(i.children));
    expect(walk(v.timeline)).toBe(true);
    expect(v.result?.verified).toBe(true);
    expect(v.currentTool).toBeNull();
  });

  it("is idempotent: duplicate or replayed events change nothing", () => {
    const events = happyPath();
    const once = buildTaskView("task_1", events);
    const twice = buildTaskView("task_1", [...events, ...events]);
    expect(twice).toEqual(once);
    const stale = reduceEvent(once, events[2]!);
    expect(stale).toBe(once);
  });

  it("tracks current tool, application and step while running", () => {
    const events = happyPath().slice(0, 7);
    const v = buildTaskView("task_1", events);
    expect(v.currentTool).toBe("fs_create");
    expect(v.currentApplication).toBe("File system · C:\\x");
    expect(v.currentStepTitle).toBe("Create folder");
    expect(v.timeline.find((t) => t.key === "step:s1")?.icon).toBe("running");
  });

  it("models the approval lifecycle (required → granted / denied)", () => {
    resetSeq();
    const a = approval();
    let v = buildTaskView("task_1", [
      ev("step_started", { message: "Delete", correlation: { step_id: "s1" } }),
      ev("approval_required", { message: a.action, data: { approval: a }, correlation: { step_id: "s1" } }),
    ]);
    expect(v.pendingApprovals.map((x) => x.id)).toEqual(["apr_1"]);
    const item = v.timeline[0]!.children.find((c) => c.key === "apr:apr_1")!;
    expect(item.icon).toBe("warn");
    expect(item.detail).toContain("old-backup.zip");

    const granted = reduceEvent(v, ev("approval_granted", { data: { approval_id: "apr_1", scope: "once" } }));
    expect(granted.pendingApprovals).toEqual([]);
    expect(granted.resolvedApprovals[0]?.status).toBe("APPROVED");
    expect(granted.timeline[0]!.children.find((c) => c.key === "apr:apr_1")?.title).toBe("Approval received");

    v = reduceEvent(v, ev("approval_denied", { data: { approval_id: "apr_1", status: "DENIED" } }));
    expect(v.resolvedApprovals[0]?.status).toBe("DENIED");
    expect(v.timeline[0]!.children.find((c) => c.key === "apr:apr_1")?.icon).toBe("error");
  });

  it("surfaces recovery, injection warnings, limits, pause and emergency stop", () => {
    resetSeq();
    const v = buildTaskView("task_1", [
      ev("step_started", { message: "Click", correlation: { step_id: "s1" } }),
      ev("injection_detected", { message: "Untrusted content from browser_read contains instruction-like text", data: { tool: "browser_read" }, correlation: { step_id: "s1" } }),
      ev("recovery_started", { message: "stale reference", data: { strategy: "refresh_state_retry" }, correlation: { step_id: "s1" } }),
      ev("recovery_completed", { message: "Recovered via refresh", correlation: { step_id: "s1" } }),
      ev("task_paused"),
      ev("limit_reached", { message: "maximum actions (200)" }),
      ev("kill_switch", { message: "STOP AGENT pressed" }),
    ]);
    const step = v.timeline.find((t) => t.key === "step:s1")!;
    expect(step.children.some((c) => c.title.includes("ignored") && c.icon === "warn")).toBe(true);
    expect(step.children.some((c) => c.title.startsWith("Recovering (refresh state retry)"))).toBe(true);
    expect(v.paused).toBe(true);
    expect(v.killSwitch).toBe(true);
    expect(v.timeline.at(-1)?.title).toBe("EMERGENCY STOP");
  });

  it("tracks the pause lifecycle so a paused task is never shown as running", () => {
    resetSeq();
    const events = [
      ev("status_changed", { data: { status: "EXECUTING", previous: "PLANNING" } }),
      ev("pause_requested"),
    ];
    let v = buildTaskView("task_1", events);
    expect(v.paused).toBe(true); // requested: the in-flight call is finishing
    expect(v.status).toBe("EXECUTING");
    v = reduceEvent(v, ev("status_changed", { data: { status: "PAUSED", previous: "EXECUTING" } }));
    v = reduceEvent(v, ev("pause_completed"));
    expect(v.status).toBe("PAUSED");
    expect(v.paused).toBe(true);
    v = reduceEvent(v, ev("resume_requested"));
    expect(v.status).toBe("PAUSED"); // still paused until the runtime restored the state
    expect(v.paused).toBe(true);
    v = reduceEvent(v, ev("status_changed", { data: { status: "EXECUTING", previous: "PAUSED" } }));
    v = reduceEvent(v, ev("resume_completed"));
    expect(v.status).toBe("EXECUTING");
    expect(v.paused).toBe(false);
    expect(v.timeline.map((t) => t.title)).toEqual(["Pause requested", "Paused", "Resume requested", "Resumed"]);
  });

  it("handles failed, skipped and verification-failure paths", () => {
    resetSeq();
    const v = buildTaskView("task_1", [
      ev("step_started", { message: "Delete", correlation: { step_id: "s1" } }),
      ev("step_failed", { message: "Failed: approval denied", correlation: { step_id: "s1" } }),
      ev("step_failed", { message: "Skipped: depends on s1", data: { skipped: true }, correlation: { step_id: "s2" } }),
      ev("verification_completed", { message: "0/1 passed", data: { passed: false } }),
      ev("task_failed", { message: "Failed", data: { result: result({ status: "FAILED", verified: false, summary: "Failed: 0/2" }) } }),
    ]);
    expect(v.timeline.find((t) => t.key === "step:s1")?.icon).toBe("error");
    expect(v.timeline.find((t) => t.key === "step:s2")?.title).toBe("Skipped");
    expect(v.timeline.some((t) => t.title === "Verification failed" && t.icon === "error")).toBe(true);
    expect(v.status).toBe("FAILED");
    expect(v.result?.verified).toBe(false);
  });

  it("clears pending approvals when the task ends (cancel / stop)", () => {
    resetSeq();
    const a = approval();
    const v = buildTaskView("task_1", [
      ev("approval_required", { data: { approval: a } }),
      ev("task_cancelled", { message: "cancelled", data: { result: result({ status: "CANCELLED", verified: false }) } }),
    ]);
    expect(v.pendingApprovals).toEqual([]);
    expect(v.status).toBe("CANCELLED");
  });

  it("starts empty", () => {
    expect(emptyView("x")).toMatchObject({ taskId: "x", status: "CREATED", timeline: [], pendingApprovals: [] });
  });
});
