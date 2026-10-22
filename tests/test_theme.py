"""The reading themes' highlighters: complementary to the page, legible, never blending in."""

import colorsys
import math

from screenplay_reader.ui.theme import ACCENT, THEMES, theme_for


def _rgb(color):
    color = color.lstrip("#")
    return tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))


def _linear(c):
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(color):
    r, g, b = (_linear(c) for c in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _lab(color):
    r, g, b = (_linear(c) for c in _rgb(color))
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116  # noqa: E731
    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def _hue(color):
    r, g, b = (c / 255 for c in _rgb(color))
    return colorsys.rgb_to_hls(r, g, b)[0] * 360


def _hue_distance(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def test_the_four_themes_as_specified():
    assert [(t.name, t.page, t.text) for t in THEMES.values()] == [
        ("Original", "#FFFFFF", "#000000"),
        ("Quiet", "#4A4A4D", "#EBEBF5"),
        ("Calm", "#F1E1C9", "#34281B"),
        ("Dark", "#000000", "#FFFFFF"),
    ]


def test_highlighted_text_stays_legible():
    for t in THEMES.values():
        assert contrast(t.text, t.highlight) >= 4.5, t.name  # WCAG AA
        if not t.dark:
            assert contrast(t.text, t.highlight) >= 7, t.name


def test_highlight_never_blends_into_the_page():
    for t in THEMES.values():
        assert math.dist(_lab(t.highlight), _lab(t.page)) > 25, t.name


def test_highlight_is_the_pages_complement_on_the_color_wheel():
    for t in THEMES.values():
        r, g, b = (c / 255 for c in _rgb(t.page))
        tinted = colorsys.rgb_to_hls(r, g, b)[2] > 0.005  # saturation: the page has a hue
        source = _hue(t.page) if tinted else _hue(ACCENT)  # neutral pages complement the accent
        assert _hue_distance(_hue(t.highlight), source) > 150, t.name


def test_old_settings_map_to_a_theme():
    assert theme_for("light").key == "original"
    assert theme_for("dark").key == "dark"
    assert theme_for("nonsense").key == "original"


def test_the_frame_stands_apart_from_the_page():
    """Toolbar, player and status bar share one color, distinct from the page
    (WCAG's 3:1 for UI shapes), with readable labels on it."""
    for t in THEMES.values():
        assert contrast(t.chrome, t.page) >= 3, t.name
        assert contrast(t.on_chrome, t.chrome) >= 4.5, t.name


def test_the_frame_complements_the_page():
    for t in THEMES.values():
        r, g, b = (c / 255 for c in _rgb(t.page))
        tinted = colorsys.rgb_to_hls(r, g, b)[2] > 0.005
        if tinted:
            assert _hue_distance(_hue(t.chrome), _hue(t.page)) > 150, t.name
        else:  # neutral pages: slate indigo, a triad with the fern-green play button
            assert 100 < _hue_distance(_hue(t.chrome), _hue(ACCENT)) < 140, t.name


def test_the_title_is_prominent_on_the_frame():
    for t in THEMES.values():
        assert contrast(t.title, t.chrome) >= 4.5, t.name
        assert _hue_distance(_hue(t.title), _hue(t.chrome)) > 150, t.name  # the frame's complement


def test_the_play_button_never_disappears_into_the_frame():
    for t in THEMES.values():
        assert contrast(ACCENT, t.chrome) >= 3 or t.play_ring, t.name
