import json
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont, ImageFilter


def load_style(path="assets/templates/style.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=None)
def _font(path, size):
    return ImageFont.truetype(path, size)


def _rgb(c):
    return tuple(c[:3])


def _rgba(c, alpha=255):
    return (*c[:3], alpha)


def _measure(draw, font, text):
    """Ink bounding box offsets/size for `text` as if drawn at (0, 0)."""
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    return l, t, r - l, b - t


def _draw_text(draw, x, y, text, font, fill):
    """Draw `text` so its ink top-left lands exactly at (x, y). Returns (w, h)."""
    l, t, w, h = _measure(draw, font, text)
    draw.text((x - l, y - t), text, font=font, fill=fill)
    return w, h


def _text_w(draw, font, text):
    return _measure(draw, font, text)[2]


def _vgrad_rgb(width, height, top_rgb, bottom_rgb):
    """A width x height opaque image whose color lerps top_rgb -> bottom_rgb top to bottom."""
    col = Image.new("RGB", (1, max(height, 1)))
    px = col.load()
    for y in range(height):
        t = y / max(height - 1, 1)
        px[0, y] = tuple(int(top_rgb[i] + (bottom_rgb[i] - top_rgb[i]) * t) for i in range(3))
    return col.resize((max(width, 1), max(height, 1)))


def _draw_gradient_text(base, draw, x, y, text, font, top_rgb, bottom_rgb):
    """Draw `text` filled with a vertical color gradient, ink top-left at (x, y)."""
    l, t, w, h = _measure(draw, font, text)
    if w <= 0 or h <= 0:
        return 0, 0
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).text((-l, -t), text, font=font, fill=255)
    grad = _vgrad_rgb(w, h, top_rgb, bottom_rgb)
    base.paste(grad, (int(x), int(y)), mask)
    return w, h


def _hgrad_bar(base, x0, y0, x1, y1, left_rgb, right_rgb, alpha):
    """Paint a horizontal color gradient rectangle (used for the top accent line)."""
    width = x1 - x0
    row = Image.new("RGB", (max(width, 1), 1))
    px = row.load()
    for x in range(width):
        t = x / max(width - 1, 1)
        px[x, 0] = tuple(int(left_rgb[i] + (right_rgb[i] - left_rgb[i]) * t) for i in range(3))
    row = row.resize((max(width, 1), max(y1 - y0, 1)))
    row_rgba = row.convert("RGBA")
    row_rgba.putalpha(alpha)
    base.paste(row_rgba, (x0, y0), row_rgba)


def _radial_gradient_circle(diameter, center_rgb, edge_rgb):
    """An RGBA image of a filled circle with a radial gradient from center to edge."""
    img = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    put = img.putpixel
    cx = cy = (diameter - 1) / 2
    max_d = diameter / 2
    for y in range(diameter):
        dy = y - cy
        for x in range(diameter):
            dx = x - cx
            dist = (dx * dx + dy * dy) ** 0.5
            if dist > max_d:
                continue
            t = dist / max_d
            rgb = tuple(round(center_rgb[i] + (edge_rgb[i] - center_rgb[i]) * t) for i in range(3))
            put((x, y), (*rgb, 255))
    return img


def _glow_layer(diameter, color_rgb, blur, alpha):
    """A blurred, padded circle to sit behind the badge as a soft glow."""
    pad = blur * 2
    size = diameter + pad * 2
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse([pad, pad, pad + diameter, pad + diameter], fill=(*color_rgb, alpha))
    return layer.filter(ImageFilter.GaussianBlur(blur)), pad


def _fit_fontsize(draw, font_path, text, max_width, start_size, min_size):
    """Shrink font size until `text` fits within max_width (never below min_size)."""
    size = start_size
    while size > min_size:
        font = _font(font_path, size)
        if _text_w(draw, font, text) <= max_width:
            return font
        size -= 2
    return _font(font_path, min_size)


