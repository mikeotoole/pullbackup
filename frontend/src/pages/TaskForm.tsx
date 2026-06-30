import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import cronstrue from "cronstrue";
import { api, type Task } from "../lib/api";

function describeCron(cron: string | undefined): { text: string; ok: boolean } {
  if (!cron) return { text: "", ok: false };
  try {
    return { text: cronstrue.toString(cron, { verbose: false }), ok: true };
  } catch {
    return { text: "invalid cron expression", ok: false };
  }
}

const empty: Partial<Task> = {
  name: "",
  source_id: 0,
  remote_path: "",
  local_path: "",
  cron: "0 3 * * *",
  enabled: true,
  description: "",
  task_type: "rsync",
  syncoid_recursive: true,
  syncoid_no_sync_snap: true,
  syncoid_compress: "",
  syncoid_extra_args: "",
  syncoid_force_full: false,
  prune_keep_hourly: null,
  archive: true,
  recursive: true,
  times: true,
  compress: true,
  delete: false,
  quiet: false,
  preserve_permissions: false,
  preserve_xattrs: false,
  delay_updates: false,
  use_sudo: false,
  bwlimit_kbps: null,
  exclude_patterns: "",
  aux_args: "",
  notify_matrix: false,
  notify_matrix_on_success: false,
  kuma_enabled: false,
};

function splitLocalPath(p: string | undefined, roots: string[]): [string, string] {
  if (!p) return [roots[0] ?? "", ""];
  const root = roots.find(r => p === r || p.startsWith(r.endsWith("/") ? r : r + "/"));
  if (!root) return [roots[0] ?? "", p];
  const rest = p.slice(root.length).replace(/^\/+/, "");
  return [root, rest];
}

function nextHourlyCron(tasks: Task[]): string {
  const used = new Set<number>();
  for (const t of tasks) {
    if (!t.enabled) continue;
    const m = /^(\d+) \* \* \* \*$/.exec(t.cron);
    if (m) used.add(Number(m[1]));
  }
  for (let i = 0; i < 60; i++) if (!used.has(i)) return `${i} * * * *`;
  return "0 * * * *";
}

function nextDailyCron(tasks: Task[]): string {
  const used = new Set<number>();
  for (const t of tasks) {
    if (!t.enabled) continue;
    const m = /^\d+ (\d+) \* \* \*$/.exec(t.cron);
    if (m) used.add(Number(m[1]));
  }
  for (let h = 3; h < 27; h++) {
    const hh = h % 24;
    if (!used.has(hh)) return `0 ${hh} * * *`;
  }
  return "0 3 * * *";
}

