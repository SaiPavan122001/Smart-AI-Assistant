import type {
  ApprovalRequest,
  ExecutionEvent,
  Plan,
  TaskResult,
  TaskStatus,
  UsageStats,
} from "../../types/api";

export type TimelineIcon = "done" | "running" | "warn" | "error" | "info";

export interface TimelineItem {
  key: string;
  icon: TimelineIcon;
  title: string;
  detail?: string;
  ts: string;
  seq: number;
  stepId?: string;
  children: TimelineItem[];
}

export interface TaskView {
  taskId: string;
  instruction: string;
  status: TaskStatus;
  paused: boolean;
  lastSeq: number;
  plan: Plan | null;
  timeline: TimelineItem[];
  pendingApprovals: ApprovalRequest[];
  resolvedApprovals: ApprovalRequest[];
  currentTool: string | null;
  currentApplication: string | null;
  currentStepId: string | null;
  currentStepTitle: string | null;
  usage: UsageStats | null;
  result: TaskResult | null;
  killSwitch: boolean;
  /** Latest checkpoint: a progress snapshot, not a resume point. */
  progress: { completed: string[]; remaining: string[]; outstanding: string[] } | null;
}

export const emptyView = (taskId = ""): TaskView => ({
  taskId,
  instruction: "",
  status: "CREATED",
  paused: false,
  lastSeq: 0,
  plan: null,
  timeline: [],
  pendingApprovals: [],
  resolvedApprovals: [],
  currentTool: null,
  currentApplication: null,
  currentStepId: null,
  currentStepTitle: null,
  usage: null,
  result: null,
  killSwitch: false,
  progress: null,
});

const str = (v: unknown, d = ""): string => (typeof v === "string" ? v : d);

function item(ev: ExecutionEvent, key: string, icon: TimelineIcon, title: string, detail?: string): TimelineItem {
  return { key, icon, title, detail, ts: ev.timestamp, seq: ev.seq, stepId: ev.correlation.step_id ?? undefined, children: [] };
}

function upsert(list: TimelineItem[], it: TimelineItem): TimelineItem[] {
  const i = list.findIndex((x) => x.key === it.key);
  if (i === -1) return [...list, it];
  const next = [...list];
  const old = next[i]!;
  next[i] = { ...old, ...it, children: it.children.length ? it.children : old.children, seq: old.seq };
  return next;
}

/** Put `it` under its step when that step exists, else at top level. */
function place(timeline: TimelineItem[], it: TimelineItem): TimelineItem[] {
  if (it.stepId) {
    const si = timeline.findIndex((x) => x.key === `step:${it.stepId}`);
    if (si !== -1) {
      const step = timeline[si]!;
      const next = [...timeline];
      next[si] = { ...step, children: upsert(step.children, it) };
      return next;
    }
  }
  return upsert(timeline, it);
}

function patchChild(timeline: TimelineItem[], key: string, patch: Partial<TimelineItem>): TimelineItem[] {
  return timeline.map((t) => {
    if (t.key === key) return { ...t, ...patch };
    if (t.children.some((c) => c.key === key)) {
      return { ...t, children: t.children.map((c) => (c.key === key ? { ...c, ...patch } : c)) };
    }
    return t;
  });
}

const FINAL_TITLE: Record<string, [TimelineIcon, string]> = {
  task_completed: ["done", "Verified final state — task completed"],
  task_failed: ["error", "Task failed"],
  task_cancelled: ["warn", "Task cancelled"],
  task_timed_out: ["error", "Task timed out"],
};

