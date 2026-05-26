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
        <h1 className="text-xl font-semibold">Sources</h1>
        <button className="btn-primary" onClick={() => setEditing({ ...empty })}>+ Add Source</button>
      </div>

      {pubkey && (
        <div className="bg-panel border border-border rounded p-4 space-y-2">
          <div className="text-sm font-semibold text-muted uppercase tracking-wider">pullback's SSH public key</div>
          <div className="text-xs text-muted">Paste this into <code>~/.ssh/authorized_keys</code> on every source host. Restrict it to read-only paths if you can.</div>
          <pre className="bg-bg border border-border rounded p-3 text-xs overflow-x-auto">{pubkey.public_key || "(generated on first start)"}</pre>
        </div>
      )}

      <div className="bg-panel border border-border rounded overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-muted text-left">
            <tr className="border-b border-border">
              <th className="px-4 py-3 font-medium">Name</th>
              <th className="px-4 py-3 font-medium">User@Host:Port</th>
              <th className="px-4 py-3 font-medium">SSH key</th>
              <th className="px-4 py-3 font-medium">Tasks</th>
              <th className="px-4 py-3 font-medium w-24"></th>
            </tr>
          </thead>
          <tbody>
            {sources.length === 0 && <tr><td colSpan={5} className="px-4 py-8 text-center text-muted">No sources yet.</td></tr>}
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

      {editing && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center p-4" onClick={() => setEditing(null)}>
          <form
            className="bg-panel border border-border rounded p-6 w-full max-w-lg space-y-3"
            onClick={e => e.stopPropagation()}
            onSubmit={e => { e.preventDefault(); save.mutate(editing); }}
          >
            <h2 className="text-lg font-semibold">{editing.id ? "Edit" : "New"} source</h2>
            <Field label="Name"><input value={editing.name ?? ""} onChange={e => setEditing({ ...editing, name: e.target.value })} required /></Field>
            <Field label="User"><input value={editing.user ?? ""} onChange={e => setEditing({ ...editing, user: e.target.value })} required /></Field>
            <Field label="Host"><input value={editing.host ?? ""} onChange={e => setEditing({ ...editing, host: e.target.value })} required /></Field>
            <Field label="Port"><input type="number" value={editing.port ?? 22} onChange={e => setEditing({ ...editing, port: Number(e.target.value) })} /></Field>
            <Field label="SSH key path (inside container)"><input value={editing.ssh_key_path ?? ""} onChange={e => setEditing({ ...editing, ssh_key_path: e.target.value })} required /></Field>
            <Field label="Description"><textarea rows={2} value={editing.description ?? ""} onChange={e => setEditing({ ...editing, description: e.target.value })} /></Field>
            <div className="flex justify-end gap-2">
              <button type="button" className="btn-ghost" onClick={() => setEditing(null)}>Cancel</button>
              <button type="submit" className="btn-primary" disabled={save.isPending}>{save.isPending ? "Saving…" : "Save"}</button>
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
