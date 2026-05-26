import type { RunState } from "../lib/api";

const styles: Record<RunState, string> = {
  pending: "border-muted text-muted",
  running: "border-warning text-warning",
  success: "border-success text-success",
  failed: "border-danger text-danger",
  cancelled: "border-muted text-muted",
};

export function StatusPill({ state }: { state: RunState | null | undefined }) {
  if (!state) return <span className="text-muted text-sm">—</span>;
  return (
    <span className={`inline-block border rounded-full px-3 py-0.5 text-xs uppercase tracking-wider ${styles[state]}`}>
      {state}
    </span>
  );
}
