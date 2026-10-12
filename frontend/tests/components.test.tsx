import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EmergencyStop } from "../src/components/EmergencyStop";
import { ApprovalDialog } from "../src/features/approvals/ApprovalDialog";
import { ActivityTimeline } from "../src/features/tasks/ActivityTimeline";
import { TaskInput } from "../src/features/tasks/TaskInput";
import { buildTaskView } from "../src/features/tasks/taskView";
import { ResultCard } from "../src/features/tasks/ResultCard";
import { AgentProvider } from "../src/hooks/AgentContext";
import { api } from "../src/services/api";
import { approval, happyPath, result } from "./fixtures";

afterEach(() => vi.restoreAllMocks());

describe("ApprovalDialog", () => {
  it("shows exactly what, why, which resource and what risk — not a vague prompt", () => {
    render(<ApprovalDialog request={approval()} onApprove={vi.fn()} onDeny={vi.fn()} />);
    const dlg = screen.getByRole("alertdialog");
    expect(dlg).toHaveTextContent("Delete file C:\\Users\\me\\Documents\\old-backup.zip");
    expect(dlg).toHaveTextContent("You asked me to delete the old backup");
    expect(dlg).toHaveTextContent("C:\\Users\\me\\Documents\\old-backup.zip");
    expect(dlg).toHaveTextContent("sent to the Recycle Bin");
    expect(dlg).toHaveTextContent("high risk");
    expect(dlg).not.toHaveTextContent(/^Allow action\?$/);
    expect(screen.queryByText(/prompt injection/i)).toBeNull();
  });

  it("focuses Deny first (the safe choice) and Escape denies", async () => {
    const onDeny = vi.fn();
    render(<ApprovalDialog request={approval()} onApprove={vi.fn()} onDeny={onDeny} />);
    expect(screen.getByRole("button", { name: "Deny" })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(onDeny).toHaveBeenCalledWith("apr_1"));
  });

  it("offers once / task scope as allowed, and hides task scope when not permitted", async () => {
    const onApprove = vi.fn();
    const { rerender } = render(<ApprovalDialog request={approval()} onApprove={onApprove} onDeny={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Approve for this task" }));
    expect(onApprove).toHaveBeenCalledWith("apr_1", "task");
    await userEvent.click(screen.getByRole("button", { name: "Approve once" }));
    expect(onApprove).toHaveBeenLastCalledWith("apr_1", "once");
    rerender(<ApprovalDialog request={approval({ allowed_scopes: ["once"], risk: 4 })} onApprove={onApprove} onDeny={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Approve for this task" })).toBeNull();
    expect(screen.getByRole("alertdialog")).toHaveTextContent("critical risk");
  });

  it("warns loudly when the task has been exposed to prompt-injection content", () => {
    render(<ApprovalDialog request={approval({ tainted: true })} onApprove={vi.fn()} onDeny={vi.fn()} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/instructions aimed at it/i);
    expect(screen.getByRole("alert")).toHaveTextContent(/only if you asked/i);
  });

  it("shows queue length and surfaces decision errors without closing", async () => {
    const onDeny = vi.fn().mockRejectedValue(new Error("Approval apr_1 is already DENIED"));
    render(<ApprovalDialog request={approval()} queued={2} onApprove={vi.fn()} onDeny={onDeny} />);
    expect(screen.getByText(/2 more approvals waiting/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Deny" }));
    expect(await screen.findByText(/already DENIED/)).toBeInTheDocument();
  });

  it("includes technical details in a collapsible section", async () => {
    render(<ApprovalDialog request={approval()} onApprove={vi.fn()} onDeny={vi.fn()} />);
    await userEvent.click(screen.getByText("Technical details"));
    expect(screen.getByText(/tool: fs_delete/)).toBeInTheDocument();
  });
});

describe("EmergencyStop", () => {
  const status = (over = {}) => ({ kill_switch_engaged: false, kill_switch_reason: "", active_tasks: [] as string[], pending_approvals: 0, ...over });
  beforeEach(() => {
    vi.spyOn(api, "agentStatus").mockResolvedValue(status({ active_tasks: ["t1"] }));
  });

  it("is always present, pulses while a task runs, and stops with ONE click (no confirmation)", async () => {
    const stop = vi.spyOn(api, "stopAgent").mockResolvedValue({ stopped_tasks: ["t1"], kill_switch_engaged: true });
    render(<AgentProvider pollMs={60_000}><EmergencyStop /></AgentProvider>);
    const btn = await screen.findByTestId("stop-agent");
    await waitFor(() => expect(btn).toHaveClass("armed"));
    expect(btn).toHaveTextContent("STOP AGENT");
    vi.spyOn(api, "agentStatus").mockResolvedValue(status({ kill_switch_engaged: true, kill_switch_reason: "STOP" }));
    await userEvent.click(btn);
    expect(stop).toHaveBeenCalledTimes(1);
    expect(await screen.findByTestId("kill-engaged")).toHaveTextContent(/engaged/i);
  });

  it("lets the user release the stop", async () => {
    vi.spyOn(api, "agentStatus").mockResolvedValue(status({ kill_switch_engaged: true }));
    const release = vi.spyOn(api, "releaseAgent").mockResolvedValue({ kill_switch_engaged: false });
    render(<AgentProvider pollMs={60_000}><EmergencyStop /></AgentProvider>);
    vi.spyOn(api, "agentStatus").mockResolvedValue(status());
    await userEvent.click(await screen.findByRole("button", { name: "Release stop" }));
    expect(release).toHaveBeenCalled();
    expect(await screen.findByTestId("stop-agent")).toBeInTheDocument();
  });
});

describe("ActivityTimeline", () => {
  it("renders reconstructed steps with accessible status icons", () => {
    const view = buildTaskView("task_1", happyPath());
    render(<ActivityTimeline items={view.timeline} />);
    const items = screen.getAllByTestId("timeline-item");
    expect(items.length).toBeGreaterThan(4);
    expect(screen.getByText("Create folder")).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: "done" }).length).toBeGreaterThan(2);
    expect(screen.getByText(/task completed/i)).toBeInTheDocument();
  });

  it("shows a helpful empty state", () => {
    render(<ActivityTimeline items={[]} />);
    expect(screen.getByText(/Describe what you want done/)).toBeInTheDocument();
  });
});

describe("TaskInput", () => {
  it("is disabled when empty, submits via button and Ctrl+Enter, and clears afterwards", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<TaskInput onSubmit={onSubmit} />);
    const run = screen.getByRole("button", { name: "Execute" });
    expect(run).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/what should i do/i), "make a folder");
    expect(run).toBeEnabled();
    await userEvent.click(run);
    expect(onSubmit).toHaveBeenCalledWith("make a folder");
    await waitFor(() => expect(screen.getByLabelText(/what should i do/i)).toHaveValue(""));
    await userEvent.type(screen.getByLabelText(/what should i do/i), "second{Control>}{Enter}{/Control}");
    await waitFor(() => expect(onSubmit).toHaveBeenLastCalledWith("second"));
  });

  it("starts exactly ONE task when submitted twice in the same tick (execution-loop regression)", async () => {
    let release: () => void = () => {};
    const onSubmit = vi.fn().mockImplementation(() => new Promise<void>((r) => (release = r)));
    render(<TaskInput onSubmit={onSubmit} />);
    const box = screen.getByLabelText(/what should i do/i);
    await userEvent.type(box, "open notepad");
    const form = screen.getByRole("form", { name: "New task" });
    fireEvent.submit(form);
    fireEvent.submit(form); // double click / double Ctrl+Enter before React re-renders
    expect(onSubmit).toHaveBeenCalledTimes(1);
    release();
    await waitFor(() => expect(box).toHaveValue(""));
  });

  it("keeps the text when starting fails so the user does not lose it", async () => {
    const onSubmit = vi.fn().mockRejectedValue(new Error("boom"));
    render(<TaskInput onSubmit={onSubmit} />);
    await userEvent.type(screen.getByLabelText(/what should i do/i), "keep me");
    await userEvent.click(screen.getByRole("button", { name: "Execute" }));
    await waitFor(() => expect(screen.getByLabelText(/what should i do/i)).toHaveValue("keep me"));
  });

  it("offers example prompts", async () => {
    render(<TaskInput onSubmit={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /Java-related PDFs/ }));
    expect((screen.getByLabelText(/what should i do/i) as HTMLTextAreaElement).value).toContain("Java-related PDFs");
  });
});

