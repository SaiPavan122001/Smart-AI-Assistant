import { useRef, useState, type FormEvent, type KeyboardEvent } from "react";

const EXAMPLES = [
  "Open my Downloads folder, find Java-related PDFs, create a Java folder and organize them",
  "Create a folder called Spring Docs on my desktop and move the downloaded PDF there",
  "Create a Python project called expense_tracker, create the required files, run it and fix any errors",
];

interface Props {
  onSubmit: (instruction: string) => Promise<void>;
  disabled?: boolean;
}

export function TaskInput({ onSubmit, disabled }: Props) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  // A ref (not state): two events in the same tick must not both pass the guard.
  const inFlight = useRef(false);

  const submit = async (e?: FormEvent) => {
    e?.preventDefault();
    const value = text.trim();
    if (!value || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    try {
      await onSubmit(value);
      setText("");
    } catch {
      /* the parent shows the error; keep the text so the user does not lose it */
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void submit();
  };

  return (
    <form className="card task-form" onSubmit={submit} aria-label="New task">
      <label htmlFor="instruction">What should I do on your computer?</label>
      <textarea
        id="instruction"
        value={text}
        maxLength={4000}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKey}
        placeholder="e.g. Open Chrome, search for the latest Spring Boot documentation, download the PDF and put it in a new folder on my desktop"
        disabled={disabled}
      />
      <div className="row" style={{ marginTop: 10 }}>
        <button className="primary" type="submit" disabled={disabled || busy || !text.trim()}>
          {busy ? "Starting…" : "Execute"}
        </button>
        <span className="muted">Ctrl+Enter to run · risky steps always ask you first</span>
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        {EXAMPLES.map((ex) => (
          <button key={ex} type="button" onClick={() => setText(ex)} style={{ fontWeight: 400, fontSize: 12 }}>
            {ex.length > 58 ? ex.slice(0, 56) + "…" : ex}
          </button>
        ))}
      </div>
    </form>
  );
}
