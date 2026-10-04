import { useEffect, useRef, useState } from "react";
import { RiskBadge } from "../../components/primitives";
import type { ApprovalRequest, ApprovalScope } from "../../types/api";

interface Props {
  request: ApprovalRequest;
  queued?: number;
  onApprove: (id: string, scope: ApprovalScope) => Promise<void> | void;
  onDeny: (id: string) => Promise<void> | void;
}

/**
 * Shows the *specific* operation: what, why, which resource, what risk. Never a vague "Allow action?".
 * Focus starts on Deny (the safe choice); Escape denies.
 */
export function ApprovalDialog({ request: r, queued = 0, onApprove, onDeny }: Props) {
  const denyRef = useRef<HTMLButtonElement>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    denyRef.current?.focus();
  }, [r.id]);

  const wrap = (fn: () => Promise<void> | void) => async () => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const canTask = r.allowed_scopes.includes("task");

  return (
    <div className="overlay" role="presentation">
      <div
        className="modal"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="apr-title"
        aria-describedby="apr-body"
        onKeyDown={(e) => {
          if (e.key === "Escape" && !busy) void wrap(() => onDeny(r.id))();
        }}
      >
        <h2 id="apr-title">
          Approval required <RiskBadge risk={r.risk} />
        </h2>
        {queued > 0 && <p className="muted">{queued} more approval{queued === 1 ? "" : "s"} waiting after this one.</p>}
        {r.tainted && (
          <div className="banner warn" role="alert">
            <strong>Careful:</strong> during this task the agent read content that contained instructions aimed at it
            (possible prompt injection). Approve only if <em>you</em> asked for this action.
          </div>
        )}
        <dl id="apr-body">
          <dt>The agent wants to</dt>
          <dd><strong>{r.action}</strong></dd>
          <dt>Why</dt>
          <dd>{r.reason}</dd>
          <dt>Affected resource</dt>
          <dd className="mono">{r.resource}</dd>
          <dt>What could go wrong</dt>
          <dd>
            <ul>{r.risk_explanation.map((x, i) => <li key={i}>{x}</li>)}</ul>
          </dd>
        </dl>
        <details>
          <summary>Technical details</summary>
          <pre>{`tool: ${r.tool}\ncategory: ${r.category}\narguments: ${JSON.stringify(r.arguments, null, 2)}`}</pre>
        </details>
        {err && <div className="banner err" role="alert">{err}</div>}
        <footer>
          <button ref={denyRef} className="danger" disabled={busy} onClick={wrap(() => onDeny(r.id))}>
            Deny
          </button>
          {canTask && (
            <button disabled={busy} onClick={wrap(() => onApprove(r.id, "task"))} title="Also covers similar actions for the rest of this task">
              Approve for this task
            </button>
          )}
          <button className="primary" disabled={busy} onClick={wrap(() => onApprove(r.id, "once"))}>
            Approve once
          </button>
        </footer>
      </div>
    </div>
  );
}
