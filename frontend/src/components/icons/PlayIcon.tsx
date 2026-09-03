// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole
import { IconBase, type IconProps } from "./IconBase";

/**
 * Run now. A play triangle. Replaces ▶.
 *
 * Stroked and closed rather than filled, so it carries the same visual weight
 * as its neighbours in the action row instead of reading as a solid blob.
 */
export function PlayIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M7.5 4.9 a1 1 0 0 1 1.52-0.85 l10.2 6.3 a1.2 1.2 0 0 1 0 2.04 l-10.2 6.3 A1 1 0 0 1 7.5 17.84 Z" />
    </IconBase>
  );
}
