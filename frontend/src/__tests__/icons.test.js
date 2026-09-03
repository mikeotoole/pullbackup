import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import { TasksList } from "../pages/TasksList";
import { Sources } from "../pages/Sources";
import { RunHistory } from "../pages/RunHistory";
import { RunDetail } from "../pages/RunDetail";

// The icon controls, asserted on RENDERED output.
//
// The emoji-to-SVG swap is the kind of change that quietly breaks assistive
// technology: delete the glyph, forget the aria-label, and a screen reader now
// announces an empty button. So these tests never look for a picture. They ask
// the same question a screen reader asks — "what controls are here, and what is
// each one called?" — and additionally require the icon itself to be silent, so
// the control's name is announced once rather than twice.
//
// No jsdom / testing-library here: this repo's suite is static-markup based and
// nine icons do not justify pulling a DOM implementation into devDependencies.
// `renderToStaticMarkup` plus attribute parsing gets at the same properties
// (accessible name, aria-hidden, tap-target class) because they are all serialised
// into the markup. What it cannot see is anything computed by CSS or by the
// browser's accessibility tree; the CSS-driven properties are covered by the
// hover-colour assertion below, which checks the class contract instead.

function render(node, seed = () => {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, enabled: false } },
  });
  seed(qc);
  return renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client: qc },
      createElement(MemoryRouter, null, node),
    ),
  );
}

const TASK = {
  id: 7,
  name: "photos",
  task_type: "rsync",
  source_id: 1,
  remote_path: "/srv/photos",
  local_path: "/data/photos",
  cron: "0 3 * * *",
  enabled: true,
  next_run: null,
  last_run_at: null,
  last_run_id: 42,
  last_run_state: "success",
  kuma_monitor_id: 9,
};

const SOURCE = {
  id: 1,
  name: "tang",
  user: "root",
  host: "tang.lan",
  port: 22,
  ssh_key_path: "/data/ssh/id_ed25519",
  task_count: 0,
  description: "",
};

const RUN = {
  id: 42,
  task_id: 7,
  state: "success",
  started_at: "2026-09-02T03:00:00Z",
  finished_at: "2026-09-02T03:04:00Z",
  exit_code: 0,
  files_transferred: 12,
  bytes_transferred: 4096,
  error_message: null,
};

const tasksList = () =>
  render(createElement(TasksList), (qc) => {
    qc.setQueryData(["tasks"], [TASK]);
    qc.setQueryData(["sources"], [SOURCE]);
    qc.setQueryData(["sysinfo"], { kuma_url: "https://kuma.example" });
  });

const sourcesPage = () =>
  render(createElement(Sources), (qc) => {
    qc.setQueryData(["sources"], [SOURCE]);
    qc.setQueryData(["pubkey"], { public_key: "ssh-ed25519 AAAA" });
  });

const runHistory = () =>
  render(createElement(RunHistory), (qc) => {
    qc.setQueryData(["runs", NaN], [RUN]);
    qc.setQueryData(["tasks"], [TASK]);
  });

/**
 * Every interactive element with an accessible name, as
 * {tag, name, className, html}. `tag` stands in for the ARIA role: <button> is
 * role=button, <a href> is role=link.
 */
function controls(html) {
  const out = [];
  for (const m of html.matchAll(/<(button|a)\b([^>]*)>([\s\S]*?)<\/\1>/g)) {
    const [full, tag, attrs, inner] = m;
    const name =
      /aria-label="([^"]*)"/.exec(attrs)?.[1] ??
      inner.replace(/<[^>]*>/g, "").trim();
    out.push({
      tag,
      role: tag === "button" ? "button" : "link",
      name,
      className: /class="([^"]*)"/.exec(attrs)?.[1] ?? "",
      html: full,
    });
  }
  return out;
}

/** All controls carrying this accessible name (one per layout, so expect 2). */
const byName = (html, name) =>
  controls(html).filter((c) => c.name === name);

