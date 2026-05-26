import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { api, type Task } from "../lib/api";

const empty: Partial<Task> = {
  name: "",
  source_id: 0,
  remote_path: "",
  local_path: "",
  cron: "0 3 * * *",
  enabled: true,
  description: "",
  archive: true,
  recursive: true,
  times: true,
  compress: true,
  delete: false,
  quiet: false,
  preserve_permissions: false,
  preserve_xattrs: false,
  delay_updates: true,
  bwlimit_kbps: null,
  exclude_patterns: "",
  aux_args: "",
  notify_matrix: false,
  notify_matrix_on_success: false,
  kuma_enabled: false,
};

export function TaskForm() {
  const { id } = useParams();
  const editing = !!id;
  const nav = useNavigate();
  const qc = useQueryClient();
  const [form, setForm] = useState<Partial<Task>>(empty);

  const { data: sources = [] } = useQuery({ queryKey: ["sources"], queryFn: api.listSources });
  const { data: sys } = useQuery({ queryKey: ["sysinfo"], queryFn: api.systemInfo });
  const { data: existing } = useQuery({
    queryKey: ["task", id],
    queryFn: () => api.getTask(Number(id)),
    enabled: editing,
  });

  useEffect(() => {
    if (existing) setForm(existing);
    else if (sources.length && !form.source_id) setForm(f => ({ ...f, source_id: sources[0].id }));
  }, [existing, sources]);

  const save = useMutation({
    mutationFn: () => editing ? api.updateTask(Number(id), form) : api.createTask(form),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks"] });
      nav("/tasks");
    },
  });

  const set = <K extends keyof Task>(k: K, v: Task[K]) => setForm(f => ({ ...f, [k]: v }));

  return (
    <form
      className="space-y-6"
      onSubmit={e => { e.preventDefault(); save.mutate(); }}
    >
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">{editing ? "Edit" : "Add"} Rsync Task</h1>
        <div className="space-x-2">
          <button type="button" className="btn-ghost" onClick={() => nav("/tasks")}>Cancel</button>
          <button type="submit" className="btn-primary" disabled={save.isPending}>{save.isPending ? "Saving…" : "Save"}</button>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <Section title="Source">
          <Field label="Name">
            <input value={form.name ?? ""} onChange={e => set("name", e.target.value)} required />
          </Field>
          <Field label="Source host">
            <select value={form.source_id ?? 0} onChange={e => set("source_id", Number(e.target.value))} required>
              <option value={0} disabled>Choose…</option>
              {sources.map(s => <option key={s.id} value={s.id}>{s.name} ({s.user}@{s.host})</option>)}
            </select>
          </Field>
          <Field label="Remote path">
            <input value={form.remote_path ?? ""} onChange={e => set("remote_path", e.target.value)} required placeholder="/mnt/pool0/dataset" />
          </Field>
          <Field label="Local path">
            <input value={form.local_path ?? ""} onChange={e => set("local_path", e.target.value)} required placeholder={`e.g. ${sys?.dest_roots?.[0] ?? "/mnt/dest/backups"}/foo`} />
            {sys?.dest_roots && <div className="text-xs text-muted mt-1">Allowed roots: {sys.dest_roots.join(", ")}</div>}
          </Field>
          <Field label="Description">
            <textarea rows={2} value={form.description ?? ""} onChange={e => set("description", e.target.value)} />
          </Field>
        </Section>

        <Section title="Schedule">
          <Field label="Cron (UTC)">
            <input value={form.cron ?? ""} onChange={e => set("cron", e.target.value)} required placeholder="m h dom mon dow" />
            <div className="text-xs text-muted mt-1">5-field cron. Examples: <code>0 3 * * *</code> daily 03:00 · <code>*/15 * * * *</code> every 15 min</div>
          </Field>
          <Checkbox label="Enabled" checked={!!form.enabled} onChange={v => set("enabled", v)} />
        </Section>

        <Section title="Rsync options">
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
          <Field label="Bandwidth limit (KB/s)">
            <input type="number" value={form.bwlimit_kbps ?? ""} onChange={e => set("bwlimit_kbps", e.target.value ? Number(e.target.value) : null)} />
          </Field>
          <Field label="Exclude patterns (one per line)">
            <textarea rows={3} value={form.exclude_patterns ?? ""} onChange={e => set("exclude_patterns", e.target.value)} />
          </Field>
          <Field label="Auxiliary args (raw)">
            <input value={form.aux_args ?? ""} onChange={e => set("aux_args", e.target.value)} placeholder="--prune-empty-dirs" />
          </Field>
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
