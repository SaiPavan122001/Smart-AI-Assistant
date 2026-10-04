import { Empty } from "../../components/primitives";
import type { TimelineIcon, TimelineItem } from "./taskView";

const GLYPH: Record<TimelineIcon, string> = { done: "✓", running: "", warn: "!", error: "✕", info: "·" };
const LABEL: Record<TimelineIcon, string> = { done: "done", running: "in progress", warn: "attention", error: "failed", info: "note" };

function Row({ it }: { it: TimelineItem }) {
  return (
    <li data-testid="timeline-item" data-icon={it.icon}>
      <span className={`ticon ${it.icon}`} role="img" aria-label={LABEL[it.icon]}>
        {GLYPH[it.icon]}
      </span>
      <div>
        <div>{it.title}</div>
        {it.detail && <div className="tdetail">{it.detail}</div>}
      </div>
      {it.children.length > 0 && (
        <ul className="children">
          {it.children.map((c) => (
            <Row key={c.key} it={c} />
          ))}
        </ul>
      )}
    </li>
  );
}

export function ActivityTimeline({ items }: { items: TimelineItem[] }) {
  if (items.length === 0) return <Empty>Nothing yet. Describe what you want done and press Execute.</Empty>;
  return (
    <ul className="timeline" aria-label="Activity timeline" aria-live="polite">
      {items.map((it) => (
        <Row key={it.key} it={it} />
      ))}
    </ul>
  );
}