describe("icon controls keep their accessible names", () => {
  it("exposes every task action by role and name, in BOTH layouts", () => {
    const html = tasksList();
    for (const [name, role] of [
      ["Run history", "link"],
      ["Edit task", "link"],
      ["Clone task", "link"],
      ["Uptime Kuma monitor", "link"],
      ["Run now", "button"],
      ["Delete task", "button"],
    ]) {
      const found = byName(html, name);
      // Two: the desktop table row and the phone card. One means a layout was
      // converted and the other was missed - the specific mistake this change
      // was most likely to make.
      expect(found.length, `expected 2 controls named "${name}", got ${found.length}`).toBe(2);
      for (const c of found) {
        expect(c.role, `"${name}" should be a ${role}`).toBe(role);
      }
    }
  });

  it("exposes both source actions by role and name, in BOTH layouts", () => {
    const html = sourcesPage();
    for (const name of ["Edit source", "Delete source"]) {
      const found = byName(html, name);
      expect(found.length, `expected 2 controls named "${name}", got ${found.length}`).toBe(2);
      for (const c of found) expect(c.role).toBe("button");
    }
  });

  it("names the back link by its text, not by an arrow glyph", () => {
    for (const html of [runHistory(), render(createElement(RunDetail))]) {
      const back = controls(html).filter((c) => c.name === "tasks");
      expect(back.length, "back link must be present and named \"tasks\"").toBeGreaterThan(0);
      for (const c of back) expect(c.role).toBe("link");
    }
  });

  it("names the view-log link 'log' in both run-history layouts", () => {
    const found = byName(runHistory(), "log");
    expect(found.length, `expected 2 "log" links, got ${found.length}`).toBe(2);
    for (const c of found) expect(c.role).toBe("link");
  });

  it("hides the icon itself from assistive technology", () => {
    // Otherwise the control is announced twice, or the SVG becomes a tab stop.
    for (const html of [tasksList(), sourcesPage(), runHistory()]) {
      const svgs = [...html.matchAll(/<svg\b[^>]*>/g)].map((m) => m[0]);
      expect(svgs.length, "expected rendered svg icons").toBeGreaterThan(0);
      for (const svg of svgs) {
        expect(svg, `svg must be aria-hidden: ${svg}`).toMatch(/aria-hidden="true"/);
        expect(svg, `svg must not be focusable: ${svg}`).toMatch(/focusable="false"/);
      }
    }
  });

  it("draws every icon in currentColor so hover and danger states apply", () => {
    // This is the whole point of the change. `hover:text-danger` sat on the
    // delete button doing nothing, because a colour emoji ignores the cascade.
    for (const html of [tasksList(), sourcesPage(), runHistory()]) {
      for (const svg of [...html.matchAll(/<svg\b[^>]*>/g)].map((m) => m[0])) {
        expect(svg, `svg must stroke in currentColor: ${svg}`).toMatch(/stroke="currentColor"/);
        expect(svg, `svg must not be filled: ${svg}`).toMatch(/fill="none"/);
      }
    }
    // And the delete controls must actually ask for the danger colour.
    for (const c of byName(tasksList(), "Delete task")) {
      expect(c.className).toMatch(/hover:text-danger/);
    }
    for (const c of byName(sourcesPage(), "Delete source")) {
      expect(c.className).toMatch(/hover:text-danger/);
    }
  });

  it("keeps 44px tap targets on the phone layouts", () => {
    // One control per action is the mobile one; it must still be tappable.
    for (const [html, names] of [
      [tasksList(), ["Run history", "Edit task", "Clone task", "Run now", "Delete task"]],
      [sourcesPage(), ["Edit source", "Delete source"]],
    ]) {
      for (const name of names) {
        const big = byName(html, name).filter((c) =>
          /min-h-\[44px\]/.test(c.className) && /min-w-\[44px\]/.test(c.className),
        );
        expect(big.length, `"${name}" must have a 44px target in the phone layout`).toBe(1);
      }
    }
  });

  it("renders an icon inside each converted control", () => {
    // Guards the empty-button failure mode: a control whose glyph was removed
    // and whose replacement was never added still passes a name assertion.
    const html = tasksList();
    for (const name of ["Run history", "Edit task", "Clone task", "Run now", "Delete task"]) {
      for (const c of byName(html, name)) {
        expect(c.html, `"${name}" renders no icon`).toMatch(/<svg\b/);
      }
    }
  });
});
