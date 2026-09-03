// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole
import { IconBase, type IconProps } from "./IconBase";

/**
 * Warning. Triangle with bang. Replaces ⚠.
 *
 * This is the one icon that sits inline with a sentence rather than inside a
 * button, so the caller aligns it with the text baseline instead of centring
 * it in a 44px target.
 */
export function WarningIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      {/* Rounded triangle: same corner treatment as the rest of the set. */}
      <path d="M12 3.6 a1.7 1.7 0 0 1 1.47 0.85 l7.3 12.9 A1.7 1.7 0 0 1 19.3 20 H4.7 a1.7 1.7 0 0 1-1.47-2.65 l7.3-12.9 A1.7 1.7 0 0 1 12 3.6 Z" />
      <path d="M12 9.4 V13.6" />
      {/* Dot as a zero-length round-capped stroke, so it inherits colour and
          weight with the rest rather than needing a filled circle. */}
      <path d="M12 16.7 h0.01" />
    </IconBase>
  );
}