def build_overlay_image(style, *, rank, title, artist, peak, years_on_chart, views,
                         entry_type="", views_gained=None, rank_change=""):
    """
    Render the full bottom-bar overlay as an RGBA PNG-ready image, sized to the
    style's canvas. ffmpeg composites this over the video with a plain `overlay`
    filter — all the visual work (gradients, rounded chips, blurred glow badge)
    happens here in Pillow, since drawtext/drawbox can't do soft edges or curves.
    """
    fb = style["font_bold"]
    fr = style["font_regular"]
    cw = style.get("canvas", {}).get("width", 1920)
    ch = style.get("canvas", {}).get("height", 1080)

    bar = style["bar"]
    solid_h = bar["solid_height"]
    fade_h  = bar["fade_height"]
    bar_top = ch - solid_h
    fade_top = bar_top - fade_h
    bar_color = _rgb(bar["color"])
    bar_alpha = bar["opacity"] * 255

    img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── Gradient fade above the solid bar, then the solid bar itself ───────
    fade_img = _vgrad_rgb(cw, fade_h, bar_color, bar_color).convert("RGBA")
    fade_alpha = Image.new("L", (cw, fade_h))
    fade_alpha.putdata([int(bar_alpha * (y / max(fade_h - 1, 1))) for y in range(fade_h) for _ in range(cw)])
    fade_img.putalpha(fade_alpha)
    img.paste(fade_img, (0, fade_top), fade_img)
    draw.rectangle([0, bar_top, cw, ch], fill=(*bar_color, int(bar_alpha)))

    # ── Top accent line (thin gradient hairline capping the fade) ──────────
    al = bar["accent_line"]
    _hgrad_bar(img, 0, fade_top - al["height"], cw, fade_top,
               _rgb(al["color_left"]), _rgb(al["color_right"]), int(al["opacity"] * 255))

    left_x  = bar["margin_left"]
    right_x = cw - bar["margin_right"]
    row1_y  = bar_top + bar["padding_top"]

    # ── Row 1: rank, divider, title • artist ───────────────────────────────
    r = style["rank"]
    rank_font = _font(fb, r["fontsize"])
    rank_text = f"{rank}."
    shadow_off = r["shadow_offset"]
    _draw_text(draw, left_x + shadow_off[0], row1_y + shadow_off[1], rank_text, rank_font,
               (*_rgb(r["shadow_color"]), r["shadow_alpha"]))
    rank_w, rank_h = _draw_gradient_text(img, draw, left_x, row1_y, rank_text, rank_font,
                                          _rgb(r["gradient_top"]), _rgb(r["gradient_bottom"]))
    row1_center_y = row1_y + rank_h / 2

    d = style["divider"]
    div_x = left_x + rank_w + d["gap"]
    draw.line([(div_x, row1_center_y - rank_h * 0.4), (div_x, row1_center_y + rank_h * 0.4)],
              fill=_rgba(d["color"], d["alpha"]), width=d["width"])

    t = style["title"]
    text_x = div_x + d["gap"]
    max_title_w = right_x - text_x - t["max_width_reserve"]

    title_font = _font(fb, t["fontsize"])
    sep_font   = _font(fr, t["artist_fontsize"])
    artist_font = _font(fr, t["artist_fontsize"])

    display_title = title
    full_w = (_text_w(draw, title_font, display_title)
              + _text_w(draw, sep_font, t["separator"])
              + _text_w(draw, artist_font, artist.upper()))
    while full_w > max_title_w and len(display_title) > 3:
        display_title = display_title[:-1]
        full_w = (_text_w(draw, title_font, display_title + "…")
                  + _text_w(draw, sep_font, t["separator"])
                  + _text_w(draw, artist_font, artist.upper()))
    if display_title != title:
        display_title += "…"

    title_font_h = _measure(draw, title_font, display_title)[3]
    title_y = row1_center_y - title_font_h / 2
    tw, th = _draw_text(draw, text_x, title_y, display_title, title_font, _rgba(t["color"]))

    sep_h = _measure(draw, sep_font, t["separator"])[3]
    sep_x = text_x + tw
    sw, _ = _draw_text(draw, sep_x, row1_center_y - sep_h / 2, t["separator"], sep_font,
                        _rgba(t["separator_color"]))

    artist_text = artist.upper()
    artist_h = _measure(draw, artist_font, artist_text)[3]
    _draw_text(draw, sep_x + sw, row1_center_y - artist_h / 2, artist_text, artist_font,
               _rgba(t["artist_color"]))

    # ── Row 2: stat chips ───────────────────────────────────────────────────
    st = style["stats"]
    label_font_cache = {}
    value_font_cache = {}

    def _lfont(size):
        return label_font_cache.setdefault(size, _font(fr, size))

    def _vfont(size):
        return value_font_cache.setdefault(size, _font(fb, size))

    label_font = _lfont(st["label_fontsize"])
    value_font = _vfont(st["value_fontsize"])
    delta_font = _font(fb, st["delta_fontsize"])

    values = {"peak": peak, "years": years_on_chart, "views": f"{int(views):,}"}
    chips = []
    for item in st["items"]:
        key = item["key"]
        value_text = str(values[key])
        extra_w = 0
        extra_render = None
        if key == "views" and views_gained is not None:
            sign = "+" if views_gained >= 0 else ""
            delta_text = f" {sign}{views_gained:,}"
            extra_w = _text_w(draw, delta_font, delta_text)
            extra_render = delta_text
        w = (st["chip_padding_x"] + st["dot_diameter"] + st["dot_gap"]
             + _text_w(draw, label_font, item["label"]) + st["value_gap"]
             + _text_w(draw, value_font, value_text) + extra_w + st["chip_padding_x"])
        chips.append((item, value_text, extra_render, w))

    row2_y = row1_y + rank_h + style["row_gap"]
    chip_h = st["chip_height"]
    cx = text_x
    for item, value_text, extra_render, w in chips:
        draw.rounded_rectangle([cx, row2_y, cx + w, row2_y + chip_h], radius=st["chip_radius"],
                                fill=_rgba((255, 255, 255), st["chip_bg_alpha"]))
        cy = row2_y + chip_h / 2
        inner_x = cx + st["chip_padding_x"]
        dot_d = st["dot_diameter"]
        draw.ellipse([inner_x, cy - dot_d / 2, inner_x + dot_d, cy + dot_d / 2],
                     fill=_rgba(item["dot_color"]))
        inner_x += dot_d + st["dot_gap"]
        lh = _measure(draw, label_font, item["label"])[3]
        lw, _ = _draw_text(draw, inner_x, cy - lh / 2, item["label"], label_font, _rgba(st["label_color"]))
        inner_x += lw + st["value_gap"]
        vh = _measure(draw, value_font, value_text)[3]
        vw, _ = _draw_text(draw, inner_x, cy - vh / 2, value_text, value_font, _rgba(item["value_color"]))
        if extra_render:
            inner_x += vw
            up = views_gained is not None and views_gained >= 0
            delta_color = st["delta_color_up"] if up else st["delta_color_down"]
            dh = _measure(draw, delta_font, extra_render)[3]
            _draw_text(draw, inner_x, cy - dh / 2, extra_render, delta_font, _rgba(delta_color))
        cx += w + st["chip_gap"]

    # ── Rank-change chip (↑ / ↓ / −), placed after the last stat chip ──────
    if rank_change:
        rc = style["rank_change"]
        color_map = {"↑": rc["color_up"], "↓": rc["color_down"], "−": rc["color_same"]}
        rc_font = _font(fb, rc["fontsize"])
        rh = _measure(draw, rc_font, rank_change)[3]
        _draw_text(draw, cx + rc["chip_gap"], row2_y + chip_h / 2 - rh / 2, rank_change, rc_font,
                    _rgba(color_map.get(rank_change, rc["color_same"])))

    # ── Entry badge: glowing gradient circle, bottom-right ─────────────────
    badge_types = style.get("badge_types", {})
    if entry_type and entry_type in badge_types:
        bg = style["badge"]
        bt = badge_types[entry_type]
        dia = bg["diameter"]
        center_x = cw - bg["margin_right"] - dia // 2
        center_y = bar_top + bg["center_y_offset"]

        grad_top, grad_bottom = (tuple(c) for c in bt["gradient"])
        glow, glow_pad = _glow_layer(dia, grad_top, bg["glow_blur"], bg["glow_alpha"])
        img.paste(glow, (int(center_x - dia / 2 - glow_pad), int(center_y - dia / 2 - glow_pad)), glow)

        circle = _radial_gradient_circle(dia, grad_top, grad_bottom)
        circle_x = int(center_x - dia / 2)
        circle_y = int(center_y - dia / 2)
        img.paste(circle, (circle_x, circle_y), circle)

        draw.ellipse([circle_x, circle_y, circle_x + dia, circle_y + dia],
                     outline=_rgba(bg["ring_color"], bg["ring_alpha"]), width=bg["ring_width"])

        max_line_w = dia * 0.8
        l1 = bt["line1"]
        l2 = bt["line2"]
        f1 = _fit_fontsize(draw, fb, l1, max_line_w, bg["fontsize"], bg["min_fontsize"])
        f2 = _fit_fontsize(draw, fb, l2, max_line_w, bg["fontsize"], bg["min_fontsize"])
        h1 = _measure(draw, f1, l1)[3]
        h2 = _measure(draw, f2, l2)[3]
        total_h = h1 + bg["line_gap"] + h2
        y1 = center_y - total_h / 2
        y2 = y1 + h1 + bg["line_gap"]
        w1 = _text_w(draw, f1, l1)
        w2 = _text_w(draw, f2, l2)
        _draw_text(draw, center_x - w1 / 2, y1, l1, f1, _rgba(bg["text_color"]))
        _draw_text(draw, center_x - w2 / 2, y2, l2, f2, _rgba(bg["text_color"]))

    return img


def build_filter_complex(cw, ch):
    """
    ffmpeg filter_complex compositing the raw clip (input 0) scaled/padded to
    the canvas with the pre-rendered overlay PNG (input 1) laid on top.
    """
    return (
        f"[0:v]scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
        f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black[bg];"
        f"[bg][1:v]overlay=0:0:format=auto[vout]"
    )


if __name__ == "__main__":
    style = load_style()
    img = build_overlay_image(
        style,
        rank=6, title="Sugar Honey Ice Tea", artist="BABYMONSTER",
        peak=6, years_on_chart=1, views=109_584_467, entry_type="new_entry",
        views_gained=1_234_567, rank_change="↑",
    )
    canvas = Image.new("RGB", img.size, (30, 30, 30))
    canvas.paste(img, (0, 0), img)
    canvas.save("assets/templates/_preview.png")
    print("Saved assets/templates/_preview.png")
