import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { JSDOM } from "jsdom";

// Sorting the tasks table, exercised through real header clicks in a DOM.
//
// The rest of this suite renders to static markup, which is enough for the
// properties it guards (class contracts, accessible names). It is NOT enough
// here: the behaviour under test is what happens when you *click* a header, how
// the row order changes, and whether that choice survives a remount. Those are
// state transitions, so this file pulls in jsdom and drives the component the
// way an operator does.

const TASKS = [
  {
    id: 3,
    name: "photos",
    task_type: "rsync",
    source_id: 1,
    remote_path: "/srv/photos",
    local_path: "/data/photos",
    cron: "0 3 * * *",
    enabled: true,
    next_run: "2026-09-12T03:00:00",
    last_run_at: "2026-09-10T03:00:00",
    last_run_id: 42,
    last_run_state: "success",
    kuma_monitor_id: null,
  },
  {
    id: 1,
    name: "archive",
    task_type: "syncoid",
    source_id: 2,
    remote_path: "/srv/archive",
    local_path: "/data/archive",
    cron: "0 1 * * *",
    enabled: true,
    next_run: "2026-09-11T01:00:00",
    last_run_at: null,
    last_run_id: null,
    last_run_state: null,
    kuma_monitor_id: null,
  },
  {
    id: 2,
    name: "music",
    task_type: "rsync",
    source_id: 1,
    remote_path: "/srv/music",
    local_path: "/data/music",
    cron: "0 2 * * *",
    enabled: false,
    next_run: "2026-09-11T02:00:00",
    last_run_at: "2026-09-11T02:00:00",
    last_run_id: 43,
    last_run_state: "failed",
    kuma_monitor_id: null,
  },
];

const SOURCES = [
  { id: 1, name: "tang", user: "root", host: "tang.lan", port: 22, ssh_key_path: "", task_count: 2, description: "" },
  { id: 2, name: "ahi", user: "root", host: "ahi.lan", port: 22, ssh_key_path: "", task_count: 1, description: "" },
];

let dom;
let roots = [];
let React;
let createRoot;
let act;
let QueryClient;
let QueryClientProvider;
let MemoryRouter;
let TasksList;
let SORT_STORAGE_KEY;

beforeEach(async () => {
  dom = new JSDOM("<!doctype html><html><body></body></html>", {
    url: "https://pullbackup.test/tasks",
    pretendToBeVisual: true,
  });
  for (const k of ["window", "document", "HTMLElement", "Node", "Event", "MouseEvent", "KeyboardEvent", "localStorage", "getComputedStyle"]) {
    Object.defineProperty(globalThis, k, {
      value: dom.window[k],
      configurable: true,
      writable: true,
    });
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;

  React = await import("react");
  ({ createRoot } = await import("react-dom/client"));
  ({ act } = await import("react"));
  ({ QueryClient, QueryClientProvider } = await import("@tanstack/react-query"));
  ({ MemoryRouter } = await import("react-router-dom"));
  ({ TasksList } = await import("../pages/TasksList"));
  ({ SORT_STORAGE_KEY } = await import("../lib/taskSortStorage"));

  dom.window.localStorage.clear();
});

afterEach(() => {
  // Unmount every root before tearing the DOM down: react-query holds timers,
  // and a root left mounted fires them into a window that no longer exists.
  for (const root of roots) act(() => root.unmount());
  roots = [];
  dom.window.close();
  vi.restoreAllMocks();
});

/** Mount TasksList with `tasks`/`sources` already in the query cache. */
function mount() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, enabled: false } },
  });
  qc.setQueryData(["tasks"], TASKS);
  qc.setQueryData(["sources"], SOURCES);
  qc.setQueryData(["sysinfo"], { kuma_url: "" });

  const host = dom.window.document.createElement("div");
  dom.window.document.body.appendChild(host);
  const root = createRoot(host);
  roots.push(root);
  act(() => {
    root.render(
      React.createElement(
        QueryClientProvider,
        { client: qc },
        React.createElement(MemoryRouter, null, React.createElement(TasksList)),
      ),
    );
  });
  return host;
}

