// Types mirroring the backend contracts (backend/app/models, backend/app/api/schemas.py).

export type TaskStatus =
  | "CREATED"
  | "PLANNING"
  | "WAITING_APPROVAL"
  | "EXECUTING"
  | "RECOVERING"
  | "VERIFYING"
  | "PAUSED"
  | "INTERRUPTED"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED"
  | "TIMED_OUT";

export const TERMINAL_STATUSES: readonly TaskStatus[] = ["COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"];
export const isTerminal = (s: TaskStatus): boolean => TERMINAL_STATUSES.includes(s);

export type StepStatus = "PENDING" | "RUNNING" | "WAITING_APPROVAL" | "SUCCEEDED" | "FAILED" | "SKIPPED";
export type RiskLevel = 1 | 2 | 3 | 4;
export const RISK_LABEL: Record<RiskLevel, string> = { 1: "low", 2: "medium", 3: "high", 4: "critical" };

export interface ErrorInfo {
  code: string;
  message: string;
  retryable: boolean;
  details: Record<string, unknown>;
}

export interface UsageStats {
  llm_calls: number;
  vision_calls: number;
  tool_calls: number;
  actions: number;
  shell_operations: number;
  retries: number;
  recoveries: number;
  replans: number;
  approvals: number;
  input_tokens: number;
  output_tokens: number;
  llm_latency_ms: number;
  estimated_cost_usd: number;
  cache_hits: number;
  verification_failures: number;
  // per-purpose model calls and verification / pause accounting (absent on older records)
  planner_calls?: number;
  replanner_calls?: number;
  repair_calls?: number;
  step_calls?: number;
  judge_calls?: number;
  answer_generation_calls?: number;
  llm_budget?: number;
  verification_attempts?: number;
  verification_polls?: number;
  verification_duration_ms?: number;
  verification_success_ms?: number;
  pause_count?: number;
  resume_count?: number;
}

/** Why a task ended (finer than TaskStatus: several map to FAILED). */
export type TaskOutcome = "COMPLETED" | "FAILED" | "CANCELLED" | "TIMED_OUT" | "DENIED" | "LOOP_DETECTED" | "PLAN_INVALID" | "PARTIAL";
export type VerificationStatus = "VERIFIED" | "JUDGED" | "INCOMPLETE" | "FAILED" | "NOT_RUN";
/** How to read `answer`: only ANSWERED is a success answer. WITHHELD text describes a failure. */
export type AnswerStatus = "ANSWERED" | "NOT_REQUESTED" | "answer_unavailable" | "WITHHELD";

export interface VerificationSpec {
  kind: string;
  params: Record<string, unknown>;
  description: string;
}

export interface VerificationResult {
  id: string;
  step_id: string | null;
  kind: string;
  passed: boolean;
  description: string;
  message: string;
  method: string;
  evidence: Record<string, unknown>;
}

export interface PlanStep {
  id: string;
  title: string;
  kind: "tool" | "adaptive";
  tool: string | null;
  args: Record<string, unknown>;
  goal: string | null;
  depends_on: string[];
  rationale: string;
  verify: VerificationSpec[];
  optional: boolean;
  status: StepStatus;
  attempts: number;
  summary: string;
  error: ErrorInfo | null;
}

export interface Plan {
  id: string;
  version: number;
  summary: string;
  assumptions: string[];
  steps: PlanStep[];
  success_criteria: VerificationSpec[];
}

export type RequirementStatus = "met" | "unmet" | "unverified";

export interface RequirementResult {
  id: string;
  kind: string;
  text: string;
  status: RequirementStatus;
  evidence: string;
}

export interface StepReport {
  id: string;
  title: string;
  status: string;
  summary: string;
}

/** What a finished task actually did: steps done / failed / not run, and the user's requirements. */
export interface CompletionReport {
  completed: StepReport[];
  failed: StepReport[];
  interrupted?: StepReport[];
  not_run: StepReport[];
  requirements: RequirementResult[];
  effects: string[];
  replans: number;
  recoveries: number;
}

export interface TaskResult {
  status: TaskStatus;
  summary: string;
  verified: boolean;
  verification: VerificationResult[];
  artifacts: Record<string, unknown>;
  error: ErrorInfo | null;
  outcome?: TaskOutcome;
  verification_status?: VerificationStatus;
  answer_status?: AnswerStatus;
  answer: string | null;
  report?: CompletionReport | null;
  duration_s: number;
  usage: UsageStats;
}

export interface TaskSummary {
  id: string;
  instruction: string;
  status: TaskStatus;
  paused: boolean;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  current_step_id: string | null;
  current_tool: string | null;
  current_application: string | null;
  usage: UsageStats;
  summary: string | null;
  verified: boolean | null;
  restarted_from?: string | null;
}

