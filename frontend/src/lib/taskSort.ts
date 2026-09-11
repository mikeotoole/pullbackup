// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole

/**
 * Only the task fields the comparators read.
 *
 * Structural rather than `import { Task }`: this keeps the sorter testable with
 * small literals instead of the ~40-field API row, and it documents exactly
 * which parts of the payload the ordering depends on.
 */
export type SortableTask = {
  id: number;
  remote_path: string;
  task_type: string;
  source_id: number;
  cron: string;
  enabled: boolean;
  next_run: string | null;
  last_run_at: string | null;
  last_run_state: string | null;
};

/** Columns the tasks table can be ordered by. `actions` is deliberately absent. */
export const SORT_COLUMNS = [
  "remote_path",
  "type",
  "source",
  "frequency",
  "next_run",
  "last_run",
  "enabled",
  "state",
] as const;

export type SortColumn = (typeof SORT_COLUMNS)[number];
export type SortDirection = "asc" | "desc";
export type SortPreference = { column: SortColumn; direction: SortDirection };

/**
 * What clicking `column` should select, given the current preference.
 *
 * A new column always starts ascending — clicking a header you have not used
 * before should never surprise you with a reversed list. Clicking the active
 * header toggles.
 */
export function nextSort(current: SortPreference | null, column: SortColumn): SortPreference {
  if (current && current.column === column) {
    return { column, direction: current.direction === "asc" ? "desc" : "asc" };
  }
  return { column, direction: "asc" };
}

/** The UI surfaces syncoid tasks as "zfs"; sorting must follow what is shown. */
const typeLabel = (t: SortableTask) => (t.task_type === "syncoid" ? "zfs" : "rsync");

/**
 * A sortable value, or `null` meaning "this row has nothing here".
 *
 * Null is not a small value: it sinks to the bottom in BOTH directions. A task
 * that has never run is absent from the ordering, not the oldest, and flipping
 * direction should not dredge a column of dashes to the top.
 */
type Key = string | number | null;

function keyOf(t: SortableTask, column: SortColumn, sourceName: (id: number) => string | null): Key {
  switch (column) {
    case "remote_path":
      return t.remote_path.toLowerCase();
    case "type":
      return typeLabel(t);
    case "source":
      return sourceName(t.source_id)?.toLowerCase() ?? null;
    case "frequency":
      return t.cron;
    case "next_run":
      // The cell reads "disabled" rather than a time for a disabled task, so
      // ordering it by the schedule it does not show would be a lie.
      return t.enabled ? timestamp(t.next_run) : null;
    case "last_run":
      return timestamp(t.last_run_at);
    case "enabled":
      // Ascending puts the tasks that are doing something first.
      return t.enabled ? 0 : 1;
    case "state":
      return t.last_run_state;
  }
}

const HAS_TIMEZONE = /(?:Z|[+-]\d{2}:?\d{2})$/i;

/** Timezone-less API timestamps are UTC — same contract as relativeTime. */
function timestamp(iso: string | null): number | null {
  if (!iso) return null;
  const ms = new Date(HAS_TIMEZONE.test(iso) ? iso : `${iso}Z`).getTime();
  return Number.isFinite(ms) ? ms : null;
}

function compareKeys(a: Key, b: Key): number {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b));
}

/**
 * Order `tasks` by `pref`, leaving the input array untouched.
 *
 * Ties break on task id — always ascending, independent of `direction`. Without
 * a total order, rows with equal keys can swap places between renders (the list
 * refetches every two seconds while a job runs) and the table appears to twitch
 * on its own.
 */
export function sortTasks<T extends SortableTask>(
  tasks: readonly T[],
  pref: SortPreference | null,
  sources: readonly { id: number; name: string }[],
): T[] {
  if (!pref) return [...tasks];
  const names = new Map(sources.map((s) => [s.id, s.name] as const));
  const sourceName = (id: number) => names.get(id) ?? null;
  const sign = pref.direction === "asc" ? 1 : -1;

  return [...tasks].sort((a, b) => {
    const ordered = compareKeys(keyOf(a, pref.column, sourceName), keyOf(b, pref.column, sourceName));
    // `compareKeys` already sinks nulls; only the non-null ordering reverses.
    if (ordered !== 0) {
      const aNull = keyOf(a, pref.column, sourceName) === null;
      const bNull = keyOf(b, pref.column, sourceName) === null;
      return aNull || bNull ? ordered : ordered * sign;
    }
    return a.id - b.id;
  });
}
