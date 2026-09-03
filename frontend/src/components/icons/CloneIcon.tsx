// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole
import { IconBase, type IconProps } from "./IconBase";

/** Clone. Two offset sheets — the copy metaphor. Replaces ⧉. */
export function CloneIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      {/* Sheet behind, drawn as the two exposed edges only. */}
      <path d="M15.5 6.5 V5 a2 2 0 0 0-2-2 H5 a2 2 0 0 0-2 2 v8.5 a2 2 0 0 0 2 2 h1.5" />
      {/* Sheet in front. */}
      <rect x="8.5" y="8.5" width="12.5" height="12.5" rx="2" />
    </IconBase>
  );
}