export function reduceEvent(state: TaskView, ev: ExecutionEvent): TaskView {
  if (ev.seq && ev.seq <= state.lastSeq) return state; // idempotent
  const s: TaskView = { ...state, lastSeq: Math.max(state.lastSeq, ev.seq) };
  const d = ev.data ?? {};
  const stepId = ev.correlation.step_id ?? undefined;

  switch (ev.type) {
    case "task_created": {
      const task = d.task as { instruction?: string } | undefined;
      s.taskId = ev.task_id;
      s.instruction = task?.instruction ?? ev.message;
      s.timeline = upsert(s.timeline, item(ev, "understand", "running", "Understanding request"));
      break;
    }
    case "status_changed":
      s.status = str(d.status, s.status) as TaskStatus;
      break;
    case "planning_started":
      s.timeline = upsert(s.timeline, item(ev, `planning:${ev.seq}`, "running", ev.message || "Planning"));
      break;
    case "plan_created":
    case "plan_revised": {
      const plan = d.plan as Plan | undefined;
      if (plan) s.plan = plan;
      const n = plan?.steps.length ?? 0;
      s.timeline = s.timeline.map((t) => (t.key === "understand" || t.key.startsWith("planning:") ? { ...t, icon: "done" as const } : t));
      s.timeline = upsert(
        s.timeline,
        item(ev, `plan:${ev.seq}`, "done", ev.type === "plan_created" ? "Created execution plan" : `Revised plan (v${plan?.version ?? "?"})`, `${n} step${n === 1 ? "" : "s"}${ev.message ? " — " + ev.message : ""}`),
      );
      break;
    }
    case "step_started":
      s.currentStepId = stepId ?? null;
      s.currentStepTitle = str(d.title, ev.message) || ev.message;
      s.timeline = upsert(s.timeline, item(ev, `step:${stepId}`, "running", ev.message));
      break;
    case "step_completed":
      s.timeline = patchChild(s.timeline, `step:${stepId}`, { icon: "done", detail: ev.message });
      break;
    case "step_failed":
      s.timeline = d.skipped
        ? upsert(s.timeline, item(ev, `step:${stepId}`, "info", "Skipped", ev.message))
        : patchChild(s.timeline, `step:${stepId}`, { icon: "error", detail: ev.message });
      break;
    case "tool_selected":
      s.currentTool = str(d.tool) || s.currentTool;
      s.timeline = place(s.timeline, item(ev, `tool:${ev.correlation.tool_call_id ?? ev.seq}`, "running", `Using ${str(d.tool)}`, ev.message));
      break;
    case "action_started":
      break;
    case "action_completed":
      s.timeline = patchChild(s.timeline, `tool:${ev.correlation.tool_call_id}`, { icon: "done", detail: ev.message });
      break;
    case "action_failed":
      s.timeline = patchChild(s.timeline, `tool:${ev.correlation.tool_call_id}`, { icon: "error", detail: ev.message });
      break;
    case "observation_received":
      if (!d.cached) s.timeline = place(s.timeline, item(ev, `obs:${ev.seq}`, "info", `Observed (${str(d.source, "state")})`, ev.message));
      break;
    case "approval_required": {
      const a = d.approval as ApprovalRequest | undefined;
      if (a) {
        s.pendingApprovals = [...s.pendingApprovals.filter((x) => x.id !== a.id), a];
        s.timeline = place(s.timeline, item(ev, `apr:${a.id}`, "warn", "Approval required", a.action));
      }
      break;
    }
    case "approval_granted":
    case "approval_denied": {
      const id = str(d.approval_id);
      const granted = ev.type === "approval_granted";
      const found = s.pendingApprovals.find((x) => x.id === id);
      s.pendingApprovals = s.pendingApprovals.filter((x) => x.id !== id);
      if (found) s.resolvedApprovals = [...s.resolvedApprovals, { ...found, status: granted ? "APPROVED" : "DENIED" }];
      s.timeline = patchChild(s.timeline, `apr:${id}`, { icon: granted ? "done" : "error", title: granted ? "Approval received" : "Approval denied" });
      break;
    }
    case "injection_detected":
      s.timeline = place(s.timeline, item(ev, `inj:${ev.seq}`, "warn", "Untrusted content contained instructions — ignored", ev.message));
      break;
    case "recovery_started":
      s.timeline = place(s.timeline, item(ev, `rec:${ev.seq}`, "warn", `Recovering (${str(d.strategy).replace(/_/g, " ")})`, ev.message));
      break;
    case "recovery_completed":
      s.timeline = place(s.timeline, item(ev, `recdone:${ev.seq}`, "done", "Recovered", ev.message));
      break;
    case "verification_started":
      s.timeline = place(s.timeline, item(ev, `verify:${stepId ?? "task"}`, "running", "Verifying", ev.message));
      break;
    case "verification_completed": {
      const ok = d.passed === true;
      // The user's own requirements are a separate line: a passing plan check must not hide an unmet requirement.
      const goal = d.scope === "goal";
      const title = goal ? (ok ? "Requirements met" : "Requirements not met") : ok ? "Verified" : "Verification failed";
      s.timeline = place(s.timeline, item(ev, goal ? "verify:goal" : `verify:${stepId ?? "task"}`, ok ? "done" : "error", title, ev.message));
      break;
    }
    case "checkpoint_saved": {
      const list = (v: unknown): string[] => (Array.isArray(v) ? v.map(String) : []);
      s.progress = { completed: list(d.completed), remaining: list(d.remaining), outstanding: list(d.outstanding_requirements) };
      // one line, updated in place: a checkpoint per step must not flood the timeline
      s.timeline = upsert(s.timeline, item(ev, "checkpoint", "info", "Progress checkpoint", ev.message));
      break;
    }
    case "usage_updated":
      s.usage = d as unknown as UsageStats;
      break;
    // Pause lifecycle: requested -> (status PAUSED) -> completed; resume: requested -> (status restored) -> completed.
    // `paused` covers the whole span, so the UI never offers "Pause" or shows a plain running state in between.
    case "pause_requested":
      s.paused = true;
      s.timeline = upsert(s.timeline, item(ev, `pause:${ev.seq}`, "info", "Pause requested", ev.message));
      break;
    case "pause_completed":
    case "task_paused": // legacy event name
      s.paused = true;
      s.timeline = upsert(s.timeline, item(ev, `paused:${ev.seq}`, "info", "Paused", ev.message));
      break;
    case "resume_requested":
      s.timeline = upsert(s.timeline, item(ev, `resume:${ev.seq}`, "info", "Resume requested"));
      break;
    case "resume_completed":
    case "task_resumed": // legacy event name
      s.paused = false;
      s.timeline = upsert(s.timeline, item(ev, `resumed:${ev.seq}`, "info", "Resumed"));
      break;
    case "limit_reached":
      s.timeline = upsert(s.timeline, item(ev, `limit:${ev.seq}`, "error", "Resource limit reached", ev.message));
      break;
    case "kill_switch":
      s.killSwitch = true;
      s.timeline = upsert(s.timeline, item(ev, `kill:${ev.seq}`, "error", "EMERGENCY STOP", ev.message));
      break;
    case "task_completed":
    case "task_failed":
    case "task_cancelled":
    case "task_timed_out": {
      const [icon0, title0] = FINAL_TITLE[ev.type]!;
      const result = d.result as TaskResult | undefined;
      const partial = ev.type === "task_failed" && result?.outcome === "PARTIAL";
      const icon: TimelineIcon = partial ? "warn" : icon0;
      const title = partial ? "Task only partly completed" : title0;
      if (result) {
        s.result = result;
        s.usage = result.usage;
      }
      s.status = ({ task_completed: "COMPLETED", task_failed: "FAILED", task_cancelled: "CANCELLED", task_timed_out: "TIMED_OUT" } as const)[ev.type];
      s.currentTool = null;
      s.currentApplication = null;
      s.currentStepId = null;
      s.pendingApprovals = [];
      // anything still "running" is not running anymore
      s.timeline = s.timeline.map((t) => settle(t));
      s.timeline = upsert(s.timeline, item(ev, `final`, icon, title, result?.summary ?? ev.message));
      break;
    }
  }
  if (ev.type === "tool_selected") {
    const label = APP_LABEL[str(d.capability)];
    if (label) {
      const res = str(d.resource).slice(0, 60);
      s.currentApplication = res ? `${label} · ${res}` : label;
    }
  }
  return s;
}

const APP_LABEL: Record<string, string> = {
  browser: "Browser",
  desktop: "Desktop",
  filesystem: "File system",
  terminal: "Terminal",
  vision: "Screen capture",
  verification: "Verification",
};

function settle(t: TimelineItem): TimelineItem {
  return { ...t, icon: t.icon === "running" ? "info" : t.icon, children: t.children.map(settle) };
}

export function buildTaskView(taskId: string, events: ExecutionEvent[]): TaskView {
  return events.reduce(reduceEvent, emptyView(taskId));
}

export const STATUS_LABEL: Record<TaskStatus, string> = {
  CREATED: "Created",
  PLANNING: "Planning",
  WAITING_APPROVAL: "Waiting for your approval",
  EXECUTING: "Executing",
  RECOVERING: "Recovering from a problem",
  VERIFYING: "Verifying the result",
  PAUSED: "Paused: nothing runs until you resume",
  COMPLETED: "Completed",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
  TIMED_OUT: "Timed out",
};
