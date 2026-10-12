import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LiveStatus } from "../src/features/tasks/LiveStatus";
import { RecoveryPanel } from "../src/features/tasks/RecoveryPanel";
import { buildTaskView, reduceEvent, STATUS_LABEL } from "../src/features/tasks/taskView";
import type { Interruption } from "../src/types/api";
import { approval, ev, happyPath, resetSeq } from "./fixtures";

afterEach(() => vi.restoreAllMocks());

const interruption = (over: Partial<Interruption> = {}): Interruption => ({
  at: new Date(2026, 0, 1).toISOString(),
  reason: "The service stopped while this task was not finished",
  status_at_interruption: "EXECUTING",
  was_paused: false,
  awaiting_approval: false,
  interrupted_step: "s2",
  in_flight: [],
  completed_steps: ["s1"],
  remaining_steps: ["s2", "s3"],
  resumable: true,
  needs_attention: false,
  notes: ["Actions completed before the interruption were not undone."],
  options: ["resume", "restart", "cancel", "inspect"],
  ...over,
});

/** The first ten events of a normal run: the task is in the middle of step s1 when the service dies. */
function runningThenInterrupted(found = interruption()) {
  const before = happyPath().slice(0, 8); // up to action_started
  return [...before, ev("task_interrupted", { message: "Interrupted by a service restart.", data: { status: "INTERRUPTED", interruption: found } })];
}

describe("an interrupted task in the task view", () => {
  it("is INTERRUPTED, shows nothing as running and keeps the interruption details", () => {
    const v = buildTaskView("task_1", runningThenInterrupted());
    expect(v.status).toBe("INTERRUPTED");
    expect(v.interruption?.remaining_steps).toEqual(["s2", "s3"]);
    expect(v.currentTool).toBeNull();
    const flat = v.timeline.flatMap((t) => [t, ...t.children]);
    expect(flat.some((t) => t.icon === "running")).toBe(false);
    expect(v.timeline.at(-1)!.title).toBe("Interrupted by a service restart");
    expect(STATUS_LABEL[v.status]).toMatch(/resume, restart or cancel/);
  });

  it("drops approvals of the stopped process: they cannot be answered any more", () => {
    resetSeq();
    const a = approval();
    const v = buildTaskView("task_1", [
      ev("approval_required", { data: { approval: a } }),
      ev("task_interrupted", { data: { interruption: interruption({ awaiting_approval: true }) } }),
    ]);
    expect(v.pendingApprovals).toEqual([]);
  });

  it("leaves the interrupted state when the user resumes, and ends normally", () => {
    const events = runningThenInterrupted();
    let v = buildTaskView("task_1", events);
    v = reduceEvent(v, ev("task_resuming", { message: "Continuing after an interruption" }));
    v = reduceEvent(v, ev("status_changed", { data: { status: "RECOVERING", previous: "INTERRUPTED" } }));
    expect(v.status).toBe("RECOVERING");
    expect(v.interruption).toBeNull();
    v = reduceEvent(v, ev("task_cancelled", { message: "Cancelled by user" }));
    expect(v.status).toBe("CANCELLED");
  });

  it("replaying the same events after a reconnect does not duplicate anything", () => {
    const events = runningThenInterrupted();
    const once = buildTaskView("task_1", events);
    const twice = [...events, ...events].reduce(reduceEvent, buildTaskView("task_1", []));
    expect(twice.timeline).toEqual(once.timeline);
    expect(twice.timeline.filter((t) => t.title === "Interrupted by a service restart")).toHaveLength(1);
  });
});

