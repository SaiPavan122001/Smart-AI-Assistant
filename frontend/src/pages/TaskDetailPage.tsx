import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Empty, ErrorBanner, RiskBadge, StatusBadge, VerifiedBadge, duration, money, when } from "../components/primitives";
import { ApprovalDialog } from "../features/approvals/ApprovalDialog";
import { ActivityTimeline } from "../features/tasks/ActivityTimeline";
import { ResultCard } from "../features/tasks/ResultCard";
import { useAsync } from "../hooks/useAsync";
import { useTaskView } from "../hooks/useTaskView";
import { api } from "../services/api";
import { isTerminal } from "../types/api";

const TABS = ["Timeline", "Plan", "Tool calls", "Approvals", "Verification", "Logs"] as const;
type Tab = (typeof TABS)[number];

export function TaskDetailPage() {
  const { id = "" } = useParams();
  const task = useAsync(() => api.getTask(id), [id], 4000);
  const running = task.data ? !isTerminal(task.data.status) : false;
  const { view } = useTaskView(id, running);
  const [tab, setTab] = useState<Tab>("Timeline");
  const t = task.data;

  return (
    <div className="page">
      <p><Link to="/history">← Task history</Link></p>
      <ErrorBanner error={task.error} />
      {t && (
        <>
          <div className="card">
            <div className="row">
              <StatusBadge status={t.status} />
              <VerifiedBadge verified={t.result?.verified} />
              <span className="muted">{when(t.created_at)} · {duration(t.started_at ?? t.created_at, t.finished_at)} · {t.usage.llm_calls} LLM · {t.usage.tool_calls} tools · {t.usage.approvals} approvals · {money(t.usage.estimated_cost_usd)}</span>
            </div>
            <h2 style={{ marginTop: 8 }}>{t.instruction}</h2>
            <code className="muted">{t.id}</code>
          </div>
          {t.result && <ResultCard result={t.result} />}
          <div className="tabs" role="tablist">
            {TABS.map((x) => (
              <button key={x} role="tab" aria-selected={tab === x} onClick={() => setTab(x)}>{x}</button>
            ))}
          </div>
          {tab === "Timeline" && <section className="card"><ActivityTimeline items={view.timeline} /></section>}
          {tab === "Plan" && <PlanTab plan={t.plan} />}
          {tab === "Tool calls" && <ToolCallsTab id={id} />}
          {tab === "Approvals" && <ApprovalsTab id={id} />}
          {tab === "Verification" && <VerificationTab id={id} />}
          {tab === "Logs" && <LogsTab id={id} />}
          {view.pendingApprovals[0] && (
            <ApprovalDialog
              request={view.pendingApprovals[0]}
              queued={view.pendingApprovals.length - 1}
              onApprove={async (aid, scope) => { await api.approve(aid, scope); }}
              onDeny={async (aid) => { await api.deny(aid); }}
            />
          )}
        </>
      )}
    </div>
  );
}

