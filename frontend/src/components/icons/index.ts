// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole
//
// The UI's icon set. Hand-drawn here rather than pulled from an icon library:
// nine glyphs do not justify a dependency, and a vendored set drags its licence
// along with it.
//
// House style, enforced by IconBase and by icons.test.js:
//   24x24 viewBox · fill none · stroke currentColor · width 1.8 · round caps
//   aria-hidden + focusable="false" (the CONTROL owns the accessible name)
//
// Adding one: build it on IconBase, keep the optical size ~18 of 24 units, and
// export it below. Do not hardcode a colour or a pixel size.
export { IconBase, type IconProps } from "./IconBase";
export { ArrowLeftIcon } from "./ArrowLeftIcon";
export { BellIcon } from "./BellIcon";
export { CloneIcon } from "./CloneIcon";
export { ExternalLinkIcon } from "./ExternalLinkIcon";
export { HistoryIcon } from "./HistoryIcon";
export { PencilIcon } from "./PencilIcon";
export { PlayIcon } from "./PlayIcon";
export { StopIcon } from "./StopIcon";
export { TrashIcon } from "./TrashIcon";
export { WarningIcon } from "./WarningIcon";