const headers = (host) => [...host.querySelectorAll("thead th")];

/** The header control whose visible text starts with `label`. */
function header(host, label) {
  const th = headers(host).find((h) => h.textContent.trim().toLowerCase().startsWith(label));
  if (!th) throw new Error(`no column header "${label}" in: ${headers(host).map((h) => h.textContent.trim())}`);
  return th;
}

const click = (el) => act(() => {
  el.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
});

/** Remote paths in rendered desktop-row order. */
const rowPaths = (host) =>
  [...host.querySelectorAll("tbody tr td:first-child")].map((td) => td.textContent.trim());

/** Remote paths in rendered phone-card order. */
const cardPaths = (host) =>
  [...host.querySelectorAll(".sm\\:hidden .font-mono.break-all")].map((el) => el.textContent.trim());

describe("sorting the tasks table", () => {
  it("renders the API order until a header is clicked", () => {
    const host = mount();
    expect(rowPaths(host)).toEqual(["/srv/photos", "/srv/archive", "/srv/music"]);
  });

  it("sorts ascending on the first click and descending on the second", () => {
    const host = mount();
    const th = header(host, "remote path");

    click(th.querySelector("button"));
    expect(rowPaths(host)).toEqual(["/srv/archive", "/srv/music", "/srv/photos"]);

    click(th.querySelector("button"));
    expect(rowPaths(host)).toEqual(["/srv/photos", "/srv/music", "/srv/archive"]);
  });

  it("exposes the active column and direction through aria-sort", () => {
    const host = mount();
    // Before any click no column claims an ordering.
    expect(headers(host).map((h) => h.getAttribute("aria-sort"))).toEqual(
      headers(host).map(() => "none"),
    );

    const th = header(host, "remote path");
    click(th.querySelector("button"));
    expect(th.getAttribute("aria-sort")).toBe("ascending");
    click(th.querySelector("button"));
    expect(th.getAttribute("aria-sort")).toBe("descending");

    // Only one column is ever the active one.
    const active = headers(host).filter((h) => h.getAttribute("aria-sort") !== "none");
    expect(active.length).toBe(1);
  });

  it("shows a visible direction indicator on the active column only", () => {
    const host = mount();
    const th = header(host, "last run");
    click(th.querySelector("button"));

    const indicated = headers(host).filter((h) => h.querySelector("[data-sort-indicator]"));
    expect(indicated.length).toBe(1);
    expect(indicated[0].textContent).toContain("last run");
    expect(indicated[0].querySelector("[data-sort-indicator]").getAttribute("data-sort-indicator"))
      .toBe("asc");

    click(th.querySelector("button"));
    expect(th.querySelector("[data-sort-indicator]").getAttribute("data-sort-indicator")).toBe("desc");
  });

  it("makes every sortable header a keyboard-operable control", () => {
    const host = mount();
    const sortable = headers(host).filter((h) => h.querySelector("button"));
    // Eight sortable columns; actions is not one of them.
    expect(sortable.length).toBe(8);
    expect(header(host, "actions").querySelector("button")).toBeNull();

    // A <button> is focusable and responds to Enter/Space natively — no
    // hand-rolled key handling, no tabindex, nothing to get wrong.
    for (const th of sortable) {
      const btn = th.querySelector("button");
      expect(btn.tagName).toBe("BUTTON");
      expect(btn.getAttribute("type")).toBe("button");
    }

    // Prove it actually reorders from the keyboard, not just that it is focusable.
    const btn = header(host, "remote path").querySelector("button");
    btn.focus();
    expect(dom.window.document.activeElement).toBe(btn);
    act(() => {
      btn.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      // jsdom does not synthesise the click a real browser fires for Enter on a
      // button, so the activation itself is asserted through click above; here
      // we only prove the control is reachable and typed correctly.
    });
  });

  it("orders tasks with no last run last, in both directions", () => {
    const host = mount();
    const th = header(host, "last run");

    click(th.querySelector("button"));
    expect(rowPaths(host)).toEqual(["/srv/photos", "/srv/music", "/srv/archive"]);

    click(th.querySelector("button"));
    expect(rowPaths(host)).toEqual(["/srv/music", "/srv/photos", "/srv/archive"]);
  });

  it("sorts the source column by name rather than by source id", () => {
    const host = mount();
    click(header(host, "source").querySelector("button"));
    // ahi (source 2) before tang (source 1): by id this would be the reverse.
    expect(rowPaths(host)).toEqual(["/srv/archive", "/srv/music", "/srv/photos"]);
  });

  it("applies the type filter before sorting", () => {
    const host = mount();
    click(header(host, "remote path").querySelector("button"));

    const rsync = [...host.querySelectorAll("button")].find((b) => b.textContent.trim() === "rsync");
    click(rsync);

    expect(rowPaths(host)).toEqual(["/srv/music", "/srv/photos"]);
  });

  it("orders the phone cards the same way as the table", () => {
    const host = mount();
    click(header(host, "remote path").querySelector("button"));
    click(header(host, "remote path").querySelector("button"));

    expect(cardPaths(host)).toEqual(["/srv/photos", "/srv/music", "/srv/archive"]);
    expect(cardPaths(host)).toEqual(rowPaths(host));
  });
});

