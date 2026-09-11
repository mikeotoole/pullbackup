import { describe, expect, it } from "vitest";

import { nextSort, sortTasks } from "./taskSort";

/** Only the fields the comparators read. */
const task = (over) => ({
  id: 1,
  remote_path: "/a",
  task_type: "rsync",
  source_id: 1,
  cron: "0 3 * * *",
  enabled: true,
  next_run: null,
  last_run_at: null,
  last_run_state: null,
  ...over,
});

const sources = [
  { id: 1, name: "tang" },
  { id: 2, name: "ahi" },
];

const ids = (rows) => rows.map((t) => t.id);

describe("sort toggling", () => {
  it("selects ascending when a new column is clicked", () => {
    expect(nextSort(null, "remote_path")).toEqual({ column: "remote_path", direction: "asc" });
  });

  it("flips direction when the active column is clicked again", () => {
    const asc = { column: "last_run", direction: "asc" };
    expect(nextSort(asc, "last_run")).toEqual({ column: "last_run", direction: "desc" });
    expect(nextSort({ column: "last_run", direction: "desc" }, "last_run")).toEqual(asc);
  });

  it("restarts at ascending when the click moves to another column", () => {
    expect(nextSort({ column: "last_run", direction: "desc" }, "type")).toEqual({
      column: "type",
      direction: "asc",
    });
  });
});

describe("comparators", () => {
  it("orders remote path case-insensitively", () => {
    const rows = [task({ id: 1, remote_path: "/Zulu" }), task({ id: 2, remote_path: "/alpha" })];
    expect(ids(sortTasks(rows, { column: "remote_path", direction: "asc" }, sources))).toEqual([2, 1]);
    expect(ids(sortTasks(rows, { column: "remote_path", direction: "desc" }, sources))).toEqual([1, 2]);
  });

  it("orders by the type LABEL, so syncoid sorts as zfs", () => {
    const rows = [task({ id: 1, task_type: "syncoid" }), task({ id: 2, task_type: "rsync" })];
    expect(ids(sortTasks(rows, { column: "type", direction: "asc" }, sources))).toEqual([2, 1]);
  });

  it("orders by the resolved source NAME, not the source id", () => {
    // source 1 is "tang", source 2 is "ahi": by id that is [1,2], by name [2,1].
    const rows = [task({ id: 1, source_id: 1 }), task({ id: 2, source_id: 2 })];
    expect(ids(sortTasks(rows, { column: "source", direction: "asc" }, sources))).toEqual([2, 1]);
  });

  it("sorts an unknown source after every named one", () => {
    const rows = [task({ id: 1, source_id: 99 }), task({ id: 2, source_id: 1 })];
    expect(ids(sortTasks(rows, { column: "source", direction: "asc" }, sources))).toEqual([2, 1]);
  });

  it("orders frequency by the cron expression text", () => {
    const rows = [task({ id: 1, cron: "30 1 * * *" }), task({ id: 2, cron: "0 3 * * *" })];
    expect(ids(sortTasks(rows, { column: "frequency", direction: "asc" }, sources))).toEqual([2, 1]);
  });

  it("orders next run and last run chronologically", () => {
    const rows = [
      task({ id: 1, next_run: "2026-09-12T03:00:00", last_run_at: "2026-09-10T03:00:00" }),
      task({ id: 2, next_run: "2026-09-11T03:00:00", last_run_at: "2026-09-11T03:00:00" }),
    ];
    expect(ids(sortTasks(rows, { column: "next_run", direction: "asc" }, sources))).toEqual([2, 1]);
    expect(ids(sortTasks(rows, { column: "last_run", direction: "asc" }, sources))).toEqual([1, 2]);
  });

  it("puts tasks with no run time LAST in both directions", () => {
    // A missing time is not "the oldest" — it is absent. Sinking it keeps the
    // rows you can actually read at the top whichever way you sort.
    const rows = [
      task({ id: 1, last_run_at: null }),
      task({ id: 2, last_run_at: "2026-09-10T03:00:00" }),
      task({ id: 3, last_run_at: "2026-09-11T03:00:00" }),
    ];
    expect(ids(sortTasks(rows, { column: "last_run", direction: "asc" }, sources))).toEqual([2, 3, 1]);
    expect(ids(sortTasks(rows, { column: "last_run", direction: "desc" }, sources))).toEqual([3, 2, 1]);
  });

  it("treats a disabled task's next run as absent", () => {
    // The column renders "disabled" rather than a time, so ordering it by the
    // hidden schedule would contradict what the row shows.
    const rows = [
      task({ id: 1, enabled: false, next_run: "2026-09-01T03:00:00" }),
      task({ id: 2, enabled: true, next_run: "2026-09-12T03:00:00" }),
    ];
    expect(ids(sortTasks(rows, { column: "next_run", direction: "asc" }, sources))).toEqual([2, 1]);
  });

  it("orders enabled before disabled ascending", () => {
    const rows = [task({ id: 1, enabled: false }), task({ id: 2, enabled: true })];
    expect(ids(sortTasks(rows, { column: "enabled", direction: "asc" }, sources))).toEqual([2, 1]);
    expect(ids(sortTasks(rows, { column: "enabled", direction: "desc" }, sources))).toEqual([1, 2]);
  });

  it("orders state by name and sinks a task that has never run", () => {
    const rows = [
      task({ id: 1, last_run_state: null }),
      task({ id: 2, last_run_state: "success" }),
      task({ id: 3, last_run_state: "failed" }),
    ];
    expect(ids(sortTasks(rows, { column: "state", direction: "asc" }, sources))).toEqual([3, 2, 1]);
    expect(ids(sortTasks(rows, { column: "state", direction: "desc" }, sources))).toEqual([2, 3, 1]);
  });

  it("breaks ties by task id, so equal rows never shuffle between renders", () => {
    const rows = [task({ id: 9 }), task({ id: 3 }), task({ id: 6 })];
    expect(ids(sortTasks(rows, { column: "enabled", direction: "asc" }, sources))).toEqual([3, 6, 9]);
    // The tie-break does NOT flip with direction: descending "enabled" still
    // leaves equal rows in ascending id order.
    expect(ids(sortTasks(rows, { column: "enabled", direction: "desc" }, sources))).toEqual([3, 6, 9]);
  });

  it("returns the input order untouched when no preference is set", () => {
    const rows = [task({ id: 9 }), task({ id: 3 })];
    expect(ids(sortTasks(rows, null, sources))).toEqual([9, 3]);
  });

  it("does not mutate the array it was given", () => {
    const rows = [task({ id: 9 }), task({ id: 3 })];
    sortTasks(rows, { column: "enabled", direction: "asc" }, sources);
    expect(ids(rows)).toEqual([9, 3]);
  });
});