function PlanTab({ plan }: { plan: import("../types/api").Plan | null }) {
  if (!plan) return <Empty>No plan was produced.</Empty>;
  return (
    <section className="card flush">
      <div style={{ padding: 16 }}>
        <strong>{plan.summary}</strong> <span className="muted">(plan v{plan.version})</span>
        {plan.assumptions.length > 0 && <ul>{plan.assumptions.map((a, i) => <li key={i} className="muted">{a}</li>)}</ul>}
      </div>
      <table aria-label="Plan steps">
        <thead><tr><th>#</th><th>Step</th><th>Method</th><th>Status</th><th>Attempts</th><th>Outcome</th></tr></thead>
        <tbody>
          {plan.steps.map((s) => (
            <tr key={s.id}>
              <td>{s.id}</td>
              <td><div>{s.title}</div><div className="muted">{s.rationale}</div></td>
              <td>{s.kind === "tool" ? <code>{s.tool}</code> : <span className="badge warn">adaptive</span>}{s.verify.length > 0 && <div className="muted">{s.verify.length} check(s)</div>}</td>
              <td><span className={`badge ${s.status === "SUCCEEDED" ? "ok" : s.status === "FAILED" ? "err" : ""}`}>{s.status}</span></td>
              <td>{s.attempts}</td>
              <td className="tdetail">{s.error ? `${s.error.code}: ${s.error.message}` : s.summary}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {plan.success_criteria.length > 0 && (
        <div style={{ padding: 16 }}>
          <strong>Success criteria</strong>
          <ul>{plan.success_criteria.map((c, i) => <li key={i}><code>{c.kind}</code> <span className="muted">{JSON.stringify(c.params)}</span></li>)}</ul>
        </div>
      )}
    </section>
  );
}

function ToolCallsTab({ id }: { id: string }) {
  const { data, error } = useAsync(() => api.toolCalls(id), [id]);
  if (error) return <ErrorBanner error={error} />;
  if (!data?.length) return <Empty>No tool calls.</Empty>;
  return (
    <section className="card flush">
      <table aria-label="Tool calls">
        <thead><tr><th>Tool</th><th>Risk</th><th>Decision</th><th>Outcome</th><th>ms</th></tr></thead>
        <tbody>
          {data.map((c) => (
            <tr key={c.call.id}>
              <td><code>{c.call.tool}</code><div className="tdetail">{c.assessment?.action ?? JSON.stringify(c.call.args).slice(0, 120)}</div></td>
              <td>{c.assessment && <RiskBadge risk={c.assessment.risk} />}</td>
              <td>{c.decision}<div className="tdetail">{c.decision_reason}</div></td>
              <td>{c.result ? <span className={`badge ${c.result.ok ? "ok" : "err"}`}>{c.result.ok ? "ok" : c.result.error?.code}</span> : "—"}{c.result?.untrusted && <span className="badge warn" title="Output treated as untrusted data">untrusted</span>}<div className="tdetail">{c.result?.ok ? c.result.summary : c.result?.error?.message}</div></td>
              <td>{c.result ? Math.round(c.result.duration_ms) : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function ApprovalsTab({ id }: { id: string }) {
  const { data, error } = useAsync(() => api.taskApprovals(id), [id], 4000);
  if (error) return <ErrorBanner error={error} />;
  if (!data?.length) return <Empty>No approvals were requested.</Empty>;
  return (
    <section className="card flush">
      <table aria-label="Approvals">
        <thead><tr><th>Request</th><th>Risk</th><th>Decision</th></tr></thead>
        <tbody>
          {data.map((a) => (
            <tr key={a.id}>
              <td><strong>{a.action}</strong><div className="tdetail">{a.reason}</div><div className="tdetail mono">{a.resource}</div></td>
              <td><RiskBadge risk={a.risk} />{a.tainted && <span className="badge err">tainted</span>}</td>
              <td><span className={`badge ${a.status === "APPROVED" ? "ok" : a.status === "PENDING" ? "warn" : "err"}`}>{a.status}</span>{a.scope_granted && <span className="badge">{a.scope_granted}</span>}<div className="tdetail">{a.decided_at ? when(a.decided_at) : ""}{a.decision_note ? ` — ${a.decision_note}` : ""}</div></td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function VerificationTab({ id }: { id: string }) {
  const { data, error } = useAsync(() => api.verifications(id), [id], 4000);
  if (error) return <ErrorBanner error={error} />;
  if (!data?.length) return <Empty>No verification checks were run.</Empty>;
  return (
    <section className="card flush">
      <table aria-label="Verification">
        <thead><tr><th>Check</th><th>Result</th><th>Evidence</th></tr></thead>
        <tbody>
          {data.map((v) => (
            <tr key={v.id}>
              <td><code>{v.kind}</code>{v.step_id && <span className="muted"> · {v.step_id}</span>}<div className="tdetail">{v.method === "llm_judgement" ? "model judgement (lower assurance)" : "deterministic"}</div></td>
              <td><span className={`badge ${v.passed ? "ok" : "err"}`}>{v.passed ? "pass" : "fail"}</span></td>
              <td className="tdetail">{v.message}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function LogsTab({ id }: { id: string }) {
  const [level, setLevel] = useState("INFO");
  const { data, error } = useAsync(() => api.logs(id, level), [id, level], 4000);
  return (
    <section className="card flush">
      <div style={{ padding: 12, width: 200 }}>
        <label htmlFor="lv">Level</label>
        <select id="lv" value={level} onChange={(e) => setLevel(e.target.value)}>
          {["DEBUG", "INFO", "WARNING", "ERROR"].map((l) => <option key={l}>{l}</option>)}
        </select>
      </div>
      <ErrorBanner error={error} />
      <LogTable rows={data ?? []} />
    </section>
  );
}

export function LogTable({ rows }: { rows: import("../types/api").LogEntry[] }) {
  if (rows.length === 0) return <Empty>No log entries.</Empty>;
  return (
    <table aria-label="Execution logs">
      <thead><tr><th>Time</th><th>Level</th><th>Component</th><th>Message</th></tr></thead>
      <tbody>
        {rows.map((l) => (
          <tr key={l.id}>
            <td className="muted mono">{new Date(l.created_at).toLocaleTimeString()}</td>
            <td><span className={`badge ${l.level === "ERROR" || l.level === "CRITICAL" ? "err" : l.level === "WARNING" ? "warn" : ""}`}>{l.level}</span></td>
            <td className="mono">{l.component}</td>
            <td>{l.message}{Object.keys(l.data).length > 0 && <div className="tdetail mono">{JSON.stringify(l.data).slice(0, 240)}</div>}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
