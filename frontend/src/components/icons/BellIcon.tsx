// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole
import { IconBase, type IconProps } from "./IconBase";

/** Uptime Kuma monitor. A bell. Replaces 🔔. */
export function BellIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      {/* Dome and skirt: the body flares out to the rim at y=17. */}
      <path d="M18 17 V11 a6 6 0 0 0-12 0 v6 l-1.6 2 h15.2 Z" />
      {/* Crown. */}
      <path d="M12 5 V3.2" />
      {/* Clapper. */}
      <path d="M10.2 19.6 a1.9 1.9 0 0 0 3.6 0" />
    </IconBase>
  );
}
