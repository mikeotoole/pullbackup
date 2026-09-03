// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole

/**
 * The pullbackup brand mark — the same "layers" artwork the app serves as its
 * favicon (`frontend/public/favicon.svg`), redrawn here as JSX because the
 * running UI paints its logo rather than loading the asset.
 *
 * It lives in one component on purpose. The header and the sign-in page
 * previously each inlined their own copy, so replacing the favicon left two
 * stale copies of the retired glyph shipping in the UI. `tests/test_app_icon.py`
 * pins both the served asset and this component, and fails if either placement
 * inlines an SVG again.
 *
 * Three bars, brightest to dimmest, read as accumulated backup generations; the
 * arrow says the generations are *pulled in*. The palette is the family palette
 * shared across the wider project, not eyedropped — do not substitute similar
 * looking values.
 *
 * The gradient ids are namespaced (`brandmark-*`) because SVG defs are
 * document-global: an unprefixed `#bg` would collide with any other inline SVG
 * on the page and silently repaint one of them.
 */
export function BrandMark({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <defs>
        <linearGradient id="brandmark-bg" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#26262b" />
          <stop offset="0.55" stopColor="#161619" />
          <stop offset="1" stopColor="#0c0c0f" />
        </linearGradient>
        <linearGradient id="brandmark-sig" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#d3b6ff" />
          <stop offset="1" stopColor="#8b5cf6" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="7" fill="url(#brandmark-bg)" />
      <g
        fill="none"
        stroke="url(#brandmark-sig)"
        strokeWidth="2.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M16 3.5 V11" />
        <path d="M12 7.2 L16 11.2 L20 7.2" />
      </g>
      <rect x="7" y="14.8" width="18" height="3.1" rx="1.2" fill="#c4a8ff" />
      <rect x="7" y="19.4" width="18" height="3.1" rx="1.2" fill="#8b5cf6" />
      <rect x="7" y="24" width="18" height="3.1" rx="1.2" fill="#5b21b6" />
    </svg>
  );
}
