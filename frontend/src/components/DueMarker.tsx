// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole

/**
 * "This task owes a backup that has not started" and "when it last succeeded".
 *
 * INCIDENT 2026-11-02: three scheduled backups were refused because a sibling
 * held their source, and the occurrence was dropped. The row looked healthy
 * because nothing said "owed". The server now retains that debt; this is the
 * row saying so, with the reason, so a wait is never mistaken for a skip.
 *
 * A word, not just colour, and deliberately a separate marker from the run
 * pill and from the missed-schedule flag: a row can be queued while its last
 * run failed, and each fact has to survive being read next to the others.
 */
import { relTime } from "../lib/relativeTime";
import type { Task } from "../lib/api";

const WAIT_REASONS: Record<string, string> = {
  source_busy: "source busy",
  destination_busy: "destination busy",
  capacity: "waiting for a free slot",
  task_active: "already running",
};

export type DueView = { kind: "queued" | "retry" | "exhausted" | "blocked"; text: string; title: string };

/** What the row should say about retained work, or null when it owes nothing visible. */
export function dueView(t: Task): DueView | null {
  switch (t.due_status) {
    case "due": {
      const why = t.wait_reason ? WAIT_REASONS[t.wait_reason] ?? t.wait_reason : "starting";
      return { kind: "queued", text: `queued · ${why}`, title: "a scheduled backup is owed and will start as soon as it can" };
    }
    case "retry":
      return {
        kind: "retry",
        text: `retry ${relTime(t.retry_at ?? null)}${t.due_attempts ? ` · attempt ${t.due_attempts}` : ""}`,
        title: t.due_error ? `last failure: ${t.due_error}` : "the last run failed; it will be retried automatically",
      };
    case "exhausted":
      return { kind: "exhausted", text: "retries exhausted", title: t.due_error ? `last failure: ${t.due_error}` : "automatic retries are used up; the next schedule or a manual run will try again" };
    case "blocked":
      return { kind: "blocked", text: "blocked · edit task", title: t.due_error ? `cannot run: ${t.due_error}` : "this task's configuration is invalid; edit it to continue" };
    default:
      return null;
  }
}

export function DueMarker({ task }: { task: Task }) {
  const view = dueView(task);
  if (!view) return null;
  const bad = view.kind === "exhausted" || view.kind === "blocked";
  return (
    <span
      data-due-state={view.kind}
      aria-label={view.text}
      title={view.title}
      className={`inline-flex items-center gap-1 border rounded-full px-2 py-0.5 text-xs whitespace-nowrap ${bad ? "border-danger text-danger" : "border-border text-muted"}`}
    >
      {view.text}
    </span>
  );
}

/** Last SUCCESSFUL backup. Separate from `last run`, which may be a failure. */
export function LastSuccess({ task }: { task: Task }) {
  const at = task.last_success_at ?? null;
  return (
    <span data-last-success={at ?? ""} title="finish time of the last successful backup">
      {at ? relTime(at, "past") : "never"}
    </span>
  );
}
