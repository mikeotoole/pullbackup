/**
 * @vitest-environment jsdom
 *
 * These tests need a real DOM: the behaviour under test is a sequence of state
 * transitions a user causes, and static markup cannot observe any of it. The
 * project default stays `node` — see the comment in vite.config.ts for why
 * making jsdom global breaks two sibling suites.
 */
// Stopping a run from the UI, and what the surrounding controls must NOT do.

//
// These drive real React through `createRoot` in jsdom, because the behaviour
// under test is a sequence of state transitions a user causes — click, confirm,
// mutation resolves or rejects, queries refetch — and static markup cannot
// observe any of it. The repo's older suites render to static markup, which is
// right for a class contract; it is the wrong instrument here.
//
// Three separate properties, deliberately not conflated:
//
//   * Stop appears only for a RUNNING run, and is confirmed before it fires.
//   * A refused stop surfaces the API's own message rather than a generic one,
//     because "run 42 already finished (success)" is the difference between
//     "it stopped" and "it finished on its own".
//   * Stop and Run now stay separate controls. Fusing them into one toggle
//     would make a mis-tap start a transfer the operator meant to end.

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
import { api } from "../lib/api";

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
  last_run_at: "2026-09-02T03:00:00Z",
  last_run_id: 42,
  last_run_state: "success",
  kuma_monitor_id: null,
  ...overrides,
});

// Every root created in a test, unmounted in afterEach. Clearing innerHTML
// alone removes nodes without running effect cleanup, which leaves react-query's
// polling timers live and turns a green suite into post-teardown noise.
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

const tasksList = (tasks, seed = () => {}) =>
  mount(createElement(TasksList), (qc) => {
    qc.setQueryData(["tasks"], tasks);
    qc.setQueryData(["sources"], [SOURCE]);
    qc.setQueryData(["sysinfo"], { kuma_url: "" });
    seed(qc);
  });

/** Controls by accessible name, across BOTH layout branches. */
const byName = (host, name) => [
  ...host.querySelectorAll(`[aria-label="${name}"]`),
];

/**
 * The enabled switch for a row.
 *
 * Selected by its shape rather than an accessible name because `Toggle` renders
 * a bare `<button>` with no aria-label and no text — a real accessibility gap,
 * but a pre-existing one outside this card's scope, raised as a triage card
 * rather than absorbed here. When it gains a name, switch this to that name.
 */
const enabledToggle = (host) =>
  host.querySelector('button.rounded-full[class*="w-10"]');

/** Flush the promise chain a mutation's onSuccess/onError sits behind. */
async function settle() {
  for (let i = 0; i < 8; i++) {
    await act(async () => {
      await Promise.resolve();
    });
  }
}

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

