import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";

export function Login() {
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.login(username, password);
      navigate("/tasks", { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "sign in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen flex items-start sm:items-center justify-center px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="flex items-center gap-2 font-semibold text-lg mb-6">
          <svg width="22" height="22" viewBox="0 0 32 32" aria-hidden="true">
            <rect width="32" height="32" rx="7" fill="#7c3aed" />
            <g fill="none" stroke="#fff" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
              <path d="M16 5.5 V16.5" />
              <path d="M10.8 11.7 L16 16.9 L21.2 11.7" />
              <path d="M8 20.5 V25 H24 V20.5" />
            </g>
          </svg>
          pullbackup
        </div>

        <form
          onSubmit={submit}
          className="bg-panel border border-border rounded-lg p-5 sm:p-6 space-y-4"
        >
          <h1 className="text-base font-medium">sign in</h1>

          <div className="space-y-1">
            <label className="block text-sm text-muted" htmlFor="username">
              username
            </label>
            <input
              id="username"
              name="username"
              autoComplete="username"
              autoFocus
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="min-h-[44px]"
            />
          </div>

          <div className="space-y-1">
            <label className="block text-sm text-muted" htmlFor="password">
              password
            </label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="min-h-[44px]"
            />
          </div>

          {error && (
            <div
              role="alert"
              className="text-sm text-red-400 border border-red-900 bg-red-950/40 rounded px-3 py-2"
            >
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={busy}
            className="w-full min-h-[44px] rounded bg-accent text-white disabled:opacity-60"
          >
            {busy ? "signing in…" : "sign in"}
          </button>
        </form>

        <p className="text-xs text-muted mt-4">
          scripted access can keep using HTTP Basic against the same credential.
        </p>
      </div>
    </div>
  );
}
