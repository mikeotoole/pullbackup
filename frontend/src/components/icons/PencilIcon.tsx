// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole
import { IconBase, type IconProps } from "./IconBase";

/** Edit. A pencil on the 45-degree diagonal, tip at bottom-left. Replaces ✎. */
export function PencilIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      {/* Body + tip: shaft down to the nib, then the ink point at 3,21. */}
      <path d="M16.6 4.4 L19.6 7.4 L7.6 19.4 L3.5 20.5 L4.6 16.4 Z" />
      {/* Ferrule: the band where the metal meets the wood. */}
      <path d="M14.4 6.6 L17.4 9.6" />
    </IconBase>
  );
}
