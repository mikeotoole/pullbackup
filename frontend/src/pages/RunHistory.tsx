import { useParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { StatusPill } from "../components/StatusPill";

function fmtTime(iso: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "—" : d.toLocaleString();
}
function fmtDur(a: string | null, b: string | null) {
  if (!a || !b) return "—";
  const ms = new Date(b).getTime() - new Date(a).getTime();
  if (!(ms >= 0)) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}
function fmtBytes(n: number | null) {
  if (n == null) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}

export function RunHistory() {
  const { id } = useParams();
  const taskId = Number(id);
  const { data: runs } = useQuery({
    queryKey: ["runs", taskId],
    queryFn: () => api.listRuns(taskId),
    // keep refreshing while something is in flight
    refetchInterval: (q) => (q.state.data?.some((r) => r.state === "running" || r.state === "pending") ? 2000 : false),
  });
  const { data: tasks } = useQuery({ queryKey: ["tasks"], queryFn: api.listTasks });
  const task = tasks?.find((t) => t.id === taskId);
  const sorted = [...(runs ?? [])].sort((a, b) => b.id - a.id);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <Link to="/tasks" className="text-muted hover:text-white">← Tasks</Link>
        <h1 className="text-xl font-semibold">Run history{task ? ` — ${task.name}` : ""}</h1>
      </div>
      {!sorted.length && <div className="text-muted text-sm">No runs yet.</div>}
      {sorted.length > 0 && (
        <table className="w-full text-sm">
          <thead className="text-muted text-left">
            <tr>
              <th className="px-3 py-2 font-medium">#</th>
              <th className="px-3 py-2 font-medium">State</th>
              <th className="px-3 py-2 font-medium">Started</th>
              <th className="px-3 py-2 font-medium">Duration</th>
              <th className="px-3 py-2 font-medium">Transferred</th>
              <th className="px-3 py-2 font-medium" />
            </tr>
          </thead>
          <tbody>
            {sorted.map((r) => (
              <tr key={r.id} className="border-t border-border hover:bg-panel align-top">
                <td className="px-3 py-2 font-mono text-xs">{r.id}</td>
                <td className="px-3 py-2"><StatusPill state={r.state} /></td>
                <td className="px-3 py-2 text-muted text-xs">{fmtTime(r.started_at)}</td>
                <td className="px-3 py-2 text-muted text-xs">{fmtDur(r.started_at, r.finished_at)}</td>
                <td className="px-3 py-2 text-muted text-xs">
                  {fmtBytes(r.bytes_transferred)}{r.files_transferred != null ? ` · ${r.files_transferred} files` : ""}
                  {r.state === "failed" && r.error_message ? (
                    <div className="text-danger text-xs mt-0.5 max-w-md truncate" title={r.error_message}>{r.error_message}</div>
                  ) : null}
                </td>
                <td className="px-3 py-2 text-right whitespace-nowrap">
                  <Link to={`/runs/${r.id}`} className="text-muted hover:text-white" title="View log">log ↗</Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
