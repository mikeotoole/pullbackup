import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type Task } from "../lib/api";
import { StatusPill } from "../components/StatusPill";
import { Toggle } from "../components/Toggle";

// syncoid tasks are surfaced as "zfs" in the UI
const typeLabel = (t: Task) => (t.task_type === "syncoid" ? "zfs" : "rsync");
type TypeFilter = "all" | "rsync" | "zfs";

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
  const { data: sys } = useQuery({ queryKey: ["sysinfo"], queryFn: api.systemInfo });
  const sourceById = Object.fromEntries(sources.map(s => [s.id, s] as const));
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");
  const shown = tasks.filter(t => typeFilter === "all" || typeLabel(t) === typeFilter);

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
    onError: (e) => alert(`Delete failed: ${String((e as Error).message)}`),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <h1 className="text-xl font-semibold">Tasks</h1>
          <div className="flex gap-1 text-xs">
            {(["all", "rsync", "zfs"] as const).map(f => (
              <button
                key={f}
                onClick={() => setTypeFilter(f)}
                className={`px-2 py-1 rounded border capitalize ${typeFilter === f ? "bg-accent text-white border-accent" : "border-border text-muted hover:text-white"}`}
              >
                {f}
              </button>
            ))}
          </div>
        </div>
        <Link to="/tasks/new" className="btn-primary">+ Add Task</Link>
      </div>
      <div className="bg-panel border border-border rounded overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-muted text-left">
            <tr className="border-b border-border">
              <th className="px-4 py-3 font-medium">Remote Path</th>
              <th className="px-4 py-3 font-medium">Type</th>
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
            {shown.length === 0 && (
              <tr><td colSpan={9} className="px-4 py-8 text-center text-muted">No tasks{typeFilter !== "all" ? ` of type "${typeFilter}"` : " yet"}. Add one to get started.</td></tr>
            )}
            {shown.map(t => {
              const pill = <StatusPill state={t.last_run_state} />;
              return (
              <tr key={t.id} className="border-b border-border last:border-0">
                <td className="px-4 py-3 font-mono text-xs" title={`local: ${t.local_path}`}>{t.remote_path}</td>
                <td className="px-4 py-3 text-xs"><span className="px-1.5 py-0.5 rounded bg-border/60 font-mono">{typeLabel(t)}</span></td>
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
                  <Link to={`/tasks/${t.id}/runs`} title="Run history" className="inline-block w-7 text-center text-muted hover:text-white">🕘</Link>
                  <Link to={`/tasks/${t.id}/edit`} title="Edit" className="inline-block w-7 text-center text-muted hover:text-white">✎</Link>
                  <Link to="/tasks/new" state={{ clone: t }} title="Clone (opens an unsaved copy)" className="inline-block w-7 text-center text-muted hover:text-white">⧉</Link>
                  {t.kuma_monitor_id && sys?.kuma_url && (
                    <a href={`${sys.kuma_url}/dashboard/${t.kuma_monitor_id}`} target="_blank" rel="noreferrer" title="Uptime Kuma monitor" className="inline-block w-7 text-center text-muted hover:text-white">🔔</a>
                  )}
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
