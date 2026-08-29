import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Source } from "../lib/api";

const empty: Partial<Source> = { name: "", user: "root", host: "", port: 22, ssh_key_path: "/data/ssh/id_ed25519", description: "" };

export function Sources() {
  const qc = useQueryClient();
  const { data: sources = [] } = useQuery({ queryKey: ["sources"], queryFn: api.listSources });
  const { data: pubkey } = useQuery({ queryKey: ["pubkey"], queryFn: api.sshPubkey });
  const [editing, setEditing] = useState<Partial<Source> | null>(null);

  const save = useMutation({
    mutationFn: (s: Partial<Source>) => s.id ? api.updateSource(s.id, s) : api.createSource(s),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["sources"] }); setEditing(null); },
  });
  const del = useMutation({
    mutationFn: (id: number) => api.deleteSource(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sources"] }),
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">sources</h1>
        <button className="btn-primary" onClick={() => setEditing({ ...empty })}>+ add source</button>
      </div>

      {pubkey && (
        <div className="bg-panel border border-border rounded p-4 space-y-2">
          <div className="text-sm font-semibold text-muted uppercase tracking-wider">pullbackup's SSH public key</div>
          <div className="text-xs text-muted">Paste this into <code>~/.ssh/authorized_keys</code> on every source host. Restrict it to read-only paths if you can.</div>
          {/* The key is one long unbreakable token. On a phone, wrapping it is
              far better than a nested horizontal scroller inside the page. */}
          <pre className="bg-bg border border-border rounded p-3 text-xs whitespace-pre-wrap break-all sm:whitespace-pre sm:break-normal sm:overflow-x-auto">{pubkey.public_key || "(generated on first start)"}</pre>
        </div>
      )}

      <div className="bg-panel border border-border rounded hidden sm:block sm:overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-muted text-left">
            <tr className="border-b border-border">
              <th className="px-4 py-3 font-medium">name</th>
              <th className="px-4 py-3 font-medium">user@host:port</th>
              <th className="px-4 py-3 font-medium">SSH key</th>
              <th className="px-4 py-3 font-medium">tasks</th>
              <th className="px-4 py-3 font-medium w-24"></th>
            </tr>
          </thead>
          <tbody>
            {sources.length === 0 && <tr><td colSpan={5} className="px-4 py-8 text-center text-muted">no sources yet.</td></tr>}
            {sources.map(s => (
              <tr key={s.id} className="border-b border-border last:border-0">
                <td className="px-4 py-3">{s.name}</td>
                <td className="px-4 py-3 font-mono text-xs">{s.user}@{s.host}:{s.port}</td>
                <td className="px-4 py-3 font-mono text-xs text-muted">{s.ssh_key_path}</td>
                <td className="px-4 py-3 text-muted">{s.task_count}</td>
                <td className="px-4 py-3 text-right space-x-2">
                  <button className="text-muted hover:text-white" onClick={() => setEditing(s)}>✎</button>
                  <button className="text-muted hover:text-danger" disabled={s.task_count > 0} onClick={() => confirm(`Delete source ${s.name}?`) && del.mutate(s.id)}>🗑</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Phone view: source cards. user@host:port and the key path are long
          monospace strings, so they wrap here rather than forcing the whole
          table sideways. */}
      <div className="sm:hidden space-y-3">
        {sources.length === 0 && (
          <div className="bg-panel border border-border rounded px-4 py-8 text-center text-muted text-sm">no sources yet.</div>
        )}
        {sources.map(s => (
          <div key={s.id} className="bg-panel border border-border rounded p-3 space-y-2">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0 space-y-1">
                <div className="font-medium">{s.name}</div>
                <div className="font-mono text-xs text-muted break-all">{s.user}@{s.host}:{s.port}</div>
              </div>
              <div className="flex items-center shrink-0">
                <button aria-label="Edit source" className="min-h-[44px] min-w-[44px] flex items-center justify-center text-muted hover:text-white" onClick={() => setEditing(s)}>✎</button>
                <button
                  aria-label="Delete source"
                  className="min-h-[44px] min-w-[44px] flex items-center justify-center text-muted hover:text-danger disabled:opacity-40"
                  disabled={s.task_count > 0}
                  title={s.task_count > 0 ? "in use by a task" : "delete source"}
                  onClick={() => confirm(`Delete source ${s.name}?`) && del.mutate(s.id)}
                >🗑</button>
              </div>
            </div>
            <div className="font-mono text-xs text-muted break-all">{s.ssh_key_path}</div>
            <div className="text-xs text-muted border-t border-border pt-2">{s.task_count} task{s.task_count === 1 ? "" : "s"}</div>
          </div>
        ))}
      </div>

      {editing && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center p-4" onClick={() => setEditing(null)}>
          <form
            className="bg-panel border border-border rounded p-6 w-full max-w-lg space-y-3"
            onClick={e => e.stopPropagation()}
            onSubmit={e => { e.preventDefault(); save.mutate(editing); }}
          >
            <h2 className="text-lg font-semibold">{editing.id ? "edit" : "new"} source</h2>
            <Field label="name"><input value={editing.name ?? ""} onChange={e => setEditing({ ...editing, name: e.target.value })} required /></Field>
            <Field label="user"><input value={editing.user ?? ""} onChange={e => setEditing({ ...editing, user: e.target.value })} required /></Field>
            <Field label="host"><input value={editing.host ?? ""} onChange={e => setEditing({ ...editing, host: e.target.value })} required /></Field>
            <Field label="port"><input type="number" value={editing.port ?? 22} onChange={e => setEditing({ ...editing, port: Number(e.target.value) })} /></Field>
            <Field label="SSH key path (inside container)"><input value={editing.ssh_key_path ?? ""} onChange={e => setEditing({ ...editing, ssh_key_path: e.target.value })} required /></Field>
            <Field label="description"><textarea rows={2} value={editing.description ?? ""} onChange={e => setEditing({ ...editing, description: e.target.value })} /></Field>
            <div className="flex justify-end gap-2">
              <button type="button" className="btn-ghost" onClick={() => setEditing(null)}>cancel</button>
              <button type="submit" className="btn-primary" disabled={save.isPending}>{save.isPending ? "saving…" : "save"}</button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="block"><div className="text-sm mb-1">{label}</div>{children}</label>;
}
