// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole
import { IconBase, type IconProps } from "./IconBase";

/** Back. A left-pointing arrow. Replaces ←. */
export function ArrowLeftIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M19 12 H5" />
      <path d="M11 6 L5 12 L11 18" />
    </IconBase>
  );
}
