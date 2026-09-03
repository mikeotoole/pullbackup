"""The shipped app icon is the chosen D4 "Layers" mark.

The icon is brand, not decoration, so it is pinned like a release artifact: a
byte digest fixes the exact artwork, and the structural assertions below say
*why* those bytes are the ones we want, so a future edit that silently guts the
meaning fails with a readable message rather than an opaque digest mismatch.

What the mark has to keep saying:

  * Three bars, brightest to dimmest, read as accumulated backup generations.
    The predecessor was an arrow into a tray, which said "download" rather than
    "backup". Flattening the three tones to one destroys the whole idea, so the
    three distinct fills are asserted individually.
  * The palette is AgentCTRL's, shared across Mike's projects and read from
    ``native/assets/icon-source.py`` rather than eyedropped. New colours are a
    regression even when they look similar.
  * The rounded-rect background is part of the mark (it is what a maskable/PWA
    tile needs), not a placeholder to be dropped.
  * The bars are filled, not stroked. AgentCTRL's own glyph is thin-stroke
    because it lives on a phone home screen; a 16px browser tab needs mass.
    This divergence is deliberate, so an outline-only regression is caught.

This module deliberately asserts on the file the app *ships*, not on a copy of
the artwork kept beside the test — a test that compares an asset to itself
proves nothing.

The favicon is not the whole inventory. The app also *draws* its own logo in
the running UI, and an earlier pass replaced only the served file — leaving the
retired glyph hardcoded in two components, so the brand split in two. The scan
below therefore covers all of ``frontend/src`` and ``frontend/index.html``, not
just the asset, and it keys on the retired geometry rather than on the presence
of the new one so that a *third* copy appearing anywhere is caught too.

Deliberately NOT in scope: ``frontend/tailwind.config.js``'s ``accent`` token,
which is also ``#7c3aed``. That is the UI theme — buttons, links, focus rings —
not the brand mark. Restyling the whole interface is a separate decision and is
not smuggled in behind an icon change.
"""

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Mike O'Toole

import hashlib
import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FAVICON = REPOSITORY_ROOT / "frontend" / "public" / "favicon.svg"
INDEX_HTML = REPOSITORY_ROOT / "frontend" / "index.html"

# sha256 of frontend/public/favicon.svg. Regenerate ONLY when the mark itself is
# deliberately redrawn:  shasum -a 256 frontend/public/favicon.svg
FAVICON_SHA256 = "24d3c71ad38212b5074bc965d8e2ee4bcd283cebb6b0c963f85e69762e713770"

# AgentCTRL's shared background gradient, darkest at the bottom.
BACKGROUND_STOPS = ("#26262b", "#161619", "#0c0c0f")
# AgentCTRL's shared signal gradient, used for the pull-down arrow.
SIGNAL_STOPS = ("#d3b6ff", "#8b5cf6")
# The three generations, brightest (newest) to dimmest (oldest).
BAR_FILLS = ("#c4a8ff", "#8b5cf6", "#5b21b6")

# A filled bar: <rect ... fill="#rrggbb"/> with no stroke.
BAR = re.compile(
    r'<rect\s[^>]*\bx="[\d.]+"[^>]*\bfill="(#[0-9a-fA-F]{6})"\s*/>',
)


def _favicon() -> str:
    return FAVICON.read_text()


def test_the_shipped_favicon_is_the_pinned_artwork():
    """Byte-exact: the file the app serves is the mark that was chosen."""
    digest = hashlib.sha256(FAVICON.read_bytes()).hexdigest()
    assert digest == FAVICON_SHA256, (
        "frontend/public/favicon.svg is not the pinned D4 artwork "
        f"(got {digest}, expected {FAVICON_SHA256})"
    )


def test_the_mark_carries_three_distinct_generation_bars():
    """Three bars, three tones. One flat colour is not this mark."""
    fills = BAR.findall(_favicon())
    assert len(fills) == 3, f"expected three generation bars, found {len(fills)}: {fills}"
    assert [f.lower() for f in fills] == list(BAR_FILLS), (
        f"generation bars must run brightest to dimmest {BAR_FILLS}, got {fills}"
    )
    assert len(set(f.lower() for f in fills)) == 3, "the three tones must stay distinct"


def test_the_bars_are_filled_rather_than_outlined():
    """A 16px tab needs mass; outline-only bars disappear at that size."""
    svg = _favicon()
    for fill in BAR_FILLS:
        rect = re.search(
            rf'<rect\s[^>]*fill="{re.escape(fill)}"\s*/>', svg, re.IGNORECASE
        )
        assert rect, f"no filled bar found for {fill}"
        assert "stroke=" not in rect.group(0), f"bar {fill} must be filled, not stroked"


def test_the_palette_is_the_shared_agentctrl_family():
    """Colours are inherited, not invented."""
    svg = _favicon().lower()
    for stop in BACKGROUND_STOPS + SIGNAL_STOPS:
        assert f'stop-color="{stop}"' in svg, f"missing gradient stop {stop}"


def test_the_rounded_rect_background_is_part_of_the_mark():
    """The tile shape is the mark's ground, not a placeholder."""
    svg = _favicon()
    assert re.search(
        r'<rect width="32" height="32" rx="7" fill="url\(#bg\)"\s*/>', svg
    ), "the rounded-rect gradient background must be present"


