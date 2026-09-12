// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole
import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type Task } from "../lib/api";
import { lastRunTime, relTime } from "../lib/relativeTime";
import { nextSort, sortTasks, type SortColumn, type SortPreference } from "../lib/taskSort";
import { loadSort, saveSort } from "../lib/taskSortStorage";
import { StatusPill } from "../components/StatusPill";
import { Toggle } from "../components/Toggle";
import { BellIcon, CloneIcon, HistoryIcon, PencilIcon, PlayIcon, StopIcon, TrashIcon } from "../components/icons";

// syncoid tasks are surfaced as "zfs" in the UI
const typeLabel = (t: Task) => (t.task_type === "syncoid" ? "zfs" : "rsync");
type TypeFilter = "all" | "rsync" | "zfs";

/**
 * Whether this row's work can be stopped right now.
 *
 * A run is stoppable only while it is RUNNING — `pending` has no subprocess to
 * signal, and the server would refuse it — so the control is not rendered at
 * all rather than rendered and failing on click. `last_run_id` is required too:
 * without it there is no run to name in the request.
 */
const isStoppable = (t: Task) => t.last_run_state === "running" && t.last_run_id != null;

const ariaDirection = (pref: SortPreference | null, column: SortColumn) =>
  pref?.column === column ? (pref.direction === "asc" ? "ascending" : "descending") : "none";

