import { useState } from "react";
import { useAgent } from "../hooks/AgentContext";

/** Always visible. Pulses while tasks run. One click: blocks all tool execution immediately. */
export function EmergencyStop() {
  const { status, stop, release } = useAgent();
  const [busy, setBusy] = useState(false);
  const active = (status?.active_tasks.length ?? 0) > 0;
  const engaged = status?.kill_switch_engaged ?? false;

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  };

  if (engaged) {
    return (
      <div className="row" role="status">
        <span className="badge err" data-testid="kill-engaged">EMERGENCY STOP ENGAGED</span>
        <button onClick={() => void run(release)} disabled={busy}>Release stop</button>
      </div>
    );
  }
  return (
    <button
      className={`stop ${active ? "armed" : ""}`}
      onClick={() => void run(stop)}
      disabled={busy}
      aria-label="STOP AGENT — immediately stop all agent activity"
      data-testid="stop-agent"
    >
      ■ STOP AGENT
    </button>
  );
}
