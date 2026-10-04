import { useEffect, useState } from "react";
import { StatusBadge, VerifiedBadge, duration, money } from "../../components/primitives";
import type { StreamState } from "../../services/stream";
import { isTerminal } from "../../types/api";
import { STATUS_LABEL, type TaskView } from "./taskView";

interface Props {
  view: TaskView;
  connection: StreamState;
  onPause: () => void;
  onResume: () => void;
  onCancel: () => void;
  busy?: boolean;
}

export function LiveStatus({ view, connection, onPause, onResume, onCancel, busy }: Props) {
  const [now, setNow] = useState(() => new Date().toISOString());
  const active = !isTerminal(view.status);
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(new Date().toISOString()), 1000);
    return () => clearInterval(t);
  }, [active]);

  const steps = view.plan?.steps ?? [];
  const done = steps.filter((s) => s.status === "SUCCEEDED" || s.status === "SKIPPED").length;
  const startedAt = view.timeline[0]?.ts;

  return (
    <section className="card" aria-label="Live status">
      <div className="statusbar">
        <div className="kv">
          <span className="k">Status</span>
          <span className="v" data-testid="status-label">
            <StatusBadge status={view.status} /> {view.paused && view.status !== "PAUSED" && <span className="badge warn">pausing…</span>}
          </span>
        </div>
        <div className="kv"><span className="k">Phase</span><span className="v">{STATUS_LABEL[view.status]}</span></div>
        <div className="kv"><span className="k">Current step</span><span className="v">{view.currentStepTitle ?? "—"}{steps.length > 0 && ` (${Math.min(done + (active ? 1 : 0), steps.length)}/${steps.length})`}</span></div>
        <div className="kv"><span className="k">Current tool</span><span className="v mono">{view.currentTool ?? "—"}</span></div>
        <div className="kv"><span className="k">Application</span><span className="v">{view.currentApplication ?? "—"}</span></div>
        <div className="kv"><span className="k">Elapsed</span><span className="v">{duration(startedAt, active ? now : view.timeline.at(-1)?.ts)}</span></div>
        {view.usage && (
          <div className="kv"><span className="k">LLM calls · est. cost</span><span className="v">{view.usage.llm_calls} · {money(view.usage.estimated_cost_usd)}</span></div>
        )}
        {view.result && <VerifiedBadge verified={view.result.verified} />}
        <span className="spacer" />
        {active && view.taskId && (
          <div className="row">
            {view.paused || view.status === "PAUSED" ? (
              <button onClick={onResume} disabled={busy}>Resume</button>
            ) : (
              <button onClick={onPause} disabled={busy}>Pause</button>
            )}
            <button className="danger" onClick={onCancel} disabled={busy}>Cancel task</button>
          </div>
        )}
      </div>
      {connection === "reconnecting" && <p className="muted">Connection lost — reconnecting and resuming the timeline…</p>}
    </section>
  );
}
