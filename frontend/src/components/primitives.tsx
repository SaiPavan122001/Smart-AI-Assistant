import type { ReactNode } from "react";
import { RISK_LABEL, type RiskLevel, type TaskStatus } from "../types/api";

export function StatusBadge({ status }: { status: TaskStatus }) {
  const tone =
    status === "COMPLETED" ? "ok" : status === "FAILED" || status === "TIMED_OUT" ? "err" : status === "WAITING_APPROVAL" || status === "RECOVERING" || status === "CANCELLED" ? "warn" : "";
  return <span className={`badge ${tone}`}>{status.replace("_", " ")}</span>;
}

export function RiskBadge({ risk }: { risk: RiskLevel }) {
  return <span className={`badge risk-${risk}`}>{RISK_LABEL[risk]} risk</span>;
}

export function VerifiedBadge({ verified }: { verified: boolean | null | undefined }) {
  if (verified === null || verified === undefined) return null;
  return verified ? <span className="badge ok">verified</span> : <span className="badge warn">not verified</span>;
}

export function ErrorBanner({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <div className="banner err" role="alert">
      {error}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export const money = (v: number | undefined): string => (v === undefined ? "—" : v < 0.01 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`);
export const when = (iso: string | null | undefined): string => (iso ? new Date(iso).toLocaleString() : "—");
export const duration = (a: string | null | undefined, b: string | null | undefined): string => {
  if (!a) return "—";
  const ms = (b ? new Date(b).getTime() : Date.now()) - new Date(a).getTime();
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
};
