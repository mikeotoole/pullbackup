/**
 * @vitest-environment jsdom
 *
 * The task list renders the server's freshness projection verbatim, in BOTH
 * layouts, and derives nothing itself.
 */
import { afterEach, describe, expect, it } from "vitest";
import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import { TasksList } from "../pages/TasksList";
import { freshnessView } from "../components/FreshnessMarker";

const SOURCE = { id: 1, name: "tang", user: "root", host: "tang.lan", port: 22, ssh_key_path: "/k", task_count: 1, description: "" };
const fresh = (state, reason = "covered") => ({
  state, reason, kuma_status: null, occurrence_at: null, deadline_at: null,
  last_success_run_id: 3, last_success_started_at: null, last_success_at: "2026-10-30T03:30:00Z",
});
const task = (o = {}) => ({
  id: 7, name: "photos", task_type: "rsync", source_id: 1, remote_path: "/srv/photos", local_path: "/data/photos",
  cron: "0 3 * * *", enabled: true, next_run: null, last_run_at: "2026-11-01T03:00:00Z", last_run_id: 42,
  last_run_state: "success", missed_schedule: false, kuma_monitor_id: null, queued: false, due_status: null,
  wait_reason: null, retry_at: null, due_at: null, due_attempts: 0, due_error: null,
  last_success_at: "2026-10-30T03:30:00Z", freshness: fresh("fresh"), ...o,
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
  roots.push(root); hosts.push(host);
  act(() => { root.render(createElement(QueryClientProvider, { client: qc }, createElement(MemoryRouter, null, createElement(TasksList)))); });
  return host;
}
afterEach(() => {
  act(() => { for (const r of roots) r.unmount(); });
  for (const h of hosts) h.remove();
  roots = []; hosts = [];
});
const marks = (host) => [...host.querySelectorAll("[data-freshness-state]")].map((n) => n.getAttribute("data-freshness-state"));

describe("freshness marker", () => {
  it("shows stale and delayed in both the table and the phone card", () => {
    const host = mount([
      task({ id: 1, freshness: fresh("stale", "last_attempt_failed") }),
      task({ id: 2, freshness: fresh("delayed", "queued") }),
    ]);
    // two layouts x (stale, delayed)
    expect(marks(host).sort()).toEqual(["delayed", "delayed", "stale", "stale"]);
  });

  it.each(["fresh", "not_yet_due", "disabled"])("stays quiet for %s", (state) => {
    expect(marks(mount([task({ freshness: fresh(state) })]))).toEqual([]);
  });

  it("stays quiet for an older server that sends no projection", () => {
    expect(marks(mount([task({ freshness: undefined })]))).toEqual([]);
    expect(freshnessView(task({ freshness: null }))).toBeNull();
  });

  it("a successful latest run cannot hide a stale projection, and the reason is explained", () => {
    const host = mount([task({ last_run_state: "success", freshness: fresh("stale", "missed") })]);
    expect(marks(host)).toContain("stale");
    expect(freshnessView(task({ freshness: fresh("stale", "missed") })).title).toContain("did not run");
    expect(freshnessView(task({ freshness: fresh("stale", "last_attempt_failed") })).title).toContain("latest attempt failed");
  });

  it("an unknown future state renders nothing rather than being coerced to healthy-or-broken", () => {
    expect(freshnessView(task({ freshness: fresh("bogus") }))).toBeNull();
  });
});