describe("ResultCard", () => {
  it("labels model-judged results as lower assurance", () => {
    render(<MemoryRouter><ResultCard result={result({ verified: false })} /></MemoryRouter>);
    expect(screen.getByText(/lower assurance/i)).toBeInTheDocument();
    expect(screen.getByText("not verified")).toBeInTheDocument();
  });

  it("never presents the text of a failed task as an answer", () => {
    render(<ResultCard result={result({
      status: "FAILED", verified: false, outcome: "DENIED", verification_status: "NOT_RUN", answer_status: "WITHHELD",
      summary: "Failed: 0/1 steps succeeded.",
      answer: "The task was not completed: a required action was not approved, so it was not performed.",
    })} />);
    expect(screen.queryByText("Answer:")).not.toBeInTheDocument();
    expect(screen.getByTestId("outcome-statement")).toHaveTextContent(/not approved/);
    expect(screen.getByText("not verified")).toBeInTheDocument();
  });

  it("does not show an answer for a failed task even on records without answer_status", () => {
    render(<ResultCard result={result({ status: "FAILED", verified: false, answer: "The file has been deleted." })} />);
    expect(screen.queryByText(/has been deleted/)).not.toBeInTheDocument();
  });

  it("says when a completed task could not provide the requested data", () => {
    render(<ResultCard result={result({
      verified: false, verification_status: "INCOMPLETE", answer_status: "answer_unavailable",
      answer: "The task completed, but the requested response data was unavailable.",
    })} />);
    expect(screen.queryByText("Answer:")).not.toBeInTheDocument();
    expect(screen.getByTestId("answer-unavailable")).toBeInTheDocument();
    expect(screen.queryByText(/lower assurance/i)).not.toBeInTheDocument();
  });

  it("shows verified results with their checks and usage", () => {
    render(<ResultCard result={result({ verification: [{ id: "v1", step_id: null, kind: "file_exists", passed: true, description: "", message: "file exists", method: "deterministic", evidence: {} }] })} />);
    expect(screen.getByText("verified")).toBeInTheDocument();
    expect(screen.getByText(/1\/1 verification checks passed/)).toBeInTheDocument();
    expect(screen.getByText(/2 LLM calls/)).toBeInTheDocument();
  });
});

