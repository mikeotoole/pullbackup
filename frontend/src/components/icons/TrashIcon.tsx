// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole
import { IconBase, type IconProps } from "./IconBase";

/** Delete. Lidded bin with two ribs. Replaces 🗑. */
export function TrashIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      {/* Lid, drawn full width so the icon reads at 16px. */}
      <path d="M4 7 H20" />
      {/* Handle. */}
      <path d="M9.5 7 V5.5 A1.5 1.5 0 0 1 11 4 h2 A1.5 1.5 0 0 1 14.5 5.5 V7" />
      {/* Body, tapering slightly like a real bin. */}
      <path d="M6.5 7 L7.4 19.2 A2 2 0 0 0 9.4 21 h5.2 a2 2 0 0 0 2-1.8 L17.5 7" />
      {/* Ribs. */}
      <path d="M10.4 10.5 V17.5" />
      <path d="M13.6 10.5 V17.5" />
    </IconBase>
  );
}
