/**
 * @vitest-environment jsdom
 *
 * Retained due work must be visible on the row, in BOTH layouts, and the
 * "last backup" time must be the last SUCCESS, separate from the last run.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import { TasksList } from "../pages/TasksList";

const SOURCE = {
  id: 1, name: "tang", user: "root", host: "tang.lan", port: 22,
  ssh_key_path: "/k", task_count: 1, description: "",
};

const task = (o = {}) => ({
  id: 7, name: "photos", task_type: "rsync", source_id: 1,
  remote_path: "/srv/photos", local_path: "/data/photos", cron: "0 3 * * *",
  enabled: true, next_run: null, last_run_at: "2026-11-01T03:00:00Z",
  last_run_id: 42, last_run_state: "failed", missed_schedule: false,
  kuma_monitor_id: null, queued: false, due_status: null, wait_reason: null,
  retry_at: null, due_at: null, due_attempts: 0, due_error: null,
  last_success_at: "2026-10-30T03:30:00Z", ...o,
});

let roots = [];
let hosts = [];

function mount(tasks) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  qc.setQueryData(["tasks"], tasks);
  qc.setQueryData(["sources"], [SOURCE]);
  qc.setQueryData(["sysinfo"], { kuma_url: "" });
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  roots.push(root);
  hosts.push(host);
  act(() => {
    root.render(createElement(QueryClientProvider, { client: qc },
      createElement(MemoryRouter, null, createElement(TasksList))));
  });
  return host;
}

afterEach(() => {
  act(() => { for (const r of roots) r.unmount(); });
  for (const h of hosts) h.remove();
  roots = []; hosts = [];
  vi.restoreAllMocks();
});

const marks = (host) => [...host.querySelectorAll("[data-due-state]")];

describe("retained due work on the task list", () => {
  it("shows a queued task with its wait reason in both layouts", () => {
    const host = mount([task({ queued: true, due_status: "due", wait_reason: "source_busy", due_at: "2026-11-02T04:30:00Z" })]);
    const m = marks(host);
    expect(m.length).toBe(2); // desktop row + phone card
    for (const el of m) {
      expect(el.getAttribute("data-due-state")).toBe("queued");
      expect(el.textContent).toMatch(/queued/i);
      expect(el.textContent).toMatch(/source busy/i);
    }
  });

  it("shows when a failed run will be retried", () => {
    const host = mount([task({ queued: true, due_status: "retry", retry_at: "2099-01-01T00:00:00Z", due_attempts: 2, due_error: "rsync exited 23" })]);
    const m = marks(host);
    expect(m.length).toBe(2);
    for (const el of m) {
      expect(el.getAttribute("data-due-state")).toBe("retry");
      expect(el.textContent).toMatch(/retry/i);
    }
  });

  it("says plainly when retries are exhausted or the task is blocked", () => {
    const ex = mount([task({ due_status: "exhausted", due_error: "rsync exited 23" })]);
    expect(marks(ex).map((e) => e.getAttribute("data-due-state"))).toEqual(["exhausted", "exhausted"]);
    const bl = mount([task({ id: 8, due_status: "blocked", due_error: "destination not allowed" })]);
    expect(marks(bl).map((e) => e.getAttribute("data-due-state"))).toEqual(["blocked", "blocked"]);
  });

  it("renders nothing for a task that owes nothing", () => {
    expect(marks(mount([task()])).length).toBe(0);
  });

  it("does not render a dispatched obligation as queued", () => {
    // A run is already executing for it; the run pill says so.
    expect(marks(mount([task({ due_status: "dispatched" })])).length).toBe(0);
  });

  it("shows the last SUCCESSFUL backup separately from the last run", () => {
    const host = mount([task()]);
    const cells = [...host.querySelectorAll("[data-last-success]")];
    expect(cells.length).toBe(2);
    for (const c of cells) {
      expect(c.textContent).toMatch(/ago/);
      expect(c.getAttribute("data-last-success")).toBe("2026-10-30T03:30:00Z");
    }
    // The failed last run is still reported by its own pill.
    expect(host.textContent).toMatch(/failed/i);
  });

  it("says no successful backup when there has never been one", () => {
    const host = mount([task({ last_success_at: null })]);
    const cells = [...host.querySelectorAll("[data-last-success]")];
    expect(cells.length).toBe(2);
    for (const c of cells) expect(c.textContent).toMatch(/never/i);
  });

  it("degrades to no marker on a server that predates these fields", () => {
    const { queued, due_status, wait_reason, last_success_at, ...older } = task();
    const host = mount([older]);
    expect(marks(host).length).toBe(0);
  });
});
