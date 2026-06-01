export type Source = {
  id: number;
  name: string;
  user: string;
  host: string;
  port: number;
  ssh_key_path: string;
  description: string;
  task_count: number;
};

export type RunState = "pending" | "running" | "success" | "failed" | "cancelled";

export type Task = {
  id: number;
  name: string;
  source_id: number;
  remote_path: string;
  local_path: string;
  cron: string;
  enabled: boolean;
  description: string;
  archive: boolean;
  recursive: boolean;
  times: boolean;
  compress: boolean;
  delete: boolean;
  quiet: boolean;
  preserve_permissions: boolean;
  preserve_xattrs: boolean;
  delay_updates: boolean;
  use_sudo: boolean;
  bwlimit_kbps: number | null;
  exclude_patterns: string;
  aux_args: string;
  notify_matrix: boolean;
  notify_matrix_on_success: boolean;
  kuma_enabled: boolean;
  kuma_monitor_id: number | null;
  kuma_push_token: string | null;
  next_run: string | null;
  last_run_id: number | null;
  last_run_at: string | null;
  last_run_state: RunState | null;
};

export type Run = {
  id: number;
  task_id: number;
  state: RunState;
  started_at: string;
  finished_at: string | null;
  exit_code: number | null;
  bytes_transferred: number | null;
  files_transferred: number | null;
  error_message: string;
  log_filename: string;
};

export type SystemInfo = {
  version: string;
  dest_roots: string[];
  tz: string;
  matrix_enabled: boolean;
  kuma_enabled: boolean;
  kuma_url: string;
};

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  if (r.status === 204) return undefined as T;
  return r.json();
}

export const api = {
  systemInfo: () => http<SystemInfo>("/api/system/info"),
  sshPubkey: () => http<{ private_path: string; public_key: string }>("/api/system/ssh-pubkey"),
  browse: (path?: string) =>
    http<{ path: string | null; is_root: boolean; entries: { name: string; path: string; is_dir: boolean }[] }>(
      "/api/system/browse" + (path ? `?path=${encodeURIComponent(path)}` : ""),
    ),

  listSources: () => http<Source[]>("/api/sources"),
  createSource: (body: Partial<Source>) =>
    http<Source>("/api/sources", { method: "POST", body: JSON.stringify(body) }),
  updateSource: (id: number, body: Partial<Source>) =>
    http<Source>(`/api/sources/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteSource: (id: number) => http<void>(`/api/sources/${id}`, { method: "DELETE" }),

  listTasks: () => http<Task[]>("/api/tasks"),
  getTask: (id: number) => http<Task>(`/api/tasks/${id}`),
  createTask: (body: Partial<Task>) =>
    http<Task>("/api/tasks", { method: "POST", body: JSON.stringify(body) }),
  updateTask: (id: number, body: Partial<Task>) =>
    http<Task>(`/api/tasks/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteTask: (id: number) => http<void>(`/api/tasks/${id}`, { method: "DELETE" }),
  runTask: (id: number) => http<{ queued: boolean }>(`/api/tasks/${id}/run`, { method: "POST" }),

  listRuns: (task_id?: number) =>
    http<Run[]>("/api/runs" + (task_id ? `?task_id=${task_id}` : "")),
  getRun: (id: number) => http<Run>(`/api/runs/${id}`),
  getRunLog: (id: number) => http<{ content: string }>(`/api/runs/${id}/log`),
};
