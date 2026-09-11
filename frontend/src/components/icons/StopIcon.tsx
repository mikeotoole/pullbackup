// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole
import { IconBase, type IconProps } from "./IconBase";

/**
 * Stop the run that is currently executing.
 *
 * A square, the universal stop glyph, drawn on the same grid as PlayIcon so the
 * two read as a pair when they sit in the same action row. Rounded corners come
 * from the shared stroke-linejoin rather than an rx, keeping it consistent with
 * the rest of the set.
 *
 * Deliberately NOT an X: an X next to a delete icon reads as "remove", and this
 * control ends a transfer rather than deleting anything.
 */
export function StopIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M7.2 7.2 h9.6 v9.6 h-9.6 Z" />
    </IconBase>
  );
}
