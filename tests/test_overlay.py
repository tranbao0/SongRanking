"""
Guards the overlay bar's fade, which was rebuilt from a full-width
putdata() into a 1px column stretched with NEAREST.

This is a rendered artifact, so the bar is checked for pixel-exact
equality against the previous construction rather than for being
"close enough" - a fade that is off by one alpha step per row would be
invisible in a diff but is still a rendering change.
"""

import unittest

from . import context  # noqa: F401  (puts src/ on sys.path)

from PIL import Image

from render import overlay


def _reference_fade(cw, fade_h, bar_color, bar_alpha):
    """The pre-rewrite construction, kept as an oracle. Do not optimise."""
    col = Image.new("RGB", (1, max(fade_h, 1)))
    px = col.load()
    for y in range(fade_h):
        t = y / max(fade_h - 1, 1)
        px[0, y] = tuple(int(bar_color[i] + (bar_color[i] - bar_color[i]) * t) for i in range(3))
    fade_img = col.resize((max(cw, 1), max(fade_h, 1))).convert("RGBA")

    fade_alpha = Image.new("L", (cw, fade_h))
    fade_alpha.putdata(
        [int(bar_alpha * (y / max(fade_h - 1, 1))) for y in range(fade_h) for _ in range(cw)]
    )
    fade_img.putalpha(fade_alpha)
    return fade_img


def _new_fade(cw, fade_h, bar_color, bar_alpha):
    fade_img = Image.new("RGBA", (cw, fade_h), (*bar_color, 0))
    fade_img.putalpha(overlay._valpha_ramp(cw, fade_h, bar_alpha))
    return fade_img


class FadeRampTest(unittest.TestCase):
    def test_pixel_identical_to_previous_construction(self):
        # Includes the real style.json geometry plus edge sizes: a 1px-tall
        # strip exercises the max(height-1, 1) divide-by-zero guard.
        for cw, fade_h in ((1920, 90), (1080, 40), (640, 1), (1, 5)):
            with self.subTest(cw=cw, fade_h=fade_h):
                self.assertEqual(
                    _new_fade(cw, fade_h, (8, 9, 18), 0.93 * 255).tobytes(),
                    _reference_fade(cw, fade_h, (8, 9, 18), 0.93 * 255).tobytes(),
                )

    def test_ramp_is_flat_across_each_row(self):
        """A vertical fade must not pick up any horizontal variation."""
        ramp = overlay._valpha_ramp(300, 20, 255)
        for y in range(20):
            row = {ramp.getpixel((x, y)) for x in range(300)}
            self.assertEqual(len(row), 1, f"row {y} varies horizontally: {row}")

    def test_ramp_runs_transparent_to_max_alpha(self):
        ramp = overlay._valpha_ramp(10, 50, 255)
        self.assertEqual(ramp.getpixel((0, 0)), 0)
        self.assertEqual(ramp.getpixel((0, 49)), 255)


class BuildOverlayImageTest(unittest.TestCase):
    """
    End-to-end smoke test over the real style, mostly to catch the truncation
    loop's hoisted measurements changing what actually renders.
    """

    def setUp(self):
        self.style = overlay.load_style("assets/templates/style.json")

    def _render(self, **kwargs):
        params = dict(
            rank=6, title="Supernova", artist="aespa", peak=6,
            release_date="2024.05.13", months_on_chart=8, views=109_584_467,
            entry_type="new_entry", views_gained=1_234_567, rank_change="—",
        )
        params.update(kwargs)
        return overlay.build_overlay_image(self.style, **params)

    def test_renders_at_canvas_size(self):
        img = self._render()
        self.assertEqual(img.size, (self.style["canvas"]["width"], self.style["canvas"]["height"]))
        self.assertEqual(img.mode, "RGBA")

    def test_long_title_is_truncated_with_an_ellipsis(self):
        img = self._render(title="A" * 400)
        self.assertEqual(img.size, (self.style["canvas"]["width"], self.style["canvas"]["height"]))

    def test_renders_hangul_and_missing_optional_fields(self):
        """Hangul is the reason Pillow draws this instead of ffmpeg drawtext."""
        img = self._render(title="설탕 허니 아이스티", artist="BABYMONSTER",
                           entry_type="", views_gained=None, rank_change="")
        self.assertEqual(img.mode, "RGBA")

    def test_truncation_is_deterministic(self):
        self.assertEqual(
            self._render(title="B" * 200).tobytes(),
            self._render(title="B" * 200).tobytes(),
        )


if __name__ == "__main__":
    unittest.main()