describe("stopping a running run", () => {
  it("offers Stop only while the task's last run is running", () => {
    const { host } = tasksList([task({ last_run_state: "running" })]);
    // Two: the desktop table row and the phone card. One means a layout was
    // converted and the other missed — the specific mistake this change is most
    // likely to make, and the reason the icon suite counts the same way.
    expect(byName(host, "Stop run").length).toBe(2);
  });

  it("does not offer Stop for any non-running state", () => {
    for (const state of ["success", "failed", "cancelled", "pending", null]) {
      const { host } = tasksList([task({ last_run_state: state })]);
      expect(
        byName(host, "Stop run").length,
        `Stop must not be offered for last_run_state=${state}`,
      ).toBe(0);
    }
  });

  it("keeps Run now available and separate from Stop", () => {
    // Fusing them into one toggle would make a mis-tap start a transfer the
    // operator meant to end.
    const { host } = tasksList([task({ last_run_state: "running" })]);
    expect(byName(host, "Run now").length).toBe(2);
    for (const stop of byName(host, "Stop run")) {
      expect(stop.getAttribute("aria-label")).not.toMatch(/run now/i);
    }
  });

  it("asks for confirmation and does nothing when the operator declines", async () => {
    const cancelRun = vi.spyOn(api, "cancelRun").mockResolvedValue({
      cancelled: true,
      state: "cancelled",
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const { host } = tasksList([task({ last_run_state: "running" })]);

    await act(async () => {
      byName(host, "Stop run")[0].click();
    });
    await settle();

    expect(confirm).toHaveBeenCalled();
    expect(cancelRun).not.toHaveBeenCalled();
  });

  it("cancels the task's CURRENT run id once confirmed", async () => {
    const cancelRun = vi.spyOn(api, "cancelRun").mockResolvedValue({
      cancelled: true,
      state: "cancelled",
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const { host } = tasksList([
      task({ last_run_state: "running", last_run_id: 99 }),
    ]);

    await act(async () => {
      byName(host, "Stop run")[0].click();
    });
    await settle();

    expect(cancelRun).toHaveBeenCalledWith(99);
  });

  it("refreshes the task and run queries so the row leaves 'running'", async () => {
    vi.spyOn(api, "cancelRun").mockResolvedValue({
      cancelled: true,
      state: "cancelled",
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const { host, qc } = tasksList([task({ last_run_state: "running" })]);
    const invalidate = vi.spyOn(qc, "invalidateQueries");

    await act(async () => {
      byName(host, "Stop run")[0].click();
    });
    await settle();

    const keys = invalidate.mock.calls.map((c) => String(c[0]?.queryKey?.[0]));
    // Without the runs invalidation an open run view keeps showing a run the
    // operator just stopped.
    expect(keys).toContain("tasks");
    expect(keys).toContain("runs");
  });

  it("surfaces the API's own refusal rather than a generic failure", async () => {
    // The distinction the operator needs: "already finished (success)" means the
    // transfer completed on its own, not that stopping went wrong.
    const detail = "409 run 42 already finished (success)";
    vi.spyOn(api, "cancelRun").mockRejectedValue(new Error(detail));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const alerted = vi.spyOn(window, "alert").mockImplementation(() => {});
    const { host } = tasksList([task({ last_run_state: "running" })]);

    await act(async () => {
      byName(host, "Stop run")[0].click();
    });
    await settle();

    expect(alerted).toHaveBeenCalled();
    expect(String(alerted.mock.calls[0][0])).toContain(detail);
  });

  it("tells the operator when the stop did not actually stop the run", async () => {
    // The 504 path. "already finished" and "has not stopped" are opposite
    // facts about the operator's data, and collapsing them into a generic
    // failure would let someone walk away from a transfer still copying bytes.
    const detail =
      "504 stop requested, but run 42 has not stopped (still running)";
    vi.spyOn(api, "cancelRun").mockRejectedValue(new Error(detail));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const alerted = vi.spyOn(window, "alert").mockImplementation(() => {});
    const { host } = tasksList([task({ last_run_state: "running" })]);

    await act(async () => {
      byName(host, "Stop run")[0].click();
    });
    await settle();

    expect(alerted).toHaveBeenCalled();
    const said = String(alerted.mock.calls[0][0]);
    expect(said).toContain("has not stopped");
    expect(said).not.toContain("already finished");
  });

  it("still shows Stop after a refused stop, so the operator can retry", async () => {
    vi.spyOn(api, "cancelRun").mockRejectedValue(new Error("boom"));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.spyOn(window, "alert").mockImplementation(() => {});
    const { host } = tasksList([task({ last_run_state: "running" })]);

    await act(async () => {
      byName(host, "Stop run")[0].click();
    });
    await settle();

    expect(byName(host, "Stop run").length).toBe(2);
  });
});

describe("starting a run now", () => {
  it("surfaces the API's 409 refusal with its source scope", async () => {
    const detail = "409 a run is already pending or running for this source";
    vi.spyOn(api, "runTask").mockRejectedValue(new Error(detail));
    const alerted = vi.spyOn(window, "alert").mockImplementation(() => {});
    const { host } = tasksList([task({ last_run_state: "running" })]);

    await act(async () => {
      byName(host, "Run now")[0].click();
    });
    await settle();

    expect(alerted).toHaveBeenCalledTimes(1);
    expect(String(alerted.mock.calls[0][0])).toContain(detail);
  });

  it("does not show an error when the run request succeeds", async () => {
    vi.spyOn(api, "runTask").mockResolvedValue({ queued: true });
    const alerted = vi.spyOn(window, "alert").mockImplementation(() => {});
    const { host } = tasksList([task()]);

    await act(async () => {
      byName(host, "Run now")[0].click();
    });
    await settle();

    expect(alerted).not.toHaveBeenCalled();
  });

  it("does not show a late refusal after the operator navigates away", async () => {
    let rejectRun;
    vi.spyOn(api, "runTask").mockImplementation(() => new Promise((_, reject) => {
      rejectRun = reject;
    }));
    const alerted = vi.spyOn(window, "alert").mockImplementation(() => {});
    const { host } = tasksList([task()]);

    await act(async () => {
      byName(host, "Run now")[0].click();
    });
    const root = roots.pop();
    const mountedHost = hosts.pop();
    act(() => root.unmount());
    mountedHost.remove();

    rejectRun(new Error("409 a run is already pending or running for this task"));
    await settle();

    expect(alerted).not.toHaveBeenCalled();
  });
});

describe("toggling a task while it runs", () => {
  it("sends only the enabled change and never touches the run", async () => {
    // The backend permits this now; the UI must not accompany it with a cancel.
    const updateTask = vi.spyOn(api, "updateTask").mockResolvedValue(task());
    const cancelRun = vi.spyOn(api, "cancelRun").mockResolvedValue({});
    const running = task({ last_run_state: "running" });
    const { host } = tasksList([running]);

    await act(async () => {
      enabledToggle(host).click();
    });
    await settle();

    expect(cancelRun).not.toHaveBeenCalled();
    expect(updateTask).toHaveBeenCalledWith(7, { ...running, enabled: false });
  });

  it("surfaces a refused toggle instead of silently reverting", async () => {
    const detail = "409 these fields cannot be changed until it finishes: delete";
    vi.spyOn(api, "updateTask").mockRejectedValue(new Error(detail));
    const alerted = vi.spyOn(window, "alert").mockImplementation(() => {});
    const { host } = tasksList([task({ last_run_state: "running" })]);

    await act(async () => {
      enabledToggle(host).click();
    });
    await settle();

    expect(alerted).toHaveBeenCalled();
    expect(String(alerted.mock.calls[0][0])).toContain(detail);
  });
});

describe("the stop control's layout contract", () => {
  // jsdom applies no CSS, so responsive layout is a class contract asserted
  // against source — the same instrument the existing mobileLayout suite uses.
  it("gives the phone card a 44px tap target for Stop", () => {
    const src = read("pages/TasksList.tsx");
    const mobile = src.slice(src.indexOf("sm:hidden space-y-3"));
    const stop = /aria-label="Stop run"[\s\S]{0,400}?\/>/.exec(mobile)?.[0] ?? "";
    expect(stop, "Stop must be present in the phone card").not.toBe("");
    expect(stop).toMatch(/min-h-\[44px\]/);
    expect(stop).toMatch(/min-w-\[44px\]/);
  });

  it("draws Stop in the danger colour on hover, like delete", () => {
    const src = read("pages/TasksList.tsx");
    for (const m of src.matchAll(/<button [\s\S]*?aria-label="Stop run"[\s\S]*?<\/button>/g)) {
      expect(m[0], `Stop must ask for the danger colour: ${m[0]}`)
        .toMatch(/hover:text-danger/);
    }
  });
});

describe("the cancel client", () => {
  it("POSTs to the run's cancel endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ cancelled: true, state: "cancelled" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await api.cancelRun(42);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/runs/42/cancel");
    expect(init.method).toBe("POST");
  });

  it("propagates the server's refusal text to the caller", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("run 42 already finished (success)", { status: 409 }),
    );

    await expect(api.cancelRun(42)).rejects.toThrow(/already finished/);
  });

  it("surfaces a stop that did not stop the run as its own answer", async () => {
    // The backend answers 504 when it signalled the run but the run is still
    // active. That is neither success nor "already finished": the transfer is
    // still copying bytes, and an operator told otherwise would walk away.
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        "stop requested, but run 42 has not stopped (still running)",
        { status: 504 },
      ),
    );

    await expect(api.cancelRun(42)).rejects.toThrow(/has not stopped/);
    await expect(api.cancelRun(42)).rejects.not.toThrow(/already finished/);
  });
});
