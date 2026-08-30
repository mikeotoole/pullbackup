import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

// Guards for the browser half of auth tranche 1.
//
// These assert against source, matching the convention already used by
// uiCasing and mobileLayout in this repo: the properties at risk are
// structural contracts (a route exists, a 401 redirects, the form stacks on a
// phone) and those are exactly what silently regresses later.

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "..");
const read = (p) => readFileSync(resolve(SRC, p), "utf8");

describe("login page", () => {
  it("registers a /login route that renders the login page", () => {
    const app = read("App.tsx");
    // Both halves matter: a route that renders nothing, or a Login import with
    // no route, each leave the page unreachable.
    expect(app).toMatch(/import\s*\{\s*Login\s*\}\s*from\s*"\.\/pages\/Login"/);
    expect(app).toMatch(/<Route\s+path="\/login"\s+element=\{<Login\s*\/>\}\s*\/>/);
  });

  it("uses lower-case chrome on the sign-in form", () => {
    // Same rule the rest of the UI follows: chrome is lower case.
    const src = read("pages/Login.tsx");
    for (const m of src.matchAll(/(?:label|title|placeholder)="([^"]+)"/g)) {
      const text = m[1];
      const titleCase = [...text.matchAll(/\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b/g)];
      expect(titleCase.map((t) => t[0]), `Title Case chrome: "${text}"`).toEqual([]);
    }
    // The visible call to action, specifically.
    expect(src).toMatch(/sign in/);
    expect(src).not.toMatch(/Sign In/);
  });

  it("submits the form through the login api", () => {
    const src = read("pages/Login.tsx");
    expect(src).toMatch(/api\.login\(/);
    expect(src).toMatch(/onSubmit=/);
  });

  it("offers a visible way to sign out that actually calls logout", () => {
    const app = read("App.tsx");
    // The label must be on a real control, and that control must reach the
    // logout endpoint — a label alone is decoration.
    expect(app).toMatch(/<button[\s\S]{0,400}?sign out[\s\S]{0,200}?<\/button>/);
    expect(app).toMatch(/api\.logout\(\)/);
  });

  it("renders the login form without a horizontal scroller at 375px", () => {
    const src = read("pages/Login.tsx");
    // A fixed width wider than a phone viewport is the way this regresses.
    const widths = src.match(/\bw-\[[^\]]+\]|\bmin-w-\[[^\]]+\]/g) ?? [];
    for (const w of widths) {
      const px = /\[(\d+)px\]/.exec(w);
      expect(
        !px || Number(px[1]) <= 343,
        `login form must fit a 375px viewport (343px after page padding): ${w}`,
      ).toBe(true);
    }
    // The card must be allowed to shrink rather than pinned.
    expect(src).toMatch(/max-w-|w-full/);
    expect(src).not.toMatch(/overflow-x-auto/);
  });

  it("does not surface the password in the DOM as plain text", () => {
    const src = read("pages/Login.tsx");
    expect(src).toMatch(/type="password"/);
  });

  it("renders a failed sign-in rather than swallowing it", () => {
    const src = read("pages/Login.tsx");
    // The state must exist, be populated on failure, AND be rendered. Holding
    // the message in state without ever showing it leaves a dead form.
    expect(src).toMatch(/setError\(""\)/);
    expect(src).toMatch(/catch[\s\S]{0,200}?setError\(\s*err/);
    expect(src).toMatch(/\{\s*error\s*&&/);
    expect(src).toMatch(/\{\s*error\s*\}/);
  });
});
