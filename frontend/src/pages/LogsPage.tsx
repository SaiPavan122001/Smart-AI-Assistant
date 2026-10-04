import { useState } from "react";
import { Empty, ErrorBanner } from "../components/primitives";
import { useAsync } from "../hooks/useAsync";
import { api } from "../services/api";
import { LogTable } from "./TaskDetailPage";

export function LogsPage() {
  const tasks = useAsync(() => api.listTasks({ limit: 50 }), []);
  const [taskId, setTaskId] = useState("");
  const [level, setLevel] = useState("INFO");
  const [query, setQuery] = useState("");
  const logs = useAsync(() => (taskId ? api.logs(taskId, level) : Promise.resolve([])), [taskId, level], taskId ? 3000 : undefined);
  const rows = (logs.data ?? []).filter((l) => !query || `${l.message} ${l.component}`.toLowerCase().includes(query.toLowerCase()));

  return (
    <div className="page">
      <h1>Execution logs</h1>
      <p className="muted">Structured, secret-redacted logs correlated by task, step and tool call.</p>
      <div className="grid2">
        <div>
          <label htmlFor="task">Task</label>
          <select id="task" value={taskId} onChange={(e) => setTaskId(e.target.value)}>
            <option value="">Select a task…</option>
            {(tasks.data?.items ?? []).map((t) => (
              <option key={t.id} value={t.id}>{t.status} · {t.instruction.slice(0, 60)}</option>
            ))}
          </select>
        </div>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div style={{ width: 130 }}>
            <label htmlFor="lvl">Level</label>
            <select id="lvl" value={level} onChange={(e) => setLevel(e.target.value)}>
              {["DEBUG", "INFO", "WARNING", "ERROR"].map((l) => <option key={l}>{l}</option>)}
            </select>
          </div>
          <div style={{ flex: 1 }}>
            <label htmlFor="q">Search</label>
            <input id="q" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="filter messages" />
          </div>
        </div>
      </div>
      <ErrorBanner error={logs.error ?? tasks.error} />
      <div className="card flush">{taskId ? <LogTable rows={rows} /> : <Empty>Choose a task to see its logs.</Empty>}</div>
    </div>
  );
}