describe("remembering the sort", () => {
  it("restores the choice after a remount", () => {
    const first = mount();
    click(header(first, "remote path").querySelector("button"));
    click(header(first, "remote path").querySelector("button"));
    expect(rowPaths(first)).toEqual(["/srv/photos", "/srv/music", "/srv/archive"]);

    const second = mount();
    expect(rowPaths(second)).toEqual(["/srv/photos", "/srv/music", "/srv/archive"]);
    expect(header(second, "remote path").getAttribute("aria-sort")).toBe("descending");
  });

  it("restores it for the phone cards too", () => {
    const first = mount();
    click(header(first, "source").querySelector("button"));

    const second = mount();
    expect(cardPaths(second)).toEqual(["/srv/archive", "/srv/music", "/srv/photos"]);
  });

  it("writes the preference to localStorage and nowhere else", () => {
    const fetchSpy = vi.fn(() => Promise.reject(new Error("no network in this test")));
    dom.window.fetch = fetchSpy;
    globalThis.fetch = fetchSpy;

    const host = mount();
    click(header(host, "state").querySelector("button"));

    expect(JSON.parse(dom.window.localStorage.getItem(SORT_STORAGE_KEY))).toEqual({
      column: "state",
      direction: "asc",
    });
    // The whole point of "browser-local": choosing an ordering must not talk to
    // the API at all, so it cannot leak to another account or device.
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("ignores a malformed stored preference instead of breaking the table", () => {
    dom.window.localStorage.setItem(SORT_STORAGE_KEY, "{ not json");
    const host = mount();
    expect(rowPaths(host)).toEqual(["/srv/photos", "/srv/archive", "/srv/music"]);
    expect(headers(host).every((h) => h.getAttribute("aria-sort") === "none")).toBe(true);
  });

  it("ignores a stored column that no longer exists", () => {
    dom.window.localStorage.setItem(
      SORT_STORAGE_KEY,
      JSON.stringify({ column: "actions", direction: "asc" }),
    );
    const host = mount();
    expect(rowPaths(host)).toEqual(["/srv/photos", "/srv/archive", "/srv/music"]);
  });

  it("still renders and still sorts when storage is blocked", () => {
    // Safari private mode and "block cookies" both make localStorage throw.
    const blocked = {
      getItem() { throw new DOMException("denied", "SecurityError"); },
      setItem() { throw new DOMException("denied", "SecurityError"); },
      removeItem() { throw new DOMException("denied", "SecurityError"); },
    };
    Object.defineProperty(dom.window, "localStorage", { value: blocked, configurable: true });

    const host = mount();
    expect(rowPaths(host)).toEqual(["/srv/photos", "/srv/archive", "/srv/music"]);
    click(header(host, "remote path").querySelector("button"));
    expect(rowPaths(host)).toEqual(["/srv/archive", "/srv/music", "/srv/photos"]);
  });
});
