import type { ApprovalRequest, ExecutionEvent, EventType, TaskResult, UsageStats } from "../src/types/api";

let seq = 0;
export const resetSeq = () => {
  seq = 0;
};

export function ev(type: EventType, extra: Partial<ExecutionEvent> & { data?: Record<string, unknown> } = {}): ExecutionEvent {
  seq += 1;
  return {
    id: `evt_${seq}`,
    task_id: "task_1",
    seq,
    type,
    timestamp: new Date(2026, 0, 1, 12, 0, seq).toISOString(),
    correlation: { task_id: "task_1", ...(extra.correlation ?? {}) },
    message: extra.message ?? "",
    data: extra.data ?? {},
  };
}

export const usage: UsageStats = {
  llm_calls: 2, vision_calls: 0, tool_calls: 3, actions: 3, shell_operations: 0, retries: 0, recoveries: 0,
  replans: 0, approvals: 1, input_tokens: 200, output_tokens: 80, llm_latency_ms: 20, estimated_cost_usd: 0.0021,
  cache_hits: 0, verification_failures: 0,
};

export const approval = (over: Partial<ApprovalRequest> = {}): ApprovalRequest => ({
  id: "apr_1", task_id: "task_1", step_id: "s1", tool_call_id: "tc_1", tool: "fs_delete",
  action: "Delete file C:\\Users\\me\\Documents\\old-backup.zip", reason: "You asked me to delete the old backup",
  resource: "C:\\Users\\me\\Documents\\old-backup.zip", risk: 3,
  risk_explanation: ["Removes the item from its current location (sent to the Recycle Bin)"],
  arguments: { path: "@Documents/old-backup.zip" }, allowed_scopes: ["once", "task"], tainted: false,
  status: "PENDING", category: "file_delete", created_at: new Date().toISOString(), expires_at: null, decided_at: null,
  decision_note: null, scope_granted: null, ...over,
});

export const result = (over: Partial<TaskResult> = {}): TaskResult => ({
  status: "COMPLETED", summary: "Completed: 2/2 steps succeeded and the result was verified.", verified: true,
  verification: [], artifacts: {}, error: null, answer: null, duration_s: 1.2, usage, ...over,
});

/** A realistic successful run with one approval. */
export function happyPath(): ExecutionEvent[] {
  resetSeq();
  const corr = (step: string, tc?: string) => ({ correlation: { step_id: step, tool_call_id: tc ?? null } });
  return [
    ev("task_created", { message: "make a folder", data: { task: { instruction: "make a folder", id: "task_1", status: "CREATED" } } }),
    ev("status_changed", { data: { status: "PLANNING" } }),
    ev("planning_started", { message: "Understanding the request and planning" }),
    ev("plan_created", { message: "Make a folder", data: { plan: { id: "plan_1", version: 1, summary: "Make a folder", assumptions: [], success_criteria: [], steps: [
      { id: "s1", title: "Create folder", kind: "tool", tool: "fs_create", args: {}, goal: null, depends_on: [], rationale: "", verify: [], optional: false, status: "PENDING", attempts: 0, summary: "", error: null },
    ] } } }),
    ev("status_changed", { data: { status: "EXECUTING" } }),
    ev("step_started", { message: "Create folder", data: { title: "Create folder", step_id: "s1" }, ...corr("s1") }),
    ev("tool_selected", { message: "fs_create: Create folder X", data: { tool: "fs_create", capability: "filesystem", resource: "C:\\x", risk: "medium" }, ...corr("s1", "tc_1") }),
    ev("action_started", { ...corr("s1", "tc_1") }),
    ev("action_completed", { message: "Created folder: C:\\x", ...corr("s1", "tc_1") }),
    ev("step_completed", { message: "Created folder: C:\\x", data: { step_id: "s1" }, ...corr("s1") }),
    ev("verification_started", { message: "Verifying task: 1 check(s)" }),
    ev("verification_completed", { message: "Verification task: 1/1 passed", data: { passed: true } }),
    ev("task_completed", { message: "Completed", data: { result: result() } }),
  ];
}
