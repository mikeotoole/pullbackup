import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type Task } from "../lib/api";
import { StatusPill } from "../components/StatusPill";
import { Toggle } from "../components/Toggle";

function relTime(iso: string | null) {
  if (!iso) return "N/A";
  const d = new Date(iso).getTime();
  const now = Date.now();
  const s = Math.round((d - now) / 1000);
  const abs = Math.abs(s);
  const fmt = (n: number, u: string) => `${n} ${u}${n !== 1 ? "s" : ""}`;
  const value = abs < 60 ? fmt(abs, "second") : abs < 3600 ? fmt(Math.round(abs/60), "minute") : abs < 86400 ? fmt(Math.round(abs/3600), "hour") : fmt(Math.round(abs/86400), "day");
  return s < 0 ? `${value} ago` : `in ${value}`;
}

export function TasksList() {
  const qc = useQueryClient();
  const { data: tasks = [] } = useQuery({
    queryKey: ["tasks"],
    queryFn: api.listTasks,
    // Poll fast when anything is running, so success/failure shows up without manual refresh
    refetchInterval: (q) => (q.state.data?.some(t => t.last_run_state === "running") ? 2000 : false),
  });
  const { data: sources = [] } = useQuery({ queryKey: ["sources"], queryFn: api.listSources });
  const sourceById = Object.fromEntries(sources.map(s => [s.id, s] as const));

  const toggle = useMutation({
    mutationFn: (t: Task) => api.updateTask(t.id, { ...t, enabled: !t.enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tasks"] }),
  });
  const runNow = useMutation({
    mutationFn: (id: number) => api.runTask(id),
    onSuccess: () => {
      // Refetch immediately so the row flips to "running"
      qc.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
  const del = useMutation({
    mutationFn: (id: number) => api.deleteTask(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tasks"] }),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Rsync Tasks</h1>
        <Link to="/tasks/new" className="btn-primary">+ Add Rsync Task</Link>
      </div>
      <div className="bg-panel border border-border rounded overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-muted text-left">
            <tr className="border-b border-border">
              <th className="px-4 py-3 font-medium">Remote Path</th>
              <th className="px-4 py-3 font-medium">Source</th>
              <th className="px-4 py-3 font-medium">Frequency</th>
              <th className="px-4 py-3 font-medium">Next Run</th>
              <th className="px-4 py-3 font-medium">Last Run</th>
              <th className="px-4 py-3 font-medium">Enabled</th>
              <th className="px-4 py-3 font-medium">State</th>
              <th className="px-4 py-3 font-medium whitespace-nowrap w-36 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {tasks.length === 0 && (
              <tr><td colSpan={8} className="px-4 py-8 text-center text-muted">No tasks yet. Add one to get started.</td></tr>
            )}
            {tasks.map(t => {
              const pill = <StatusPill state={t.last_run_state} />;
              return (
              <tr key={t.id} className="border-b border-border last:border-0">
                <td className="px-4 py-3 font-mono text-xs" title={`local: ${t.local_path}`}>{t.remote_path}</td>
                <td className="px-4 py-3 text-muted text-xs">{sourceById[t.source_id]?.name ?? "—"}</td>
                <td className="px-4 py-3 font-mono text-xs">{t.cron}</td>
                <td className="px-4 py-3 text-muted text-xs">{t.enabled ? relTime(t.next_run) : "Disabled"}</td>
                <td className="px-4 py-3 text-muted text-xs">{relTime(t.last_run_at)}</td>
                <td className="px-4 py-3"><Toggle checked={t.enabled} onChange={() => toggle.mutate(t)} /></td>
                <td className="px-4 py-3">
                  {t.last_run_id ? (
                    <Link to={`/runs/${t.last_run_id}`} title="View last run">{pill}</Link>
                  ) : pill}
                </td>
                <td className="px-4 py-3 text-right whitespace-nowrap">
                  <Link to={`/tasks/${t.id}/edit`} title="Edit" className="inline-block w-7 text-center text-muted hover:text-white">✎</Link>
                  <Link to="/tasks/new" state={{ clone: t }} title="Clone (opens an unsaved copy)" className="inline-block w-7 text-center text-muted hover:text-white">⧉</Link>
                  <button title="Run now" onClick={() => runNow.mutate(t.id)} className="inline-block w-7 text-center text-muted hover:text-white">▶</button>
                  <button title="Delete" onClick={() => confirm(`Delete task ${t.name}?`) && del.mutate(t.id)} className="inline-block w-7 text-center text-muted hover:text-danger">🗑</button>
                </td>
              </tr>
            )})}
          </tbody>
        </table>
      </div>
    </div>
  );
}
