import { VerifiedBadge, money } from "../../components/primitives";
import type { CompletionReport, TaskResult } from "../../types/api";

const TONE = { COMPLETED: "ok", FAILED: "err", TIMED_OUT: "err", CANCELLED: "warn", PARTIAL: "warn" } as const;
const MARK = { met: "✓", unmet: "✗", unverified: "?" } as const;

/** How to present `answer`. Older records have no answer_status: only a COMPLETED task may show one. */
function answerKind(result: TaskResult): "answer" | "unavailable" | "withheld" | "none" {
  if (result.answer_status === "ANSWERED") return result.status === "COMPLETED" ? "answer" : "none";
  if (result.answer_status === "answer_unavailable") return "unavailable";
  if (result.answer_status === "WITHHELD") return "withheld";
  if (result.answer_status === undefined) return result.status === "COMPLETED" ? "answer" : "none";
  return "none";
}

/** Steps done / failed / not run and each stated requirement, from the final state (never from the model). */
function Progress({ report }: { report: CompletionReport }) {
  const rows = [
    ["Done", report.completed],
    ["Failed", report.failed],
    ["Interrupted (may have partly completed)", report.interrupted ?? []],
    ["Not run", report.not_run],
  ] as const;
  return (
    <div style={{ marginTop: 10 }} data-testid="completion-report">
      {report.requirements.length > 0 && (
        <ul aria-label="Requirements">
          {report.requirements.map((r) => (
            <li key={r.id} data-status={r.status}>
              <span className={`badge ${r.status === "met" ? "ok" : r.status === "unmet" ? "err" : "warn"}`}>{MARK[r.status]} {r.status}</span> {r.text}
              {r.evidence && <span className="muted"> — {r.evidence}</span>}
            </li>
          ))}
        </ul>
      )}
      {rows.map(([label, steps]) =>
        steps.length > 0 ? (
          <p key={label} className="muted" style={{ margin: "4px 0" }}>
            <strong>{label}:</strong> {steps.map((s) => s.title).join("; ")}
          </p>
        ) : null,
      )}
      {report.effects.length > 0 && <p className="muted" style={{ margin: "4px 0" }}>Completed actions were not undone.</p>}
    </div>
  );
}

export function ResultCard({ result }: { result: TaskResult }) {
  const partial = result.outcome === "PARTIAL";
  const label = partial ? "PARTIAL" : result.status;
  const tone = TONE[label as keyof typeof TONE] ?? "";
  return (
    <section className="card" aria-label="Final result">
      <div className="row">
        <h2 style={{ margin: 0 }}>Result</h2>
        <span className={`badge ${tone}`} data-testid="result-status">{label}</span>
        <VerifiedBadge verified={result.verified} />
        {!result.verified && result.status === "COMPLETED" && result.verification_status !== "INCOMPLETE" && (
          <span className="muted">Judged by the model only (no machine-checkable criteria) — lower assurance.</span>
        )}
        {result.verification_status === "INCOMPLETE" && (
          <span className="muted">Completed, but the requested response data was unavailable.</span>
        )}
      </div>
      <pre style={{ marginTop: 10 }}>{result.summary}</pre>
      {/* Only an ANSWERED result is a success answer. Text on a failed task states what did NOT happen. */}
      {result.answer && answerKind(result) === "answer" && (
        <div className="banner ok" style={{ marginTop: 10 }}>
          <strong>Answer:</strong> {result.answer}
        </div>
      )}
      {result.answer && answerKind(result) === "unavailable" && (
        <div className="banner warn" style={{ marginTop: 10 }} data-testid="answer-unavailable">
          <strong>No answer:</strong> {result.answer}
        </div>
      )}
      {result.answer && answerKind(result) === "withheld" && (
        <div className="banner err" style={{ marginTop: 10 }} data-testid="outcome-statement">
          <strong>Not completed:</strong> {result.answer}
        </div>
      )}
      {result.report && (result.status !== "COMPLETED" || result.report.requirements.length > 0) && <Progress report={result.report} />}
      {result.error && <p className="muted">Error code: <code>{result.error.code}</code></p>}
      {result.verification.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary>{result.verification.filter((v) => v.passed).length}/{result.verification.length} verification checks passed</summary>
          <ul>
            {result.verification.map((v) => (
              <li key={v.id}>
                <span className={`badge ${v.passed ? "ok" : "err"}`}>{v.passed ? "pass" : "fail"}</span> <code>{v.kind}</code> — {v.message}
              </li>
            ))}
          </ul>
        </details>
      )}
      <p className="muted" style={{ marginBottom: 0 }}>
        {result.duration_s}s · {result.usage.llm_calls} LLM calls · {result.usage.tool_calls} tool calls · {result.usage.vision_calls} vision calls · {money(result.usage.estimated_cost_usd)} est.
      </p>
    </section>
  );
}
