import { clearToken, getSessionId, getToken } from "./auth";
import type {
  AgentStatus,
  ApprovalRequest,
  ApprovalScope,
  ApiErrorBody,
  ExecutionEvent,
  LogEntry,
  PermissionPolicy,
  Task,
  TaskList,
  TaskStatus,
  TaskSummary,
  ToolCallRecord,
  ToolSpec,
  VerificationResult,
} from "../types/api";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly requestId?: string | null,
  ) {
    super(message);
    this.name = "ApiError";
  }
  get unauthorized(): boolean {
    return this.status === 401;
  }
}

type Listener = () => void;
const unauthorizedListeners = new Set<Listener>();
/** Subscribe to "token rejected" so the UI can return to the login screen. */
export function onUnauthorized(fn: Listener): () => void {
  unauthorizedListeners.add(fn);
  return () => unauthorizedListeners.delete(fn);
}

const BASE = "/api/v1";

async function request<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${getToken()}`,
    "X-Session-Id": getSessionId(),
  };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    throw new ApiError(0, "network_error", "Cannot reach the agent backend. Is it running?");
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let data: unknown = undefined;
  try {
    data = text ? JSON.parse(text) : undefined;
  } catch {
    /* non-JSON error body */
  }
  if (!res.ok) {
    const err = (data as ApiErrorBody | undefined)?.error;
    if (res.status === 401) {
      clearToken();
      unauthorizedListeners.forEach((l) => l());
    }
    throw new ApiError(res.status, err?.code ?? "http_error", err?.message ?? `Request failed (${res.status})`, err?.request_id);
  }
  return data as T;
}

const q = (params: Record<string, string | number | boolean | undefined>): string => {
  const s = Object.entries(params)
    .filter(([, v]) => v !== undefined)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
    .join("&");
  return s ? `?${s}` : "";
};

export const api = {
  health: () => request<{ status: string; version: string }>("GET", "/health"),
  ready: () => request<{ ready: boolean; checks: Record<string, unknown> }>("GET", "/ready"),
  createTask: (instruction: string) => request<TaskSummary>("POST", "/tasks", { instruction }),
  listTasks: (p: { limit?: number; offset?: number; status?: TaskStatus } = {}) =>
    request<TaskList>("GET", `/tasks${q(p)}`),
  getTask: (id: string) => request<Task>("GET", `/tasks/${id}`),
  cancelTask: (id: string) => request<TaskSummary>("POST", `/tasks/${id}/cancel`),
  pauseTask: (id: string) => request<TaskSummary>("POST", `/tasks/${id}/pause`),
  resumeTask: (id: string) => request<TaskSummary>("POST", `/tasks/${id}/resume`),
  events: (id: string, afterSeq = 0) => request<ExecutionEvent[]>("GET", `/tasks/${id}/events${q({ after_seq: afterSeq, limit: 5000 })}`),
  logs: (id: string, level = "INFO") => request<LogEntry[]>("GET", `/tasks/${id}/logs${q({ level })}`),
  toolCalls: (id: string) => request<ToolCallRecord[]>("GET", `/tasks/${id}/tool-calls`),
  taskApprovals: (id: string) => request<ApprovalRequest[]>("GET", `/tasks/${id}/approvals`),
  verifications: (id: string) => request<VerificationResult[]>("GET", `/tasks/${id}/verifications`),
  pendingApprovals: () => request<ApprovalRequest[]>("GET", "/approvals?pending=true"),
  approve: (id: string, scope: ApprovalScope = "once", note?: string) =>
    request<ApprovalRequest>("POST", `/approvals/${id}/approve`, { scope, note }),
  deny: (id: string, note?: string) => request<ApprovalRequest>("POST", `/approvals/${id}/deny`, { note }),
  getPermissions: () => request<PermissionPolicy>("GET", "/permissions"),
  updatePermissions: (p: PermissionPolicy) => request<PermissionPolicy>("PUT", "/permissions", p),
  tools: () => request<ToolSpec[]>("GET", "/tools"),
  settings: () => request<Record<string, Record<string, unknown> | string>>("GET", "/settings"),
  metrics: () => request<Record<string, number>>("GET", "/metrics/summary"),
  agentStatus: () => request<AgentStatus>("GET", "/agent/status"),
  stopAgent: () => request<{ stopped_tasks: string[]; kill_switch_engaged: boolean }>("POST", "/agent/stop"),
  releaseAgent: () => request<{ kill_switch_engaged: boolean }>("POST", "/agent/release"),
};
