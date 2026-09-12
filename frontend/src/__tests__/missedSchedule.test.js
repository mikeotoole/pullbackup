/**
 * @vitest-environment jsdom
 *
 * Needs a real DOM: the behaviour under test is what a row RENDERS for a given
 * server payload, and the flag has to be observable next to the run-state pill
 * it must not be confused with.
 */
// A task whose schedule stopped firing must say so on its own row.
//
// INCIDENT 2026-09-06..10: for four days this list showed `success` on every
// row while nothing had executed since the 6th. Both facts were true — the
// last COMPLETED run really did succeed — and the list only showed one of
// them. The missed-schedule flag is the other one.
//
// Three properties, deliberately not conflated:
//
//   * The flag renders when the server says the schedule was missed, in BOTH
//     layouts. The phone card is where an operator actually checks a backup
//     at 7am, and a desktop-only badge would miss exactly that reader.
//   * It is DISTINCT from the run-state pill. A `success` row carrying a
//     missed-schedule flag is the incident's exact shape, and if the flag
//     were folded into the pill's vocabulary the row could only say one of
//     the two things again.
//   * It does not render when the schedule is healthy, whatever the last run
//     did. A failed backup is not a wedged scheduler.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { TasksList } from "../pages/TasksList";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "..");
const read = (p) => readFileSync(resolve(SRC, p), "utf8");

const SOURCE = {
  id: 1,
  name: "tang",
  user: "root",
  host: "tang.lan",
  port: 22,
  ssh_key_path: "/data/ssh/id_ed25519",
  task_count: 1,
  description: "",
};

const task = (overrides = {}) => ({
  id: 7,
  name: "photos",
  task_type: "rsync",
  source_id: 1,
  remote_path: "/srv/photos",
  local_path: "/data/photos",
  cron: "0 3 * * *",
  enabled: true,
  next_run: null,
  last_run_at: "2026-09-06T03:00:00Z",
  last_run_id: 42,
  last_run_state: "success",
  missed_schedule: false,
  kuma_monitor_id: null,
  ...overrides,
});

let roots = [];
let hosts = [];

function mount(node, seed = () => {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, enabled: false } },
  });
  seed(qc);
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  roots.push(root);
  hosts.push(host);
  act(() => {
    root.render(
      createElement(
        QueryClientProvider,
        { client: qc },
        createElement(MemoryRouter, null, node),
      ),
    );
  });
  return { host, qc };
}

const tasksList = (tasks) =>
  mount(createElement(TasksList), (qc) => {
    qc.setQueryData(["tasks"], tasks);
    qc.setQueryData(["sources"], [SOURCE]);
    qc.setQueryData(["sysinfo"], { kuma_url: "" });
  });

/** The missed-schedule flags, across BOTH layout branches. */
const flags = (host) => [...host.querySelectorAll("[data-missed-schedule]")];

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  act(() => {
    for (const root of roots) root.unmount();
  });
  for (const host of hosts) host.remove();
  roots = [];
  hosts = [];
  vi.restoreAllMocks();
});

describe("a task whose schedule stopped firing", () => {
  it("flags the row when the server reports a missed schedule", () => {
    const { host } = tasksList([task({ missed_schedule: true })]);
    // Two: the desktop table row and the phone card. One means a layout was
    // converted and the other missed — the same count the Stop control and
    // the icon suite assert, for the same reason.
    expect(flags(host).length).toBe(2);
  });

  it("does not flag a row whose schedule is on time", () => {
    const { host } = tasksList([task({ missed_schedule: false })]);
    expect(flags(host).length).toBe(0);
  });

  it("flags a MISSED schedule even while the last run reads success", () => {
    // The incident in one assertion. `last_run_state: success` was true and
    // useless; the row has to be able to say both things at once.
    const { host } = tasksList([
      task({ missed_schedule: true, last_run_state: "success" }),
    ]);
    expect(flags(host).length).toBe(2);
    expect(host.textContent).toMatch(/success/i);
  });

  it("does not flag a failed run whose schedule is still firing", () => {
    // A failed backup is a different fault with a different fix. The flag
    // must not become a second way of saying "something is wrong".
    const { host } = tasksList([
      task({ missed_schedule: false, last_run_state: "failed" }),
    ]);
    expect(flags(host).length).toBe(0);
  });

  it("keeps the flag out of the run-state pill's vocabulary", () => {
    // If "missed" were a pill state the row could show the last run OR the
    // schedule, never both — which is how four days went unnoticed.
    const pill = read("components/StatusPill.tsx");
    expect(pill).not.toMatch(/missed/i);
  });

  it("says what is wrong in words, not only in colour", () => {
    const { host } = tasksList([task({ missed_schedule: true })]);
    for (const flag of flags(host)) {
      const described = `${flag.textContent} ${flag.getAttribute("title") ?? ""}`;
      expect(described, "the flag must name the fault").toMatch(/schedule/i);
    }
  });

  it("gives the flag an accessible name rather than colour alone", () => {
    const { host } = tasksList([task({ missed_schedule: true })]);
    for (const flag of flags(host)) {
      const name = flag.getAttribute("aria-label") ?? flag.textContent ?? "";
      expect(name.trim().length, "the flag must be readable by a screen reader")
        .toBeGreaterThan(0);
    }
  });

  it("flags only the rows the server flagged", () => {
    const { host } = tasksList([
      task({ id: 1, remote_path: "/a", missed_schedule: true }),
      task({ id: 2, remote_path: "/b", missed_schedule: false }),
      task({ id: 3, remote_path: "/c", missed_schedule: true }),
    ]);
    // Two flagged rows, two layouts.
    expect(flags(host).length).toBe(4);
  });

  it("treats a payload with no missed_schedule field as unflagged", () => {
    // An older server, or a response shaped before this field existed. The
    // list must degrade to its previous behaviour rather than flag everything.
    const { missed_schedule, ...older } = task();
    const { host } = tasksList([older]);
    expect(flags(host).length).toBe(0);
  });
});

describe("the missed-schedule flag's layout contract", () => {
  it("renders in the phone card as well as the desktop table", () => {
    // The count assertion above already proves BOTH layouts render a flag —
    // a desktop-only badge would count one. This pins WHERE the phone one
    // lives, so a later refactor that drops the card branch fails here with a
    // readable reason instead of as an arithmetic surprise. Asserted against
    // source, like the sibling mobileLayout and stop-control suites, because
    // jsdom applies no CSS and cannot tell the two branches apart.
    const src = read("pages/TasksList.tsx");
    const mobile = src.slice(src.indexOf("sm:hidden space-y-3"));
    expect(mobile, "the phone card must render the flag").toMatch(
      /<MissedScheduleFlag\b/,
    );
  });

  it("does not bury the flag inside a truncating phone cell", () => {
    // The card's `next run` cell is ~160px wide and carries `truncate`. A
    // fault notice clipped in half is the same silence this card exists to
    // end, so the flag must not live inside that <dl>.
    const src = read("pages/TasksList.tsx");
    const mobile = src.slice(src.indexOf("sm:hidden space-y-3"));
    const definitionList = mobile.slice(
      mobile.indexOf("<dl"),
      mobile.indexOf("</dl>"),
    );
    expect(definitionList).not.toMatch(/<MissedScheduleFlag\b/);
  });
});