describe("ResultCard: complex workflow reporting", () => {
  const report = {
    completed: [{ id: "s1", title: "Find the PDFs", status: "SUCCEEDED", summary: "" }, { id: "s2", title: "Move them", status: "SUCCEEDED", summary: "" }],
    failed: [{ id: "s4", title: "Write the summary", status: "FAILED", summary: "not grounded" }],
    not_run: [],
    requirements: [
      { id: "r1", kind: "artifact", text: "save the output as Java-summary.md", status: "met" as const, evidence: "C:/ws/Java-summary.md (315 bytes)" },
      { id: "r2", kind: "grounded_summary", text: "the saved summary must be based on the content that was actually read", status: "unmet" as const, evidence: "only 10% of its words occur in what was read" },
    ],
    effects: ["fs_organize", "fs_create"], replans: 1, recoveries: 0,
  };

  it("shows a PARTIAL result with what was done, what was not, and each requirement", () => {
    render(<ResultCard result={result({
      status: "FAILED", verified: false, outcome: "PARTIAL", verification_status: "FAILED", answer_status: "WITHHELD",
      answer: "The task was only partly completed; the request as a whole was NOT fulfilled.", report,
    })} />);
    expect(screen.getByTestId("result-status")).toHaveTextContent("PARTIAL");
    expect(screen.queryByText("Answer:")).not.toBeInTheDocument();
    const box = screen.getByTestId("completion-report");
    expect(box).toHaveTextContent("Done: Find the PDFs; Move them");
    expect(box).toHaveTextContent("Failed: Write the summary");
    expect(box).toHaveTextContent("Completed actions were not undone");
    const items = screen.getAllByRole("listitem").filter((li) => li.getAttribute("data-status"));
    expect(items.map((li) => li.getAttribute("data-status"))).toEqual(["met", "unmet"]);
    expect(items[1]).toHaveTextContent("only 10%");
  });

  it("a plain failure keeps its FAILED label", () => {
    render(<ResultCard result={result({ status: "FAILED", verified: false, outcome: "FAILED", answer_status: "WITHHELD", answer: "no", report: { ...report, requirements: [] } })} />);
    expect(screen.getByTestId("result-status")).toHaveTextContent("FAILED");
  });
});
