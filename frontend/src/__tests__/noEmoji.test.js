import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, relative, resolve } from "node:path";

// No emoji or pictographic glyphs may be used as UI affordances.
//
// Emoji render as full-colour platform glyphs: a different picture on macOS,
// Windows, Android and Linux, usually misaligned with the surrounding text,
// and — the reason this is a correctness guard rather than a taste one — they
// ignore `currentColor`. The delete control carried `hover:text-danger` for
// months and it did nothing at all, because there is no stroke on a 🗑 for the
// cascade to recolour. The replacement is our own stroke-only SVG set in
// components/icons, which does inherit.
//
// The scan strips comments first. Prose about a glyph is not a UI affordance,
// and TaskForm.tsx has a legitimate `→` inside a code comment.
//
// Deliberately NOT forbidden: —  ·  …  “  ”  ’ — typographic punctuation, not
// pictographs. Restricting the ranges rather than "all non-ASCII" is what
// keeps this guard honest instead of merely loud.

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "..");

/** Pictographic / symbol ranges. Punctuation ranges are excluded on purpose. */
const PICTOGRAPHIC = new RegExp(
  "[" +
    "\\u2190-\\u21FF" + // arrows            ←  →  ↗
    "\\u2300-\\u23FF" + // misc technical    ⌫  ⏸
    "\\u25A0-\\u25FF" + // geometric shapes  ▶  ■
    "\\u2600-\\u27BF" + // misc symbols + dingbats  ⚠  ✎  ✔
    "\\u2900-\\u29FF" + // supplemental arrows-B / misc math-B  ⧉
    "\\u2B00-\\u2BFF" + // misc symbols and arrows
    "\\uFE0F" + // variation selector-16 (emoji presentation)
    "\\u{1F300}-\\u{1FAFF}" + // emoji proper  🗑  🕘  🔔
    "]",
  "gu",
);

/** Every .ts/.tsx/.js/.jsx file under src, recursively. */
function sourceFiles(dir = SRC) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const full = resolve(dir, name);
    if (statSync(full).isDirectory()) {
      out.push(...sourceFiles(full));
    } else if (/\.(tsx?|jsx?)$/.test(name)) {
      out.push(full);
    }
  }
  return out.sort();
}

/**
 * Blank out comments, preserving line count and column positions so the
 * reported line numbers still point at the real source.
 *
 * Covers `//`, `/* *\/` and therefore JSX `{/* *\/}` too. String literals are
 * NOT parsed — a `//` inside a URL string would blank the rest of that line,
 * which can only ever hide an offender, never invent one. This test's job is
 * to fail on real offenders; a rare false negative in a string beats dragging
 * a TypeScript parser in for nine glyphs.
 */
function stripComments(text) {
  const blank = (m) => m.replace(/[^\n]/g, " ");
  return text
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/\/\/[^\n]*/g, blank);
}

function offenders(file) {
  const text = readFileSync(file, "utf8");
  const scanned = stripComments(text);
  const hits = [];
  scanned.split("\n").forEach((line, i) => {
    for (const m of line.matchAll(PICTOGRAPHIC)) {
      hits.push(
        `${relative(SRC, file)}:${i + 1} U+${m[0]
          .codePointAt(0)
          .toString(16)
          .toUpperCase()
          .padStart(4, "0")} ${m[0]}  in: ${text.split("\n")[i].trim().slice(0, 90)}`,
      );
    }
  });
  return hits;
}

describe("no emoji in the UI", () => {
  it("scans a non-trivial number of source files", () => {
    // A glob that silently matches nothing is a green test that proves
    // nothing. Pin the floor so a refactor of the tree cannot mute the guard.
    expect(sourceFiles().length).toBeGreaterThan(8);
  });

  it("uses no emoji or pictographic glyph anywhere in frontend/src", () => {
    const all = sourceFiles().flatMap(offenders);
    expect(all, `emoji used as UI affordances:\n${all.join("\n")}`).toEqual([]);
  });

  it("still allows typographic punctuation", () => {
    // Guard the guard: these must NOT be treated as pictographs, or the fix
    // would be to mangle real copy.
    for (const ch of ["\u2014", "\u00B7", "\u2026", "\u201C", "\u201D", "\u2019"]) {
      expect([...ch.matchAll(PICTOGRAPHIC)], `punctuation flagged: ${ch}`).toEqual([]);
    }
  });

  it("would still catch a glyph outside a comment", () => {
    // Guard the guard, other direction: prove stripComments does not blank
    // everything. A positive control keeps the two rules honest about each
    // other.
    const sample = 'const a = "\u{1F5D1}"; // \u{1F514} in a comment\n';
    const scanned = stripComments(sample);
    expect([...scanned.matchAll(PICTOGRAPHIC)].map((m) => m[0])).toEqual(["\u{1F5D1}"]);
  });
});
