// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole

/**
 * Successful-backup freshness, exactly as the server projected it.
 *
 * The server (services/freshness.py) is the only place this is computed; the
 * same projection decides what Uptime Kuma is told. This component only
 * renders `task.freshness` and never derives a state of its own, so the row
 * and the monitor cannot disagree. Shown only for the states that need
 * attention (`delayed`, `stale`) -- a healthy row stays quiet.
 */
import type { Freshness, Task } from "../lib/api";

const REASONS: Record<string, string> = {
  awaiting_start: "waiting for the scheduled run to start",
  queued: "queued behind other work",
  running: "a run is in progress",
  retrying: "last attempt failed; retry scheduled",
  retries_exhausted: "automatic retries used up",
  blocked: "configuration invalid; edit the task",
  last_attempt_failed: "the latest attempt failed",
  last_attempt_cancelled: "the latest attempt was cancelled",
  missed: "the scheduled backup did not run",
  never_succeeded: "no successful backup yet",
  invalid_schedule: "the cron expression is invalid",
};

export type FreshnessView = { kind: "delayed" | "stale"; text: string; title: string };

export function freshnessView(t: Task): FreshnessView | null {
  const f: Freshness | null | undefined = t.freshness;
  if (!f || (f.state !== "delayed" && f.state !== "stale")) return null;
  const why = REASONS[f.reason] ?? f.reason;
  return f.state === "stale"
    ? { kind: "stale", text: "stale backup", title: `No successful backup within its grace period: ${why}. Kuma monitor is DOWN.` }
    : { kind: "delayed", text: "backup delayed", title: `A scheduled backup is overdue but inside its grace period: ${why}.` };
}

export function FreshnessMarker({ task }: { task: Task }) {
  const view = freshnessView(task);
  if (!view) return null;
  return (
    <span
      data-freshness-state={view.kind}
      aria-label={view.text}
      title={view.title}
      className={`inline-flex items-center gap-1 border rounded-full px-2 py-0.5 text-xs whitespace-nowrap ${view.kind === "stale" ? "border-danger text-danger" : "border-border text-muted"}`}
    >
      {view.text}
    </span>
  );
}