export function TaskForm() {
  const { id } = useParams();
  const editing = !!id;
  const nav = useNavigate();
  const qc = useQueryClient();
  // Clone: the list passes a task via router state to /tasks/new → start as an unsaved copy.
  const cloneFrom = (useLocation().state as { clone?: Task } | null)?.clone;
  const [form, setForm] = useState<Partial<Task>>(() =>
    cloneFrom ? { ...cloneFrom, id: undefined, name: `${cloneFrom.name} copy` } : empty
  );

  const { data: sources = [] } = useQuery({ queryKey: ["sources"], queryFn: api.listSources });
  const { data: sys } = useQuery({ queryKey: ["sysinfo"], queryFn: api.systemInfo });
  const { data: allTasks = [] } = useQuery({ queryKey: ["tasks"], queryFn: api.listTasks });
  const { data: existing } = useQuery({
    queryKey: ["task", id],
    queryFn: () => api.getTask(Number(id)),
    enabled: editing,
  });

  const roots = sys?.dest_roots ?? [];
  const [rootChoice, subdir] = useMemo(() => splitLocalPath(form.local_path, roots), [form.local_path, roots]);
  const isSyncoid = form.task_type === "syncoid";

  useEffect(() => {
    if (existing) setForm(existing);
    else if (sources.length && !form.source_id) setForm(f => ({ ...f, source_id: sources[0].id }));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [existing, sources]);

  const save = useMutation({
    mutationFn: () => {
      // rsync: local_path must be under a configured dest root. syncoid: it's a ZFS dataset name.
      if (!isSyncoid) {
        const lp = (form.local_path ?? "").trim();
        const rootMatch = roots.some(r => lp === r || lp.startsWith(r.endsWith("/") ? r : r + "/"));
        if (!rootMatch) {
          throw new Error("Local path must be beneath one of the configured destination roots.");
        }
      }
      return editing ? api.updateTask(Number(id), form) : api.createTask(form);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks"] });
      nav("/tasks");
    },
  });

  const set = <K extends keyof Task>(k: K, v: Task[K]) => setForm(f => ({ ...f, [k]: v }));

  const setLocalParts = (root: string, sub: string) => {
    const clean = sub.replace(/^\/+/, "");
    set("local_path", clean ? `${root.replace(/\/+$/, "")}/${clean}` : root);
  };

  return (
    <form
      className="space-y-6"
      onSubmit={e => { e.preventDefault(); save.mutate(); }}
    >
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">{editing ? "Edit" : "Add"} Task</h1>
        <div className="space-x-2">
          <button type="button" className="btn-ghost" onClick={() => nav("/tasks")}>Cancel</button>
          <button type="submit" className="btn-primary" disabled={save.isPending}>{save.isPending ? "Saving…" : "Save"}</button>
        </div>
      </div>
      {save.error && <div className="bg-danger/20 border border-danger text-danger px-3 py-2 rounded text-sm">{String((save.error as Error).message)}</div>}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <Section title="Source">
          <Field label="Type">
            <select value={form.task_type ?? "rsync"} onChange={e => set("task_type", e.target.value)}>
              <option value="rsync">rsync (files)</option>
              <option value="syncoid">syncoid (ZFS replication)</option>
            </select>
          </Field>
          <Field label="Name">
            <input value={form.name ?? ""} onChange={e => set("name", e.target.value)} required />
          </Field>
          <Field label="Source host">
            <select value={form.source_id ?? 0} onChange={e => set("source_id", Number(e.target.value))} required>
              <option value={0} disabled>Choose…</option>
              {sources.map(s => <option key={s.id} value={s.id}>{s.name} ({s.user}@{s.host})</option>)}
            </select>
          </Field>
          <Field label={isSyncoid ? "Remote dataset" : "Remote path"}>
            <input value={form.remote_path ?? ""} onChange={e => set("remote_path", e.target.value)} required placeholder={isSyncoid ? "pool0/docker/eel" : "/mnt/pool0/dataset"} />
          </Field>
          {isSyncoid ? (
            <Field label="Local dataset">
              <input value={form.local_path ?? ""} onChange={e => set("local_path", e.target.value)} required placeholder="cache/docker_remote/eel" />
              <div className="text-xs text-muted mt-1">Local ZFS dataset name (no <span className="font-mono">/mnt</span>, no leading slash). <span className="font-mono">zfs receive</span> creates it.</div>
            </Field>
          ) : (
            <Field label="Local path">
              <div className="grid grid-cols-[minmax(0,1fr)_auto_minmax(0,2fr)] gap-2 items-center">
                <select value={rootChoice} onChange={e => setLocalParts(e.target.value, subdir)}>
                  {roots.map(r => <option key={r} value={r}>{r}</option>)}
                </select>
                <span className="text-muted">/</span>
                <input
                  value={subdir}
                  onChange={e => setLocalParts(rootChoice, e.target.value)}
                  placeholder="subdir/path (optional)"
                />
              </div>
              <div className="text-xs text-muted mt-1">Resolves to: <span className="font-mono">{form.local_path || "—"}</span></div>
            </Field>
          )}
          <Field label="Description">
            <textarea rows={2} value={form.description ?? ""} onChange={e => set("description", e.target.value)} />
          </Field>
        </Section>

        <Section title="Schedule">
          <Field label={`Cron (${sys?.tz ?? "UTC"})`}>
            <input value={form.cron ?? ""} onChange={e => set("cron", e.target.value)} required placeholder="m h dom mon dow" />
            {(() => {
              const d = describeCron(form.cron);
              return (
                <div className={`text-sm mt-1.5 ${d.ok ? "text-accent-hover" : "text-danger"}`}>
                  {d.text ? `${d.text} (${sys?.tz ?? "UTC"})` : ""}
                </div>
              );
            })()}
            <div className="text-xs text-muted mt-1 flex gap-2 flex-wrap items-center">
              <button type="button" className="btn-ghost !px-2 !py-1 !text-xs" onClick={() => set("cron", nextHourlyCron(allTasks))}>Next free hourly</button>
              <button type="button" className="btn-ghost !px-2 !py-1 !text-xs" onClick={() => set("cron", nextDailyCron(allTasks))}>Next free daily</button>
              <span className="text-muted">5-field cron. Suggestions pick a minute/hour no other enabled task uses.</span>
            </div>
          </Field>
          <Checkbox label="Enabled" checked={!!form.enabled} onChange={v => set("enabled", v)} />
        </Section>

        <Section title={isSyncoid ? "Syncoid options" : "Rsync options"}>
          {isSyncoid && <>
            <Checkbox label="Recursive (--recursive)" checked={!!form.syncoid_recursive} onChange={v => set("syncoid_recursive", v)} />
            <Checkbox label="No sync snapshot (--no-sync-snap)" checked={!!form.syncoid_no_sync_snap} onChange={v => set("syncoid_no_sync_snap", v)} />
            <Checkbox
              label="Replicate from scratch (--force-delete) — destroys & recreates the target"
              checked={!!form.syncoid_force_full}
              onChange={v => set("syncoid_force_full", v)}
            />
            {form.syncoid_force_full && (
              <p style={{ color: "#f59e0b", fontSize: "0.8rem", margin: "0.25rem 0 0.5rem 1.6rem" }}>
                ⚠ Destroys the existing target dataset and does a full initial send. Use when the
                replica is out of sync (“no snapshots matching”). Turn back off once it’s tracking.
              </p>
            )}
            <Field label="Compression (--compress)">
              <select value={form.syncoid_compress ?? ""} onChange={e => set("syncoid_compress", e.target.value)}>
                <option value="">default</option>
                <option value="none">none</option>
                <option value="lz4">lz4</option>
                <option value="zstd-fast">zstd-fast</option>
                <option value="gzip">gzip</option>
              </select>
            </Field>
            <Field label="Prune: keep N newest hourly snaps on dest (blank = no prune)">
              <input type="number" value={form.prune_keep_hourly ?? ""} onChange={e => set("prune_keep_hourly", e.target.value ? Number(e.target.value) : null)} placeholder="e.g. 24" />
            </Field>
            <Field label="Extra syncoid args (raw)">
              <input value={form.syncoid_extra_args ?? ""} onChange={e => set("syncoid_extra_args", e.target.value)} placeholder="--no-privilege-elevation --mbuffer-size=128M" />
            </Field>
          </>}
          {!isSyncoid && <>
          <Checkbox label="Archive (-a)" checked={!!form.archive} onChange={v => set("archive", v)} />
          {!form.archive && <>
            <Checkbox label="Recursive (-r)" checked={!!form.recursive} onChange={v => set("recursive", v)} />
            <Checkbox label="Times (-t)" checked={!!form.times} onChange={v => set("times", v)} />
            <Checkbox label="Preserve permissions (-p)" checked={!!form.preserve_permissions} onChange={v => set("preserve_permissions", v)} />
          </>}
          <Checkbox label="Compress (-z)" checked={!!form.compress} onChange={v => set("compress", v)} />
          <Checkbox label="Preserve xattrs (-X)" checked={!!form.preserve_xattrs} onChange={v => set("preserve_xattrs", v)} />
          <Checkbox label="Delete extraneous on destination (--delete)" checked={!!form.delete} onChange={v => set("delete", v)} />
          <Checkbox label="Quiet (-q)" checked={!!form.quiet} onChange={v => set("quiet", v)} />
          <Checkbox label="Delay updates (--delay-updates)" checked={!!form.delay_updates} onChange={v => set("delay_updates", v)} />
          <Checkbox label="Use sudo on remote (read root-owned files)" checked={!!form.use_sudo} onChange={v => set("use_sudo", v)} />
          <Field label="Bandwidth limit (KB/s)">
            <input type="number" value={form.bwlimit_kbps ?? ""} onChange={e => set("bwlimit_kbps", e.target.value ? Number(e.target.value) : null)} />
          </Field>
          <Field label="Exclude patterns (one per line)">
            <textarea rows={3} value={form.exclude_patterns ?? ""} onChange={e => set("exclude_patterns", e.target.value)} />
          </Field>
          <Field label="Auxiliary args (raw)">
            <input value={form.aux_args ?? ""} onChange={e => set("aux_args", e.target.value)} placeholder="--rsync-path='sudo /usr/bin/rsync'" />
          </Field>
          </>}
        </Section>

        <Section title="Notifications">
          {sys?.matrix_enabled ? (
            <>
              <Checkbox label="Send Matrix message on failure" checked={!!form.notify_matrix} onChange={v => set("notify_matrix", v)} />
              <Checkbox label="…also on success" checked={!!form.notify_matrix_on_success} onChange={v => set("notify_matrix_on_success", v)} disabled={!form.notify_matrix} />
            </>
          ) : <div className="text-xs text-muted">Matrix not configured in env.</div>}

          {sys?.kuma_enabled ? (
            <>
              <Checkbox label="Create Uptime Kuma push monitor for this task" checked={!!form.kuma_enabled} onChange={v => set("kuma_enabled", v)} />
              <div className="text-xs text-muted">When enabled, pullback creates/updates a Kuma push monitor named <code>pullback: {form.name || "<task>"}</code>. Heartbeat interval tracks the cron schedule.</div>
            </>
          ) : <div className="text-xs text-muted">Uptime Kuma not configured in env.</div>}
        </Section>
      </div>
    </form>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-panel border border-border rounded p-4 space-y-3">
      <h2 className="text-sm font-semibold text-muted uppercase tracking-wider">{title}</h2>
      {children}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className="text-sm mb-1">{label}</div>
      {children}
    </label>
  );
}

function Checkbox({ label, checked, onChange, disabled }: { label: string; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <label className={`flex items-center gap-2 text-sm ${disabled ? "opacity-50" : ""}`}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={e => onChange(e.target.checked)} className="accent-accent w-4 h-4" />
      {label}
    </label>
  );
}
