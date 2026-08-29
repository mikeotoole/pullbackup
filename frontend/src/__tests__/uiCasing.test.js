import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

// UI casing rule (Mike, 2026-08): general UI chrome is lower case; run/status
// tokens are UPPER CASE. Acronyms and proper nouns keep their real casing.
//
// The status side is already handled in CSS — StatusPill applies `uppercase` —
// so these guards police the chrome: headings, column headers, field labels,
// buttons and tooltips must not be Title Case.

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "..");
const read = (p) => readFileSync(resolve(SRC, p), "utf8");

const PAGES = [
  "App.tsx",
  "pages/TasksList.tsx",
  "pages/RunHistory.tsx",
  "pages/RunDetail.tsx",
  "pages/Sources.tsx",
  "pages/TaskForm.tsx",
];

// Words that legitimately keep a capital: acronyms, proper nouns, product and
// tool names. Deliberately NARROW — an over-broad allowlist silently disables
// the guard, which is exactly what happened on the first draft.
const PROPER = new Set([
  "SSH", "ZFS", "UTC", "KB", "MB", "GB", "TB",
  "Matrix", "Uptime", "Kuma", "Pullbackup", "Syncoid",
]);

/** Title Case = two or more consecutive Capitalised words. */
function titleCasePhrases(text) {
  const hits = [];
  for (const m of text.matchAll(/\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b/g)) {
    const [phrase, a, b] = m;
    if (PROPER.has(a) && PROPER.has(b)) continue;
    hits.push(phrase);
  }
  return hits;
}

describe("ui casing", () => {
  it("uses lower case for table column headers", () => {
    // These were "Remote Path", "Next Run", "Last Run" — Title Case chrome.
    const src = read("pages/TasksList.tsx");
    const headers = [...src.matchAll(/<th[^>]*>([^<]+)<\/th>/g)].map(m => m[1].trim());
    for (const h of headers) {
      if (!h) continue;
      const first = h.split(/\s+/)[0];
      expect(
        h === h.toLowerCase() || PROPER.has(first),
        `column header should be lower case: "${h}"`,
      ).toBe(true);
    }
  });

  it("uses lower case for page headings and nav", () => {
    for (const page of ["App.tsx", "pages/TasksList.tsx", "pages/Sources.tsx"]) {
      const src = read(page);
      for (const m of src.matchAll(/<h1[^>]*>([^<{]*)</g)) {
        const text = m[1].trim();
        if (!text) continue;
        expect(
          text === text.toLowerCase() || PROPER.has(text.split(/\s+/)[0]),
          `${page} heading should be lower case: "${text}"`,
        ).toBe(true);
      }
    }
  });

  it("has no Title Case phrases in button labels", () => {
    for (const page of PAGES) {
      const src = read(page);
      for (const m of src.matchAll(/>\+?\s*([A-Za-z][A-Za-z ]{2,30})</g)) {
        const hits = titleCasePhrases(m[1]);
        expect(hits, `${page} button/link text is Title Case: ${m[1].trim()}`)
          .toEqual([]);
      }
    }
  });

  it("has no Title Case in field labels, section titles or tooltips", () => {
    // Where most of the chrome actually lives.
    for (const page of PAGES) {
      const src = read(page);
      for (const m of src.matchAll(/(?:label|title|placeholder)="([^"]+)"/g)) {
        const hits = titleCasePhrases(m[1]);
        expect(hits, `${page} has Title Case chrome: "${m[1]}"`).toEqual([]);
      }
    }
  });

  it("starts single-word chrome in lower case", () => {
    // Single words dodge the two-word Title Case detector, so check them too.
    for (const page of PAGES) {
      const src = read(page);
      for (const m of src.matchAll(/(?:label|title)="([A-Za-z][a-z]*)"/g)) {
        const word = m[1];
        expect(
          word === word.toLowerCase() || PROPER.has(word),
          `${page} single-word chrome should be lower case: "${word}"`,
        ).toBe(true);
      }
    }
  });

  it("keeps run state tokens upper case", () => {
    // The pill is the one place that must SHOUT. It does it in CSS so the
    // underlying value stays a clean lowercase enum.
    const pill = read("components/StatusPill.tsx");
    expect(pill).toMatch(/\buppercase\b/);
  });
});
