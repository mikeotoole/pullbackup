// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole
import { IconBase, type IconProps } from "./IconBase";

/**
 * Run history. A clock with a counter-clockwise arrow — "history", not "now".
 * Replaces 🕘.
 */
export function HistoryIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      {/* Dial, opened at the top-left so the rewind arrow can enter it. */}
      <path d="M3.6 9.6 A9 9 0 1 1 3 12" />
      {/* Rewind arrowhead at the opening. */}
      <path d="M3 4.5 V9.8 H8.3" />
      {/* Hands at ~9:00, matching the glyph this replaces. */}
      <path d="M12 7.4 V12 H8.4" />
    </IconBase>
  );
}