export interface Task extends Omit<TaskSummary, "summary" | "verified"> {
  plan: Plan | null;
  result: TaskResult | null;
  interruption?: Interruption | null;
}

/** What is known about an action that had started when the service stopped. */
export type ActionCertainty = "safe_to_retry" | "confirmed_done" | "confirmed_not_done" | "uncertain";
export type RecoveryOption = "resume" | "restart" | "cancel" | "inspect";

export interface InFlightAction {
  tool_call_id: string;
  tool: string;
  step_id: string | null;
  certainty: ActionCertainty;
  detail: string;
  arguments: Record<string, unknown>;
}

/** A task the service left unfinished. It waits for the user: resume, restart or cancel. */
export interface Interruption {
  at: string;
  reason: string;
  status_at_interruption: TaskStatus;
  was_paused: boolean;
  awaiting_approval: boolean;
  interrupted_step: string | null;
  in_flight: InFlightAction[];
  completed_steps: string[];
  remaining_steps: string[];
  resumable: boolean;
  needs_attention: boolean;
  notes: string[];
  options: RecoveryOption[];
}

export interface RecoveryView {
  task: TaskSummary;
  interruption: Interruption;
  report: CompletionReport;
}

export type ApprovalScope = "once" | "task";
export type ApprovalStatus = "PENDING" | "APPROVED" | "DENIED" | "EXPIRED" | "CANCELLED";

export interface ApprovalRequest {
  id: string;
  task_id: string;
  step_id: string | null;
  tool_call_id: string;
  tool: string;
  action: string;
  reason: string;
  resource: string;
  risk: RiskLevel;
  risk_explanation: string[];
  arguments: Record<string, unknown>;
  allowed_scopes: ApprovalScope[];
  tainted: boolean;
  status: ApprovalStatus;
  category: string;
  created_at: string;
  expires_at: string | null;
  decided_at: string | null;
  decision_note: string | null;
  scope_granted: ApprovalScope | null;
}

export type EventType =
  | "task_created" | "planning_started" | "plan_created" | "plan_revised" | "step_started" | "step_completed"
  | "step_failed" | "tool_selected" | "approval_required" | "approval_granted" | "approval_denied"
  | "action_started" | "action_completed" | "action_failed" | "observation_received" | "injection_detected"
  | "recovery_started" | "recovery_completed" | "verification_started" | "verification_completed"
  | "usage_updated" | "status_changed" | "pause_requested" | "pause_completed" | "resume_requested" | "resume_completed"
  | "task_paused" | "task_resumed" | "task_completed" | "task_failed"
  | "task_cancelled" | "task_timed_out" | "limit_reached" | "kill_switch" | "checkpoint_saved"
  | "task_interrupted" | "task_resuming";

export interface Correlation {
  request_id?: string | null;
  session_id?: string | null;
  task_id?: string | null;
  plan_id?: string | null;
  step_id?: string | null;
  tool_call_id?: string | null;
}

export interface ExecutionEvent {
  id: string;
  task_id: string;
  seq: number;
  type: EventType;
  timestamp: string;
  correlation: Correlation;
  message: string;
  data: Record<string, unknown>;
}

export interface LogEntry {
  id: string;
  task_id: string | null;
  level: string;
  component: string;
  message: string;
  data: Record<string, unknown>;
  created_at: string;
}

export interface ToolCallRecord {
  call: { id: string; tool: string; args: Record<string, unknown>; step_id: string | null; rationale: string };
  result: { ok: boolean; summary: string; duration_ms: number; error: ErrorInfo | null; untrusted: boolean; cached: boolean } | null;
  assessment: { risk: RiskLevel; action: string; resource: string; category: string } | null;
  decision: string;
  decision_reason: string;
}

export type Action = "allow" | "ask" | "deny";

export interface TrustedOperation {
  tool: string;
  resource_glob: string;
  max_risk: RiskLevel;
  note: string;
}

export interface PermissionPolicy {
  risk_actions: Record<string, Action>;
  tool_overrides: Record<string, Action>;
  category_overrides: Record<string, Action>;
  trusted_operations: TrustedOperation[];
  disabled_capabilities: string[];
  version: number;
}

export interface ToolSpec {
  name: string;
  description: string;
  capability: string;
  risk: RiskLevel;
  approval: string;
  timeout_s: number;
  idempotent: boolean;
  read_only: boolean;
}

export interface AgentStatus {
  kill_switch_engaged: boolean;
  kill_switch_reason: string;
  active_tasks: string[];
  pending_approvals: number;
}

export interface TaskList {
  items: TaskSummary[];
  limit: number;
  offset: number;
}

export interface ApiErrorBody {
  error: { code: string; message: string; request_id?: string | null; details?: Record<string, unknown> };
}
