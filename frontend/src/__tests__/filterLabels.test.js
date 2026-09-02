import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { TasksList } from "../pages/TasksList";

// The task-type filter chips must read "all", "rsync", "zfs" — lower case,
// like the rest of the UI chrome (run/status tokens are the only exception).
//
// This renders the real component rather than reading the source, because the
// defect was NOT in the label strings: the source already held lowercase
// literals ("all"/"rsync"/"zfs") and a Tailwind `capitalize` class shouted
// them as "All"/"Rsync"/"Zfs" at paint time. A source-text assertion passes
// against the broken UI, so it has to be the rendered markup plus the absence
// of a text-transform that would re-case it.

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "..");
const read = (p) => readFileSync(resolve(SRC, p), "utf8");

function renderTasksList() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, enabled: false } },
  });
  return renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client: qc },
      createElement(MemoryRouter, null, createElement(TasksList)),
    ),
  );
}

/** The three filter <button> elements, as {text, className}. */
function filterButtons(html) {
  return [...html.matchAll(/<button[^>]*class="([^"]*)"[^>]*>([^<]*)<\/button>/g)]
    .map((m) => ({ className: m[1], text: m[2].trim() }))
    .filter((b) => ["all", "rsync", "zfs"].includes(b.text.toLowerCase()));
}

describe("task type filter labels", () => {
  it("renders exactly the three filter chips", () => {
    const buttons = filterButtons(renderTasksList());
    expect(buttons.map((b) => b.text.toLowerCase())).toEqual([
      "all",
      "rsync",
      "zfs",
    ]);
  });

  it("renders the filter labels in lower case", () => {
    for (const b of filterButtons(renderTasksList())) {
      expect(b.text, `filter label should be lower case: "${b.text}"`)
        .toBe(b.text.toLowerCase());
    }
  });

  it("does not re-case the filter labels with a text-transform", () => {
    // `capitalize` / `uppercase` on the chip turns lowercase "rsync" into
    // "Rsync" on screen while the DOM text stays lowercase. That is the exact
    // bug, so the class contract is asserted alongside the text.
    for (const b of filterButtons(renderTasksList())) {
      expect(
        b.className,
        `filter chip "${b.text}" must not carry a capitalising text-transform: ${b.className}`,
      ).not.toMatch(/\b(capitalize|uppercase)\b/);
    }
  });

  it("leaves the run-state pill's uppercase transform alone", () => {
    // Status/state values are the documented exception to the lowercase rule.
    expect(read("components/StatusPill.tsx")).toMatch(/\buppercase\b/);
  });
});
