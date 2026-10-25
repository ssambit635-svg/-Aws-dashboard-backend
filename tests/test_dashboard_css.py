"""Static-asset checks that the DOM tests structurally cannot make.

The jsdom suite runs the real page with the real JavaScript, but it does not
apply the real stylesheet. That gap hid a total failure: `.overlay` and
`.errorbar` set `display: grid/flex`, which — being author declarations — beat
the user-agent rule that implements the `hidden` attribute. Every element the
code hid with `el.hidden = true` stayed on screen.

In a browser that meant both full-screen overlays were always visible, stacked
on top of the dashboard, swallowing every click. The page looked blank and no
button worked. jsdom reported `display: none` for the same markup, because it
special-cases the attribute instead of running the cascade, so 49 DOM checks
passed on a page that was unusable for a human.

These tests read the CSS as a document and enforce the invariants that keep that
from coming back. They are cheap and they are the only automated guard available
without a real browser engine.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = Path("app/static/css/app.css").read_text(encoding="utf-8")
HTML = Path("app/static/index.html").read_text(encoding="utf-8")
JS = Path("app/static/js/app.js").read_text(encoding="utf-8")

# The UA-stylesheet rule the `hidden` attribute implies, which any author
# `display` declaration would otherwise override.
HIDDEN_GUARD = re.compile(r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important", re.DOTALL)


def _rule_bodies(css: str) -> dict[str, str]:
    """Map a flat selector to its declaration block. Good enough for this file,
    which uses no nesting; @media blocks are matched by their inner selectors."""
    return {
        " ".join(match.group(1).split()): match.group(2)
        for match in re.finditer(r"([^{}@/]+)\{([^}]*)\}", css)
    }


def _selectors_setting_display() -> list[str]:
    return [
        selector
        for selector, body in _rule_bodies(CSS).items()
        if re.search(r"(^|;|\s)display\s*:", body)
    ]


def hidden_element_selectors() -> set[str]:
    """Selectors matching elements the HTML hides by default.

    Only elements with the attribute in the markup matter: those are the ones
    whose initial state depends on `hidden` doing its job.
    """
    selectors: set[str] = set()
    for match in re.finditer(r'<[a-z][^>]*\bid="([^"]+)"[^>]*>', HTML):
        tag = match.group(0)
        if "hidden" not in tag:
            continue
        element_id = match.group(1)
        selectors.add(f"#{element_id}")
        classes = re.search(r'class="([^"]+)"', tag)
        if classes:
            selectors |= {f".{name}" for name in classes.group(1).split()}
    return selectors


# ── the invariant ─────────────────────────────────────────────────────────
def test_hidden_attribute_is_guarded_with_important():
    """Without `!important`, any later author `display` rule wins again."""
    assert HIDDEN_GUARD.search(CSS), (
        "app.css must contain `[hidden] { display: none !important; }`. "
        "Author `display` declarations override the UA rule behind the `hidden` "
        "attribute, which leaves overlays permanently on screen."
    )


def test_every_hidden_element_is_protected():
    """Catch the specific combination that blanked the dashboard."""
    hidden_selectors = hidden_element_selectors()
    assert "#palette" in hidden_selectors and "#shortcuts" in hidden_selectors, (
        "the overlay markup changed; this test needs updating"
    )

    display_rules = _selectors_setting_display()
    offenders = {
        selector
        for selector in hidden_selectors
        for rule in display_rules
        if selector in rule.split(",") or rule == selector
    }

    # The offenders are exactly why the guard exists; if any appear, the guard
    # must be present for the page to work at all.
    assert HIDDEN_GUARD.search(CSS), (
        f"{sorted(offenders)} set display, which defeats the hidden attribute "
        "without a [hidden] guard"
    )


def test_overlays_are_full_screen_layers():
    """Sanity: confirm these really are the click-blocking kind of element."""
    bodies = _rule_bodies(CSS)
    assert "position: fixed" in bodies[".overlay"]
    assert "inset: 0" in bodies[".overlay"]


# ── one overlay at a time ─────────────────────────────────────────────────
def test_shortcuts_open_closes_the_palette():
    assert re.search(r"function openShortcuts\(\)\s*\{[^}]*closePalette\(\)", JS), (
        "opening the shortcuts dialog must dismiss the palette; two full-screen "
        "overlays leave the user inside a modal they cannot dismiss"
    )


def test_opening_the_palette_closes_the_shortcuts_dialog():
    opening = JS[JS.index("function openPalette()") : JS.index("function openPalette()") + 700]
    assert 'el("shortcuts").hidden = true' in opening


def test_escape_closes_both_overlays():
    keydown = JS[JS.index('if (e.key === "Escape")') :][:400]
    assert "closePalette()" in keydown
    assert "closeShortcuts()" in keydown


# ── the styling the screenshots depend on ─────────────────────────────────
@pytest.mark.parametrize(
    "selector",
    [".overlay", ".modal", ".palette", ".errorbar", ".rail-badge", ".toast"],
)
def test_core_styles_are_present(selector):
    """A truncated or unserved stylesheet produced a raw unstyled page."""
    assert selector in CSS, f"{selector} is missing from app.css"


def test_page_links_the_stylesheet():
    assert 'href="/static/css/app.css"' in HTML


def test_no_inline_style_hides_an_overlay():
    """Inline `style="display:none"` would win over the class and desync the JS."""
    for element_id in ("palette", "shortcuts"):
        pattern = re.compile(rf'<[^>]*id="{element_id}"[^>]*style="[^"]*display', re.IGNORECASE)
        assert not pattern.search(HTML), f"#{element_id} must be controlled by the hidden attribute"
