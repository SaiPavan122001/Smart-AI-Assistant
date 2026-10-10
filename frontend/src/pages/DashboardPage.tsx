import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ErrorBanner, StatusBadge, when } from "../components/primitives";
import { ApprovalDialog } from "../features/approvals/ApprovalDialog";
import { ActivityTimeline } from "../features/tasks/ActivityTimeline";
import { LiveStatus } from "../features/tasks/LiveStatus";
import { RecoveryPanel } from "../features/tasks/RecoveryPanel";
import { ResultCard } from "../features/tasks/ResultCard";
import { TaskInput } from "../features/tasks/TaskInput";
import { useAgent } from "../hooks/AgentContext";
import { useAsync } from "../hooks/useAsync";
import { useTaskView } from "../hooks/useTaskView";
import { api } from "../services/api";
import type { ApprovalScope } from "../types/api";

export function DashboardPage() {
  const [params, setParams] = useSearchParams();
  const taskId = params.get("task");
  const { view, connection, error: streamError } = useTaskView(taskId, true);
  const { refresh } = useAgent();
  const recent = useAsync(() => api.listTasks({ limit: 5 }), [taskId, view.status], 5000);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const guard = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const start = async (instruction: string) => {
    setError(null);
    try {
      const t = await api.createTask(instruction);
      setParams({ task: t.id });
      await refresh();
    } catch (e) {
      setError((e as Error).message);
      throw e;
    }
  };

  const pending = view.pendingApprovals;
  const first = pending[0];

  return (
    <div className="page">
      <h1>Dashboard</h1>
      <TaskInput onSubmit={start} />
      <ErrorBanner error={error ?? streamError} />

      {taskId && (
        <>
          <div className="card">
            <div className="muted">Request</div>
            <div style={{ fontWeight: 600 }}>{view.instruction || "…"}</div>
          </div>
          <LiveStatus
            view={view}
            connection={connection}
            busy={busy}
            onPause={() => void guard(() => api.pauseTask(taskId))}
            onResume={() => void guard(() => api.resumeTask(taskId))}
            onCancel={() => void guard(() => api.cancelTask(taskId))}
          />
          {view.status === "INTERRUPTED" && view.interruption && (
            <RecoveryPanel
              interruption={view.interruption}
              onResume={async () => {
                await api.recoverTask(taskId, "resume");
                await refresh();
              }}
              onRestart={async () => {
                const t = await api.recoverTask(taskId, "restart");
                setParams({ task: t.id });
                await refresh();
              }}
              onCancel={async () => {
                await api.recoverTask(taskId, "cancel");
                await refresh();
              }}
            />
          )}
          <section className="card" aria-label="Activity">
            <h2>Activity</h2>
            <ActivityTimeline items={view.timeline} />
          </section>
          {view.result && <ResultCard result={view.result} />}
          <p>
            <Link to={`/history/${taskId}`}>Open full task details →</Link>
          </p>
        </>
      )}

      {first && (
        <ApprovalDialog
          request={first}
          queued={pending.length - 1}
          onApprove={async (id, scope: ApprovalScope) => {
            await api.approve(id, scope);
          }}
          onDeny={async (id) => {
            await api.deny(id);
          }}
        />
      )}

      <section className="card flush" aria-label="Recent tasks">
        <div style={{ padding: "12px 16px" }}><h2 style={{ margin: 0 }}>Recent tasks</h2></div>
        <table>
          <tbody>
            {(recent.data?.items ?? []).map((t) => (
              <tr key={t.id} className="clickable" onClick={() => setParams({ task: t.id })}>
                <td><StatusBadge status={t.status} /></td>
                <td>{t.instruction.length > 90 ? t.instruction.slice(0, 88) + "…" : t.instruction}</td>
                <td className="muted">{when(t.created_at)}</td>
              </tr>
            ))}
            {recent.data?.items.length === 0 && (
              <tr><td className="muted">No tasks yet.</td></tr>
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}
