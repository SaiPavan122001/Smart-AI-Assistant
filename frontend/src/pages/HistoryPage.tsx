import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Empty, ErrorBanner, StatusBadge, VerifiedBadge, duration, money, when } from "../components/primitives";
import { useAsync } from "../hooks/useAsync";
import { api } from "../services/api";
import type { TaskStatus } from "../types/api";

const STATUSES: (TaskStatus | "")[] = ["", "COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT", "EXECUTING", "WAITING_APPROVAL"];
const PAGE = 20;

export function HistoryPage() {
  const nav = useNavigate();
  const [status, setStatus] = useState<TaskStatus | "">("");
  const [offset, setOffset] = useState(0);
  const { data, error, loading } = useAsync(
    () => api.listTasks({ limit: PAGE, offset, status: status || undefined }),
    [status, offset],
    5000,
  );
  const items = data?.items ?? [];

  return (
    <div className="page">
      <h1>Task history</h1>
      <div className="row">
        <div style={{ width: 240 }}>
          <label htmlFor="st">Status</label>
          <select id="st" value={status} onChange={(e) => { setOffset(0); setStatus(e.target.value as TaskStatus | ""); }}>
            {STATUSES.map((s) => <option key={s} value={s}>{s || "All"}</option>)}
          </select>
        </div>
      </div>
      <ErrorBanner error={error} />
      <div className="card flush">
        {items.length === 0 ? (
          <Empty>{loading ? "Loading…" : "No tasks match."}</Empty>
        ) : (
          <table aria-label="Tasks">
            <thead>
              <tr><th>Status</th><th>Request</th><th>Started</th><th>Duration</th><th>LLM</th><th>Cost</th><th>Result</th></tr>
            </thead>
            <tbody>
              {items.map((t) => (
                <tr key={t.id} className="clickable" onClick={() => nav(`/history/${t.id}`)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && nav(`/history/${t.id}`)}>
                  <td><StatusBadge status={t.status} /></td>
                  <td>{t.instruction.length > 100 ? t.instruction.slice(0, 98) + "…" : t.instruction}</td>
                  <td className="muted">{when(t.created_at)}</td>
                  <td>{duration(t.started_at ?? t.created_at, t.finished_at)}</td>
                  <td>{t.usage.llm_calls}</td>
                  <td>{money(t.usage.estimated_cost_usd)}</td>
                  <td><VerifiedBadge verified={t.verified} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <div className="row">
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>← Newer</button>
        <button disabled={items.length < PAGE} onClick={() => setOffset(offset + PAGE)}>Older →</button>
      </div>
    </div>
  );
}
