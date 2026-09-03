// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole
import type { SVGProps } from "react";

export type IconProps = SVGProps<SVGSVGElement>;

/**
 * Shared chrome for the icon family.
 *
 * Every icon in this directory is drawn on the same 24x24 grid with the same
 * 1.8 stroke and round caps/joins, so they read as one set rather than nine
 * unrelated pictures. Only the path data differs.
 *
 * Three properties are deliberate and load-bearing:
 *
 *  - `stroke="currentColor"` + `fill="none"`. This is the whole reason the
 *    emoji had to go: `hover:text-danger` on the delete button did nothing to
 *    a 🗑, because a colour emoji ignores the cascade. A stroked path inherits.
 *  - `width/height="1em"`. Icons default to the surrounding font size and stay
 *    aligned with adjacent text. A caller wanting a fixed box passes a Tailwind
 *    size class — a CSS rule beats a presentational attribute, so `w-4 h-4`
 *    wins without `!important`.
 *  - `aria-hidden` + `focusable="false"`. The control around the icon carries
 *    the accessible name via aria-label/title; the icon must not add a second
 *    one, and must not become a tab stop (which `focusable` prevents in IE/old
 *    Edge, where SVG defaults to focusable).
 *
 * Props spread LAST so a caller can override any of the above.
 */
export function IconBase({ children, ...props }: IconProps) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      width="1em"
      height="1em"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      {children}
    </svg>
  );
}
