import { getToken } from "./auth";
import type { ExecutionEvent } from "../types/api";

export type StreamState = "connecting" | "open" | "reconnecting" | "closed";

export interface StreamOptions {
  /** `/ws/tasks/<id>` or `/ws/events` */
  path: string;
  afterSeq?: number;
  onEvent: (e: ExecutionEvent) => void;
  onState?: (s: StreamState) => void;
  /** injectable for tests */
  socketFactory?: (url: string, protocols: string[]) => WebSocket;
  maxBackoffMs?: number;
}

/**
 * WebSocket event stream with automatic reconnect. On reconnect it resumes from the last seen
 * sequence number (`?after_seq=`), so the timeline is rebuilt without gaps or duplicates.
 * The token travels in the `bearer.<token>` subprotocol, never in the URL.
 */
export class EventStream {
  private ws: WebSocket | null = null;
  private lastSeq: number;
  private attempts = 0;
  private closedByUser = false;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly opts: StreamOptions) {
    this.lastSeq = opts.afterSeq ?? 0;
  }

  start(): void {
    this.closedByUser = false;
    this.connect();
  }

  close(): void {
    this.closedByUser = true;
    if (this.timer) clearTimeout(this.timer);
    this.ws?.close();
    this.opts.onState?.("closed");
  }

  get seq(): number {
    return this.lastSeq;
  }

  private url(): string {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const sep = this.opts.path.includes("?") ? "&" : "?";
    const resume = this.opts.path.startsWith("/ws/tasks/") ? `${sep}after_seq=${this.lastSeq}` : "";
    return `${proto}//${location.host}/api/v1${this.opts.path}${resume}`;
  }

  private connect(): void {
    this.opts.onState?.(this.attempts === 0 ? "connecting" : "reconnecting");
    const factory = this.opts.socketFactory ?? ((u, p) => new WebSocket(u, p));
    const ws = factory(this.url(), ["saa.v1", `bearer.${getToken()}`]);
    this.ws = ws;
    ws.onopen = () => {
      this.attempts = 0;
      this.opts.onState?.("open");
    };
    ws.onmessage = (m: MessageEvent) => {
      if (typeof m.data !== "string" || m.data === "pong") return;
      let ev: ExecutionEvent;
      try {
        ev = JSON.parse(m.data) as ExecutionEvent;
      } catch {
        return;
      }
      if (this.opts.path.startsWith("/ws/tasks/")) {
        if (ev.seq <= this.lastSeq) return; // duplicate after a resume
        this.lastSeq = ev.seq;
      }
      this.opts.onEvent(ev);
    };
    ws.onclose = (c: CloseEvent) => {
      if (this.closedByUser) return;
      if (c.code === 1008) {
        this.opts.onState?.("closed"); // policy violation: bad token/origin; do not hammer the server
        return;
      }
      this.scheduleReconnect();
    };
    ws.onerror = () => ws.close();
  }

  private scheduleReconnect(): void {
    this.attempts += 1;
    const delay = Math.min(this.opts.maxBackoffMs ?? 8000, 400 * 2 ** Math.min(this.attempts, 6));
    this.opts.onState?.("reconnecting");
    this.timer = setTimeout(() => this.connect(), delay);
  }
}