def test_the_pull_down_arrow_survives_in_the_signal_gradient():
    """The bars say 'generations'; the arrow says 'pulled in'."""
    svg = _favicon()
    assert 'stroke="url(#sig)"' in svg, "the arrow must use the signal gradient"
    assert 'd="M16 3.5 V11"' in svg, "the arrow shaft is missing"
    assert 'd="M12 7.2 L16 11.2 L20 7.2"' in svg, "the arrow head is missing"


def test_the_retired_download_into_a_tray_glyph_is_gone():
    """The predecessor read as 'download', which is the wrong verb."""
    svg = _favicon()
    assert 'd="M8 20.5 V25 H24 V20.5"' not in svg, "the old storage tray is still drawn"
    assert '#7c3aed"' not in svg.lower(), "the old flat violet ground is still present"


def test_the_accessible_label_is_preserved():
    assert 'aria-label="pullbackup"' in _favicon()


def test_every_icon_the_app_declares_points_at_the_replaced_asset():
    """The inventory is one asset; prove nothing else is declared.

    If a manifest, an apple-touch-icon or a PNG size is ever added, this test
    fails and forces the new asset to be swapped too, instead of the brand
    silently splitting in two.
    """
    html = INDEX_HTML.read_text()
    declared = re.findall(r'<link\s[^>]*rel="([^"]*icon[^"]*)"[^>]*href="([^"]+)"', html)
    assert declared == [("icon", "/favicon.svg")], (
        f"unexpected icon declarations in index.html: {declared}"
    )
    assert "manifest" not in html, "a web manifest now ships icons; pin them here too"

    public = REPOSITORY_ROOT / "frontend" / "public"
    shipped = sorted(p.name for p in public.iterdir() if p.is_file())
    assert shipped == ["favicon.svg"], (
        f"frontend/public now ships more than the one icon asset: {shipped}"
    )


# --- the in-app logo, which is drawn rather than served -------------------

FRONTEND_SRC = REPOSITORY_ROOT / "frontend" / "src"

# The retired mark's two identifying fingerprints. The tray floor is unique to
# the old glyph; the flat violet was its ground.
RETIRED_TRAY_PATH = "M8 20.5 V25 H24 V20.5"
RETIRED_BRAND_VIOLET = "#7c3aed"


def _frontend_sources() -> list[Path]:
    return sorted(
        p
        for p in FRONTEND_SRC.rglob("*")
        if p.is_file() and p.suffix in {".ts", ".tsx", ".js", ".jsx", ".css", ".svg"}
    )


def test_no_frontend_source_still_draws_the_retired_tray_glyph():
    """The served asset was replaced; every hand-drawn copy must go too.

    Keyed on the retired geometry, so a copy that reappears anywhere under
    ``frontend/src`` fails here rather than shipping a second brand.
    """
    offenders = [
        str(p.relative_to(REPOSITORY_ROOT))
        for p in _frontend_sources()
        if RETIRED_TRAY_PATH in p.read_text()
    ]
    assert offenders == [], (
        "the retired download-into-a-tray glyph is still drawn in: "
        f"{offenders}. The app icon and the in-app logo must be the same mark."
    )


def test_no_frontend_source_still_uses_the_retired_brand_violet():
    """``#7c3aed`` was the old mark's ground and is not in the D4 palette.

    Scoped to ``frontend/src`` and ``index.html``. ``tailwind.config.js``'s
    ``accent`` token keeps the value on purpose — that is the UI theme, not the
    brand mark, and retheming the interface is not part of this change.
    """
    offenders = [
        str(p.relative_to(REPOSITORY_ROOT))
        for p in _frontend_sources()
        if RETIRED_BRAND_VIOLET in p.read_text().lower()
    ]
    if RETIRED_BRAND_VIOLET in INDEX_HTML.read_text().lower():
        offenders.append(str(INDEX_HTML.relative_to(REPOSITORY_ROOT)))
    assert offenders == [], (
        f"the retired brand violet {RETIRED_BRAND_VIOLET} is still present in: "
        f"{offenders}"
    )


def test_the_in_app_logo_is_the_same_three_generation_mark():
    """One component draws the logo, and it carries the D4 structure.

    A single source means the two placements (app header, sign-in page) cannot
    drift apart again — which is exactly how the previous split happened.
    """
    brand = FRONTEND_SRC / "components" / "BrandMark.tsx"
    assert brand.is_file(), (
        "expected a single shared brand-mark component at "
        f"{brand.relative_to(REPOSITORY_ROOT)}"
    )
    source = brand.read_text()
    for fill in BAR_FILLS:
        assert fill in source, f"the in-app logo is missing generation bar {fill}"
    for stop in BACKGROUND_STOPS + SIGNAL_STOPS:
        assert stop in source, f"the in-app logo is missing gradient stop {stop}"
    assert 'd="M16 3.5 V11"' in source, "the in-app arrow shaft is missing"
    assert 'd="M12 7.2 L16 11.2 L20 7.2"' in source, "the in-app arrow head is missing"


def test_both_logo_placements_use_the_shared_component():
    """Neither placement may inline its own SVG again."""
    for relative in ("App.tsx", "pages/Login.tsx"):
        source = (FRONTEND_SRC / relative).read_text()
        assert "BrandMark" in source, (
            f"frontend/src/{relative} must render the shared <BrandMark />"
        )
        assert "<svg" not in source, (
            f"frontend/src/{relative} still inlines its own logo SVG"
        )

