import { useState } from "react";
import type { ActionCertainty, Interruption } from "../../types/api";

const CERTAINTY: Record<ActionCertainty, { label: string; tone: string }> = {
  safe_to_retry: { label: "safe to repeat", tone: "" },
  confirmed_done: { label: "completed — will not be repeated", tone: "ok" },
  confirmed_not_done: { label: "did not happen — will run again", tone: "" },
  uncertain: { label: "outcome unknown — needs your approval before it is repeated", tone: "warn" },
};

interface Props {
  interruption: Interruption;
  onResume: () => Promise<unknown>;
  onRestart: () => Promise<unknown>;
  onCancel: () => Promise<unknown>;
}

/** An interrupted task never continues by itself: this is where the user decides what happens to it. */
export function RecoveryPanel({ interruption, onResume, onRestart, onCancel }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = (fn: () => Promise<unknown>) => async () => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const done = interruption.completed_steps.length;
  const left = interruption.remaining_steps.length;

  return (
    <section className="card" aria-label="Interrupted task" data-testid="recovery-panel">
      <h2>This task was interrupted</h2>
      <p>
        The service stopped before the task finished. Nothing runs until you choose what to do.
        {" "}
        <strong>{done}</strong> step{done === 1 ? "" : "s"} completed, <strong>{left}</strong> remaining.
      </p>
      {interruption.needs_attention && (
        <p className="banner warn" role="alert">
          An action was in progress and it could not be determined whether it took effect. Check the
          current state before you resume; you will be asked before that action is repeated.
        </p>
      )}
      {interruption.in_flight.length > 0 && (
        <>
          <div className="muted">In progress when the service stopped</div>
          <ul>
            {interruption.in_flight.map((a) => (
              <li key={a.tool_call_id}>
                <span className="mono">{a.tool}</span>{" "}
                <span className={`badge ${CERTAINTY[a.certainty].tone}`}>{CERTAINTY[a.certainty].label}</span>
                {a.detail && <span className="muted"> — {a.detail}</span>}
              </li>
            ))}
          </ul>
        </>
      )}
      <ul className="muted">
        {interruption.notes.map((n) => (
          <li key={n}>{n}</li>
        ))}
      </ul>
      {error && <p className="banner err" role="alert">{error}</p>}
      <div className="row">
        {interruption.resumable && (
          <button className="primary" onClick={run(onResume)} disabled={busy} title="Continue from the recorded state. Completed steps are not run again.">
            Resume safely
          </button>
        )}
        <button onClick={run(onRestart)} disabled={busy} title="End this task and run the same request again from the start, as a new task. What was already done stays in place.">
          Restart as a new task
        </button>
        <button className="danger" onClick={run(onCancel)} disabled={busy} title="End this task. Completed actions are not undone.">
          Cancel task
        </button>
      </div>
      {!interruption.resumable && (
        <p className="muted">This task cannot be resumed; it can be restarted or cancelled.</p>
      )}
    </section>
  );
}
