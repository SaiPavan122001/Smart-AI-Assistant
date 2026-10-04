import { VerifiedBadge, money } from "../../components/primitives";
import type { TaskResult } from "../../types/api";

const TONE = { COMPLETED: "ok", FAILED: "err", TIMED_OUT: "err", CANCELLED: "warn" } as const;

export function ResultCard({ result }: { result: TaskResult }) {
  const tone = TONE[result.status as keyof typeof TONE] ?? "";
  return (
    <section className="card" aria-label="Final result">
      <div className="row">
        <h2 style={{ margin: 0 }}>Result</h2>
        <span className={`badge ${tone}`}>{result.status}</span>
        <VerifiedBadge verified={result.verified} />
        {!result.verified && result.status === "COMPLETED" && (
          <span className="muted">Judged by the model only (no machine-checkable criteria) — lower assurance.</span>
        )}
      </div>
      <pre style={{ marginTop: 10 }}>{result.summary}</pre>
      {result.answer && (
        <div className="banner ok" style={{ marginTop: 10 }}>
          <strong>Answer:</strong> {result.answer}
        </div>
      )}
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