/** A native button gives the column header Enter/Space behavior for free. */
function SortHeader({
  column,
  label,
  pref,
  onSort,
}: {
  column: SortColumn;
  label: ReactNode;
  pref: SortPreference | null;
  onSort: (column: SortColumn) => void;
}) {
  const active = pref?.column === column;
  return (
    <th className="px-4 py-3 font-medium" aria-sort={ariaDirection(pref, column)}>
      <button
        type="button"
        onClick={() => onSort(column)}
        className="inline-flex items-center gap-1 hover:text-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent rounded"
      >
        <span>{label}</span>
        {active && (
          <span
            data-sort-indicator={pref.direction}
            className="text-[10px] font-mono text-accent"
            aria-hidden="true"
          >
            {pref.direction}
          </span>
        )}
      </button>
    </th>
  );
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
  // Lazy initialization reads this browser profile exactly once per mount. A
  // blocked or malformed localStorage simply yields the server's default order.
  const [sort, setSort] = useState<SortPreference | null>(() => loadSort());
  const filtered = tasks.filter(t => typeFilter === "all" || typeLabel(t) === typeFilter);
  // Filtering first matters: the chosen order applies to the visible set, and
  // both desktop rows and phone cards consume this one projection.
  const shown = sortTasks(filtered, sort, sources);

  const chooseSort = (column: SortColumn) => {
    const selected = nextSort(sort, column);
    setSort(selected);
    saveSort(selected);
  };

  const toggle = useMutation({
    mutationFn: (t: Task) => api.updateTask(t.id, { ...t, enabled: !t.enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tasks"] }),
    // Toggling is now permitted while a run is active, but the server still
    // refuses a payload that changes a command-shaping field. Surfacing its 409
    // text beats a silent revert that leaves the switch disagreeing with the
    // stored row.
    onError: (e) => alert(`Could not change enabled: ${String((e as Error).message)}`),
  });
  const runNow = useMutation({
    mutationFn: (id: number) => api.runTask(id),
    onSuccess: () => {
      // Refetch immediately so the row flips to "running"
      qc.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
  const requestRun = (id: number) => runNow.mutate(id, {
    // A per-call callback belongs to this mounted observer. Unlike a mutation-
    // level callback, react-query drops it when navigation unmounts this list,
    // so a late refusal cannot interrupt the operator on another screen.
    onError: (e) => alert(`Could not start the run: ${String((e as Error).message)}`),
  });
  const stopRun = useMutation({
    mutationFn: (runId: number) => api.cancelRun(runId),
    onSuccess: () => {
      // Both queries: "tasks" drives this row's state pill, and "runs" drives
      // the history and detail views. Without the second, an open run view
      // keeps showing a run the operator just stopped.
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["runs"] });
    },
    // The server's own text is the answer that matters: "already finished
    // (success)" means the transfer completed on its own, which is a different
    // outcome from "the stop failed".
    onError: (e) => alert(`Could not stop the run: ${String((e as Error).message)}`),
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
          <h1 className="text-xl font-semibold">tasks</h1>
          <div className="flex gap-1 text-xs">
            {(["all", "rsync", "zfs"] as const).map(f => (
              <button
                key={f}
                onClick={() => setTypeFilter(f)}
                // No text-transform utility here. One used to sit in this class
                // list and painted the lowercase values as "All"/"Rsync"/"Zfs".
                // Chrome stays lower case; only the run-state pill shouts, and
                // it does so from StatusPill. The offending class name is left
                // unwritten on purpose: Tailwind scans this file for candidate
                // class names and would re-emit the now-dead rule.
                className={`px-2 py-1 rounded border ${typeFilter === f ? "bg-accent text-white border-accent" : "border-border text-muted hover:text-white"}`}
              >
                {f}
              </button>
            ))}
          </div>
        </div>
        <Link to="/tasks/new" className="btn-primary">+ add task</Link>
      </div>
      <div className="bg-panel border border-border rounded hidden sm:block sm:overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-muted text-left">
            <tr className="border-b border-border">
              <SortHeader column="remote_path" label="remote path" pref={sort} onSort={chooseSort} />
              <SortHeader column="type" label="type" pref={sort} onSort={chooseSort} />
              <SortHeader column="source" label="source" pref={sort} onSort={chooseSort} />
              <SortHeader column="frequency" label="frequency" pref={sort} onSort={chooseSort} />
              <SortHeader column="next_run" label="next run" pref={sort} onSort={chooseSort} />
              <SortHeader column="last_run" label="last run" pref={sort} onSort={chooseSort} />
              <SortHeader column="enabled" label="enabled" pref={sort} onSort={chooseSort} />
              <SortHeader column="state" label="state" pref={sort} onSort={chooseSort} />
              <th className="px-4 py-3 font-medium whitespace-nowrap w-36 text-right" aria-sort="none">actions</th>
            </tr>
          </thead>
          <tbody>
            {shown.length === 0 && (
              <tr><td colSpan={9} className="px-4 py-8 text-center text-muted">no tasks{typeFilter !== "all" ? ` of type "${typeFilter}"` : " yet"}. add one to get started.</td></tr>
            )}
            {shown.map(t => {
              const pill = <StatusPill state={t.last_run_state} />;
              return (
              <tr key={t.id} className="border-b border-border last:border-0">
                <td className="px-4 py-3 font-mono text-xs" title={`local: ${t.local_path}`}>{t.remote_path}</td>
                <td className="px-4 py-3 text-xs"><span className="px-1.5 py-0.5 rounded bg-border/60 font-mono">{typeLabel(t)}</span></td>
                <td className="px-4 py-3 text-muted text-xs">{sourceById[t.source_id]?.name ?? "—"}</td>
                <td className="px-4 py-3 font-mono text-xs">{t.cron}</td>
                <td className="px-4 py-3 text-muted text-xs">{t.enabled ? relTime(t.next_run) : "disabled"}</td>
                <td className="px-4 py-3 text-muted text-xs">{lastRunTime(t.last_run_at)}</td>
                <td className="px-4 py-3"><Toggle checked={t.enabled} onChange={() => toggle.mutate(t)} /></td>
                <td className="px-4 py-3">
                  {t.last_run_id ? (
                    <Link to={`/runs/${t.last_run_id}`} title="view last run">{pill}</Link>
                  ) : pill}
                </td>
                <td className="px-4 py-3 text-right whitespace-nowrap">
                  <Link to={`/tasks/${t.id}/runs`} title="run history" aria-label="Run history" className="inline-block w-7 text-center text-muted hover:text-white"><HistoryIcon className="inline-block w-4 h-4 align-middle" /></Link>
                  <Link to={`/tasks/${t.id}/edit`} title="edit" aria-label="Edit task" className="inline-block w-7 text-center text-muted hover:text-white"><PencilIcon className="inline-block w-4 h-4 align-middle" /></Link>
                  <Link to="/tasks/new" state={{ clone: t }} title="clone (opens an unsaved copy)" aria-label="Clone task" className="inline-block w-7 text-center text-muted hover:text-white"><CloneIcon className="inline-block w-4 h-4 align-middle" /></Link>
                  {t.kuma_monitor_id && sys?.kuma_url && (
                    <a href={`${sys.kuma_url}/dashboard/${t.kuma_monitor_id}`} target="_blank" rel="noreferrer" title="Uptime Kuma monitor" aria-label="Uptime Kuma monitor" className="inline-block w-7 text-center text-muted hover:text-white"><BellIcon className="inline-block w-4 h-4 align-middle" /></a>
                  )}
                  <button title="run now" aria-label="Run now" onClick={() => requestRun(t.id)} className="inline-block w-7 text-center text-muted hover:text-white"><PlayIcon className="inline-block w-4 h-4 align-middle" /></button>
                  {isStoppable(t) && (
                    <button title="stop the running run" aria-label="Stop run" onClick={() => confirm(`Stop the running ${typeLabel(t)} run for ${t.name}?`) && stopRun.mutate(t.last_run_id!)} className="inline-block w-7 text-center text-muted hover:text-danger"><StopIcon className="inline-block w-4 h-4 align-middle" /></button>
                  )}
                  <button title="delete" aria-label="Delete task" onClick={() => confirm(`Delete task ${t.name}?`) && del.mutate(t.id)} className="inline-block w-7 text-center text-muted hover:text-danger"><TrashIcon className="inline-block w-4 h-4 align-middle" /></button>
                </td>
              </tr>
            )})}
          </tbody>
        </table>
      </div>

      {/* Phone view: one card per task. The table above carries nine columns,
          which on a phone meant scrolling sideways to learn anything. Here the
          three facts that matter at a glance — state, which task, when it last
          ran — are visible without interaction, and the row actions become
          44px tap targets instead of 28px icons. */}
      <div className="sm:hidden space-y-3">
        {shown.length === 0 && (
          <div className="bg-panel border border-border rounded px-4 py-8 text-center text-muted text-sm">
            no tasks{typeFilter !== "all" ? ` of type "${typeFilter}"` : " yet"}. add one to get started.
          </div>
        )}
        {shown.map(t => (
          <div key={t.id} className="bg-panel border border-border rounded p-3 space-y-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0 space-y-1">
                <div className="font-mono text-xs break-all">{t.remote_path}</div>
                <div className="text-xs text-muted">
                  {sourceById[t.source_id]?.name ?? "—"}
                  <span className="mx-1.5">·</span>
                  <span className="px-1.5 py-0.5 rounded bg-border/60 font-mono">{typeLabel(t)}</span>
                </div>
              </div>
              {t.last_run_id ? (
                <Link to={`/runs/${t.last_run_id}`} title="view last run" className="shrink-0">
                  <StatusPill state={t.last_run_state} />
                </Link>
              ) : <span className="shrink-0"><StatusPill state={t.last_run_state} /></span>}
            </div>

            <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
              <div className="min-w-0">
                <dt className="text-muted">last run</dt>
                <dd className="truncate">{lastRunTime(t.last_run_at)}</dd>
              </div>
              <div className="min-w-0">
                <dt className="text-muted">next run</dt>
                <dd className="truncate">{t.enabled ? relTime(t.next_run) : "disabled"}</dd>
              </div>
            </dl>

            {/* Wraps: six 44px controls (seven with Kuma) plus the toggle and
                label need ~384px, more than an iPhone SE's 343px of usable
                width. Without wrapping the card overflows horizontally, which
                is the scrolling this layout exists to remove. */}
            <div className="flex flex-wrap items-center justify-between gap-y-1 border-t border-border pt-2">
              <div className="flex items-center gap-2 text-xs text-muted">
                <Toggle checked={t.enabled} onChange={() => toggle.mutate(t)} />
                <span>{t.enabled ? "enabled" : "disabled"}</span>
              </div>
              <div className="flex flex-wrap items-center justify-end">
                <Link to={`/tasks/${t.id}/runs`} title="run history" aria-label="Run history" className="min-h-[44px] min-w-[44px] flex items-center justify-center text-muted hover:text-white"><HistoryIcon className="w-5 h-5" /></Link>
                <Link to={`/tasks/${t.id}/edit`} title="edit" aria-label="Edit task" className="min-h-[44px] min-w-[44px] flex items-center justify-center text-muted hover:text-white"><PencilIcon className="w-5 h-5" /></Link>
                <Link to="/tasks/new" state={{ clone: t }} title="clone (opens an unsaved copy)" aria-label="Clone task" className="min-h-[44px] min-w-[44px] flex items-center justify-center text-muted hover:text-white"><CloneIcon className="w-5 h-5" /></Link>
                {t.kuma_monitor_id && sys?.kuma_url && (
                  <a href={`${sys.kuma_url}/dashboard/${t.kuma_monitor_id}`} target="_blank" rel="noreferrer" title="Uptime Kuma monitor" aria-label="Uptime Kuma monitor" className="min-h-[44px] min-w-[44px] flex items-center justify-center text-muted hover:text-white"><BellIcon className="w-5 h-5" /></a>
                )}
                <button title="run now" aria-label="Run now" onClick={() => requestRun(t.id)} className="min-h-[44px] min-w-[44px] flex items-center justify-center text-muted hover:text-white"><PlayIcon className="w-5 h-5" /></button>
                {isStoppable(t) && (
                  <button title="stop the running run" aria-label="Stop run" onClick={() => confirm(`Stop the running ${typeLabel(t)} run for ${t.name}?`) && stopRun.mutate(t.last_run_id!)} className="min-h-[44px] min-w-[44px] flex items-center justify-center text-muted hover:text-danger"><StopIcon className="w-5 h-5" /></button>
                )}
                <button title="delete" aria-label="Delete task" onClick={() => confirm(`Delete task ${t.name}?`) && del.mutate(t.id)} className="min-h-[44px] min-w-[44px] flex items-center justify-center text-muted hover:text-danger"><TrashIcon className="w-5 h-5" /></button>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
