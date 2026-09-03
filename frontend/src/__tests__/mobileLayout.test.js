import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

// Structural guards for the phone layout.
//
// The complaint these encode: every list was a wide <table>, so on a phone the
// only way to read a row was to scroll sideways. TasksList carried NINE columns.
//
// These assert against the real source rather than a render, because the fix is
// a responsive-class contract (hidden below `sm`, table restored at `sm:` and
// up) and that contract is exactly what silently regresses when someone later
// adds a column.

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "..");
const read = (p) => readFileSync(resolve(SRC, p), "utf8");

const LIST_PAGES = [
  "pages/TasksList.tsx",
  "pages/RunHistory.tsx",
  "pages/Sources.tsx",
];

describe("phone layout", () => {
  it("gives every list a card view that replaces the table on small screens", () => {
    for (const page of LIST_PAGES) {
      const src = read(page);
      // The table must be hidden on phones and restored from `sm:` up...
      expect(src, `${page} must hide its table below the sm breakpoint`)
        .toMatch(/hidden\s+sm:table|hidden\s+sm:block/);
      // ...and a card list must exist that does the opposite.
      expect(src, `${page} must render a card list on phones`)
        .toMatch(/sm:hidden/);
    }
  });

  it("never leaves a horizontal scroller as the phone experience", () => {
    // `overflow-x-auto` on the table wrapper is what produced the side-scroll.
    // It is fine from `sm:` up — either because the element is hidden on
    // phones, or because the scroller itself is breakpoint-scoped
    // (`sm:overflow-x-auto`). What must not exist is an unconditional
    // horizontal scroller that a phone gets.
    for (const page of LIST_PAGES) {
      const src = read(page);
      const withOverflow = /className="[^"]*\boverflow-x-auto\b[^"]*"/g;
      for (const match of src.match(withOverflow) ?? []) {
        const hiddenOnPhone = /hidden\s+sm:/.test(match);
        const scopedToDesktop = /\bsm:overflow-x-auto\b/.test(match)
          && !/(^|[\s"])overflow-x-auto\b/.test(match.replace(/\bsm:overflow-x-auto\b/g, ""));
        expect(
          hiddenOnPhone || scopedToDesktop,
          `${page} still scrolls sideways on phones: ${match}`,
        ).toBe(true);
      }
    }
  });

  it("keeps tap targets usable on the tasks row actions", () => {
    // w-7 is 28px; below Apple/Google's ~44px guidance. The card view must use
    // a larger hit area for the same actions.
    const src = read("pages/TasksList.tsx");
    expect(src).toMatch(/min-h-\[44px\]|h-11|py-2\.5|p-2\.5/);
  });

  it("lets the header wrap instead of overflowing a narrow screen", () => {
    const src = read("App.tsx");
    expect(src).toMatch(/flex-wrap|flex-col/);
    // Generous desktop padding must shrink on phones.
    expect(src).toMatch(/px-4\s+sm:px-6|px-3\s+sm:px-6/);
  });

  it("shows the fields that matter at a glance without expanding", () => {
    // Mike reads status first, then which task, then when it last ran.
    const src = read("pages/TasksList.tsx");
    const cardRegion = src.slice(src.indexOf("sm:hidden"));
    expect(cardRegion).toContain("StatusPill");
    expect(cardRegion).toContain("remote_path");
    expect(cardRegion).toMatch(/lastRunTime|last_run_at/);
  });

  it("does not force a three-column path picker onto a narrow screen", () => {
    // The rsync local-path row was grid-cols-[minmax(0,1fr)_auto_minmax(0,2fr)]:
    // a <select>, a "/" separator, and a text input side by side. At 375px that
    // squeezes the dest-root select down to a few unreadable characters.
    const src = read("pages/TaskForm.tsx");
    const threeCol = /grid-cols-\[minmax\(0,1fr\)_auto_minmax\(0,2fr\)\]/;
    for (const match of src.match(/className="[^"]*grid-cols-\[[^"]*"/g) ?? []) {
      if (threeCol.test(match)) {
        expect(match, `path picker must stack on phones: ${match}`)
          .toMatch(/sm:grid-cols-\[/);
      }
    }
  });

  it("keeps the form's save/cancel buttons reachable on a phone", () => {
    const src = read("pages/TaskForm.tsx");
    // Window must START at the header container, not at the <h1> inside it,
    // or the wrapping class on that container falls outside the slice.
    const start = src.indexOf('<div className="flex', src.indexOf("onSubmit="));
    const header = src.slice(start, src.indexOf("save.error"));
    expect(header).toMatch(/flex-wrap|flex-col/);
  });

  it("uses full-width form controls so labels and inputs line up", () => {
    // A bare <select>/<input> inside a stacked field should span the field,
    // otherwise controls sit at intrinsic width and look ragged on a phone.
    const css = read("index.css");
    expect(css).toMatch(/input,\s*select,\s*textarea\s*\{[^}]*w-full|@apply[^;]*w-full/);
  });


  it("offers the same task actions on phone as on desktop", () => {
    // Review 341 (medium): the mobile card omitted the clone link, so a phone
    // user simply could not clone a task. Comparing the two action sets
    // catches the whole class - any action added to one view and not the other.
    const src = read("pages/TasksList.tsx");

    const desktop = src.slice(src.indexOf("text-right whitespace-nowrap"), src.indexOf("</tbody>"));
    const mobile = src.slice(src.indexOf("sm:hidden space-y-3"));

    // Identify actions by their aria-label. This USED to key off the emoji
    // glyph, which was stable across both views right up until the glyphs were
    // replaced by SVG components - at which point both sets became empty and
    // the guard passed while proving nothing. An accessible name is the more
    // durable identity anyway: it is what a screen-reader user navigates by,
    // so a view missing one is a real defect whatever it renders.
    for (const label of [
      "Run history",
      "Edit task",
      "Clone task",
      "Run now",
      "Delete task",
      "Uptime Kuma monitor",
    ]) {
      const needle = `aria-label="${label}"`;
      const inDesktop = desktop.includes(needle);
      const inMobile = mobile.includes(needle);
      expect(
        inDesktop && inMobile,
        `action "${label}" is in desktop=${inDesktop} but mobile=${inMobile}; ` +
        `both views must offer the same actions`,
      ).toBe(true);
    }
  });


  it("keeps the mobile action row inside a phone viewport", () => {
    // Review 343 (medium): the action row was a non-wrapping flex row holding
    // six 44px controls (seven with Kuma) plus the enabled toggle and label.
    //
    //   6 actions: 6*44 + (44 toggle + ~52 label) + 24 card padding = 384px
    //   iPhone SE has 375 - 32 page padding = 343px  -> overflows
    //
    // Adding the clone action in the previous commit is what pushed it over,
    // so the row must be allowed to wrap rather than sized by hand.
    const src = read("pages/TasksList.tsx");
    const mobile = src.slice(src.indexOf("sm:hidden space-y-3"));

    const actionRow = mobile.slice(mobile.indexOf("border-t border-border pt-2"));
    const container = actionRow.slice(0, actionRow.indexOf("</div>", actionRow.indexOf("min-h-[44px]")));
    expect(container, "mobile action row must wrap instead of overflowing")
      .toMatch(/flex-wrap/);
  });

  it("lets the whole mobile card shrink rather than force a minimum width", () => {
    // A card that cannot shrink reintroduces the horizontal scroll this work
    // set out to remove. min-w-[44px] on tap targets is fine and expected.
    const src = read("pages/TasksList.tsx");
    const mobile = src.slice(src.indexOf("sm:hidden space-y-3"));
    const widths = mobile.match(/\bmin-w-\[[^\]]+\]|\bw-\[[^\]]+\]/g) ?? [];
    const offenders = widths.filter((w) => w !== "min-w-[44px]");
    expect(offenders, "mobile card must not pin a width").toEqual([]);
  });

});