describe("LiveStatus for an interrupted task", () => {
  it("offers neither Pause nor Resume nor Cancel: the recovery panel is the only control", () => {
    const view = buildTaskView("task_1", runningThenInterrupted());
    render(<LiveStatus view={view} connection="open" onPause={vi.fn()} onResume={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByTestId("status-label")).toHaveTextContent("INTERRUPTED");
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel task" })).toBeNull();
  });
});

describe("RecoveryPanel", () => {
  const handlers = () => ({ onResume: vi.fn().mockResolvedValue(undefined), onRestart: vi.fn().mockResolvedValue(undefined), onCancel: vi.fn().mockResolvedValue(undefined) });

  it("says what happened and offers resume, restart and cancel as separate, explicit choices", async () => {
    const h = handlers();
    render(<RecoveryPanel interruption={interruption()} {...h} />);
    const panel = screen.getByTestId("recovery-panel");
    expect(panel).toHaveTextContent("Nothing runs until you choose what to do");
    expect(panel).toHaveTextContent("1 step completed, 2 remaining");
    expect(panel).toHaveTextContent("were not undone");
    expect(screen.queryByRole("alert")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Resume safely" }));
    expect(h.onResume).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Restart as a new task" }));
    expect(h.onRestart).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Cancel task" }));
    expect(h.onCancel).toHaveBeenCalledTimes(1);
  });

  it("warns when an action's outcome is unknown and labels each in-flight action honestly", () => {
    render(
      <RecoveryPanel
        {...handlers()}
        interruption={interruption({
          needs_attention: true,
          in_flight: [
            { tool_call_id: "tc_1", tool: "browser_click", step_id: "s2", certainty: "uncertain", detail: "cannot be determined", arguments: {} },
            { tool_call_id: "tc_2", tool: "fs_move", step_id: "s2", certainty: "confirmed_done", detail: "the item is at the destination", arguments: {} },
          ],
        })}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/could not be determined whether it took effect/);
    const items = screen.getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("browser_click");
    expect(items[0]).toHaveTextContent("needs your approval before it is repeated");
    expect(items[1]).toHaveTextContent("will not be repeated");
  });

  it("does not offer resume for a task that cannot be resumed", () => {
    render(<RecoveryPanel {...handlers()} interruption={interruption({ resumable: false, options: ["restart", "cancel", "inspect"] })} />);
    expect(screen.queryByRole("button", { name: "Resume safely" })).toBeNull();
    expect(screen.getByRole("button", { name: "Restart as a new task" })).toBeEnabled();
    expect(screen.getByTestId("recovery-panel")).toHaveTextContent("cannot be resumed");
  });

  it("shows the backend's refusal (e.g. emergency stop engaged) and stays usable", async () => {
    const h = { ...handlers(), onResume: vi.fn().mockRejectedValue(new Error("The emergency stop is engaged. Release it before resuming a task.")) };
    render(<RecoveryPanel interruption={interruption()} {...h} />);
    await userEvent.click(screen.getByRole("button", { name: "Resume safely" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("emergency stop is engaged");
    await waitFor(() => expect(screen.getByRole("button", { name: "Cancel task" })).toBeEnabled());
  });
});

describe("rollback of a finished task (NEXT-D)", () => {
  it("shows the rollback in the timeline without changing the task's outcome", () => {
    let v = buildTaskView("task_1", happyPath());
    const status = v.status;
    v = reduceEvent(v, ev("rollback_started", { message: "Rollback requested: 3 change(s) to look at", data: { open: 3 } }));
    expect(v.timeline.at(-1)?.title).toBe("Rolling back file changes");
    expect(v.timeline.at(-1)?.icon).toBe("running");
    v = reduceEvent(v, ev("rollback_completed", { message: "Rollback finished: 2 undone, 1 left as they are", data: { open: 0 } }));
    expect(v.status).toBe(status);
    expect(v.timeline.filter((t) => t.title === "Rolling back file changes")[0]?.icon).toBe("done");
    expect(v.timeline.at(-1)).toMatchObject({ title: "Rollback finished", icon: "done" });
  });

  it("marks a rollback that left changes open as needing attention", () => {
    let v = buildTaskView("task_1", happyPath());
    v = reduceEvent(v, ev("rollback_started", { data: { open: 2 } }));
    v = reduceEvent(v, ev("rollback_completed", { message: "Rollback stopped", data: { open: 1 } }));
    expect(v.timeline.at(-1)?.icon).toBe("warn");
  });
});
