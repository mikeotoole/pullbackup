// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole
import { IconBase, type IconProps } from "./IconBase";

/**
 * External link / open. Arrow leaving an open-cornered box, the conventional
 * "opens elsewhere" mark. Replaces ↗.
 */
export function ExternalLinkIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      {/* Box, open at the top-right where the arrow exits. */}
      <path d="M14 5 H6 A2 2 0 0 0 4 7 v11 a2 2 0 0 0 2 2 h11 a2 2 0 0 0 2-2 v-8" />
      {/* Arrow: shaft on the same 45-degree diagonal as the pencil. */}
      <path d="M11 13 L20 4" />
      <path d="M14 4 H20 V10" />
    </IconBase>
  );
}
