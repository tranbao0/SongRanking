import json
import unicodedata
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFont


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
    # NFC first: some sources (e.g. copy-pasted artist names) hand us
    # decomposed Unicode - a base letter plus a combining accent as two
    # codepoints. Pillow's default (non-raqm) layout draws those as two
    # separate glyphs instead of composing them, so a combining accent
    # with no precomposed glyph in the font renders as a tofu box.
    text = unicodedata.normalize("NFC", text)
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    return l, t, r - l, b - t


def _draw_text(draw, x, y, text, font, fill):
    """Draw `text` so its ink top-left lands exactly at (x, y). Returns (w, h)."""
    text = unicodedata.normalize("NFC", text)
    l, t, w, h = _measure(draw, font, text)
    draw.text((x - l, y - t), text, font=font, fill=fill)
    return w, h


def _text_w(draw, font, text):
    return _measure(draw, font, text)[2]


def _draw_baseline(draw, x, baseline_y, text, font, fill):
    """
    Draw `text` with its left edge at x and its baseline at baseline_y, then
    return its advance width. Used for runs of mixed-size text that need to
    read as one line (title + "by" + artist) - centering each piece on its
    own ink bbox instead would drift them apart, since bbox height depends
    on the specific string (a bare-caps title has no descender; "by" does),
    so same-sized text ends up sitting at visibly different heights.
    """
    text = unicodedata.normalize("NFC", text)
    draw.text((x, baseline_y), text, font=font, fill=fill, anchor="ls")
    return draw.textlength(text, font=font)


def _valpha_ramp(width, height, max_alpha):
    """
    A width x height 8-bit mask ramping 0 -> max_alpha top to bottom, each
    row a single flat value. Built one pixel wide and stretched with NEAREST
    (exact replication, no resampling, since only the width changes), rather
    than materialising a width*height Python list to putdata() - at 1920px
    wide that list alone costs ~45ms per overlay for an identical result.
    """
    column = Image.new("L", (1, max(height, 1)))
    column.putdata([int(max_alpha * (y / max(height - 1, 1))) for y in range(height)])
    return column.resize((max(width, 1), max(height, 1)), Image.NEAREST)


def _halpha_ramp(width, height, max_alpha, min_alpha):
    """
    A width x height 8-bit mask ramping max_alpha -> min_alpha left to
    right, each column a single flat value - the horizontal counterpart to
    _valpha_ramp, built one pixel tall and stretched with NEAREST for the
    same reason (only the height changes, so no resampling needed).
    """
    row = Image.new("L", (max(width, 1), 1))
    row.putdata([
        int(max_alpha + (min_alpha - max_alpha) * (x / max(width - 1, 1)))
        for x in range(width)
    ])
    return row.resize((max(width, 1), max(height, 1)), Image.NEAREST)


def _fit_fontsize(draw, font_path, text, max_width, start_size, min_size):
    """Shrink font size until `text` fits within max_width (never below min_size)."""
    size = start_size
    while size > min_size:
        font = _font(font_path, size)
        if _text_w(draw, font, text) <= max_width:
            return font
        size -= 2
    return _font(font_path, min_size)


def build_overlay_image(style, *, rank, title, artist, peak, release_date, months_on_chart,
                         views, entry_type="", views_gained=None, rank_change=""):
    """
    Render the full bottom-bar overlay as an RGBA PNG-ready image, sized to the
    style's canvas. ffmpeg composites this over the video with a plain `overlay`
    filter - Pillow does the actual drawing (font supports Hangul via Malgun
    Gothic) since drawtext/drawbox can't do soft-edged gradients cleanly.

    The bar sits `bottom_margin` px above the frame's bottom edge so it isn't
    covered by YouTube's timeline/scrubber overlay during playback. The only
    gradients in the design are the bar's own fade-to-solid top edge and its
    mirrored solid-to-fade bottom edge; every other element (rank, badge,
    stats) is flat-colored per the style config.
    """
    fb = style["font_bold"]
    fr = style["font_regular"]
    cw = style.get("canvas", {}).get("width", 1920)
    ch = style.get("canvas", {}).get("height", 1080)

    bar = style["bar"]
    solid_h = bar["solid_height"]
    fade_h  = bar["fade_height"]
    bottom_margin = bar.get("bottom_margin", 0)
    bar_bottom = ch - bottom_margin
    bar_top = bar_bottom - solid_h
    fade_top = bar_top - fade_h
    bar_color = _rgb(bar["color"])
    bar_alpha = bar["opacity"] * 255
    # The whole bar - fades and solid alike - also dims left to right, down
    # to this floor at the right edge, so it reads as one long horizontal
    # blur rather than a uniform-opacity rectangle with only its top/bottom
    # edges soft.
    min_alpha = bar_alpha * bar.get("opacity_right", 1.0)

    img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── Gradient fades above and below the solid bar ────────────────────────
    # Everything else (rank, stats, badge) is drawn with flat fills per the
    # style constraints. Each region's alpha is the product of its vertical
    # fraction ramp (0->1 for the top fade, flat 1 for the solid strip, 1->0
    # for the bottom fade) and the shared horizontal ramp - ImageChops.
    # multiply does that per-pixel product directly on the 8-bit masks.
    h_fade_top = _halpha_ramp(cw, fade_h, bar_alpha, min_alpha)
    v_fade_top = _valpha_ramp(cw, fade_h, 255)
    fade_img = Image.new("RGBA", (cw, fade_h), (*bar_color, 0))
    fade_img.putalpha(ImageChops.multiply(v_fade_top, h_fade_top))
    img.paste(fade_img, (0, fade_top), fade_img)

    solid_img = Image.new("RGBA", (cw, solid_h), (*bar_color, 0))
    solid_img.putalpha(_halpha_ramp(cw, solid_h, bar_alpha, min_alpha))
    img.paste(solid_img, (0, bar_top), solid_img)

    # The bottom fade is capped to bottom_margin (rather than reusing fade_h
    # like the top fade does) so it always finishes dissolving to fully
    # transparent right at the canvas edge. Since the canvas ends bottom_
    # margin px below the bar, a fade taller than that gets hard-clipped
    # mid-gradient - visible as a sharp cutoff line that reads as a floating
    # box instead of the bar bleeding into the footage below it.
    bottom_fade_h = min(fade_h, bottom_margin)
    if bottom_fade_h > 0:
        h_fade_bottom = _halpha_ramp(cw, bottom_fade_h, bar_alpha, min_alpha)
        v_fade_bottom = _valpha_ramp(cw, bottom_fade_h, 255).transpose(Image.FLIP_TOP_BOTTOM)
        bottom_fade_img = Image.new("RGBA", (cw, bottom_fade_h), (*bar_color, 0))
        bottom_fade_img.putalpha(ImageChops.multiply(v_fade_bottom, h_fade_bottom))
        img.paste(bottom_fade_img, (0, bar_bottom), bottom_fade_img)

    left_x  = bar["margin_left"]
    right_x = cw - bar["margin_right"]
    content_top    = bar_top + bar["padding_top"]
    content_bottom = bar_bottom - bar["padding_bottom"]

    # ── Horizontal layout: rank width fixes where the divider/title start ──
    r = style["rank"]
    rank_font = _font(fb, r["fontsize"])
    rank_text = f"{rank}."
    rank_w, rank_h = _measure(draw, rank_font, rank_text)[2:]

    next_x = left_x + rank_w
    rc_font = rc_w = rc_display = None
    if rank_change:
        rc = style["rank_change"]
        rc_font = _font(fb, rc["fontsize"])
        rc_display = f"({rank_change})"
        rc_w = _text_w(draw, rc_font, rc_display)
        next_x += rc["chip_gap"] + rc_w

    d = style["divider"]
    div_x = next_x + d["gap"]

    t = style["title"]
    text_x = div_x + d["gap"]
    max_title_w = right_x - text_x - t["max_width_reserve"]

    title_font  = _font(fb, t["fontsize"])
    # "by" always renders in the artist's own font/size, never the title's -
    # it's part of the same visual unit as the artist name, not the title.
    artist_font = _font(fr, t["artist_fontsize"])

    # The separator and artist are fixed for the whole loop, so they're
    # measured once instead of re-measured on every character dropped.
    artist_text = artist.upper()
    fixed_w = _text_w(draw, artist_font, t["separator"]) + _text_w(draw, artist_font, artist_text)

    display_title = title
    full_w = _text_w(draw, title_font, display_title) + fixed_w
    while full_w > max_title_w and len(display_title) > 3:
        display_title = display_title[:-1]
        full_w = _text_w(draw, title_font, display_title + "…") + fixed_w
    if display_title != title:
        display_title += "…"

    st = style["stats"]
    label_font = _font(fr, st["fontsize"])
    value_font = _font(fr, st["fontsize"])
    delta_font = _font(fr, st["fontsize"])

    # ── Vertical layout: title row, then stats row, then rank spans both ───
    # Row height comes from the font's fixed ascent+descent, not the specific
    # string's ink bbox - titles with descenders (e.g. "Soda Pop") would
    # otherwise measure taller than ones without, throwing off row spacing
    # inconsistently from clip to clip.
    title_h = sum(title_font.getmetrics())
    value_h = sum(value_font.getmetrics())

    # Centered in the padded content area rather than pinned to content_top:
    # the block's actual height (from fixed font metrics) is smaller than
    # the space padding_top/padding_bottom leave for it, so anchoring to the
    # top dumped all that slack below row 2 - title hugging the bar's top
    # edge while a visibly empty gap sat under the stats row.
    block_h = title_h + style["row_gap"] + value_h
    row1_y = content_top + max(0, (content_bottom - content_top - block_h) / 2)
    row1_baseline = row1_y + title_font.getmetrics()[0]
    row2_y = row1_y + title_h + style["row_gap"]
    row2_center_y = row2_y + value_h / 2
    block_bottom = row2_y + value_h

    rank_center_y = (row1_y + block_bottom) / 2
    rank_y = rank_center_y - rank_h / 2
    rank_y = max(content_top, min(rank_y, content_bottom - rank_h))
    rank_center_y = rank_y + rank_h / 2

    # ── Rank number ─────────────────────────────────────────────────────────
    shadow_off = r["shadow_offset"]
    _draw_text(draw, left_x + shadow_off[0], rank_y + shadow_off[1], rank_text, rank_font,
               (*_rgb(r["shadow_color"]), r["shadow_alpha"]))
    _draw_text(draw, left_x, rank_y, rank_text, rank_font, _rgba(r["color"]))

    if rank_change:
        rc = style["rank_change"]
        color_map = {"↑": rc["color_up"], "↓": rc["color_down"], "—": rc["color_same"]}
        rc_h = _measure(draw, rc_font, rc_display)[3]
        rc_x = left_x + rank_w + rc["chip_gap"]
        _draw_text(draw, rc_x, rank_center_y - rc_h / 2, rc_display, rc_font,
                   _rgba(color_map.get(rank_change, rc["color_same"])))

    draw.line([(div_x, row1_y), (div_x, block_bottom)],
              fill=_rgba(d["color"], d["alpha"]), width=d["width"])

    # ── Row 1: title, then "by" + artist sharing one baseline ──────────────
    # All three pieces sit on row1_baseline rather than each being centered
    # on its own ink bbox, so "by ARTIST" (smaller, regular weight) reads as
    # part of the same text line as the title instead of floating at a
    # slightly different height. "by" always uses artist_font (same size/
    # weight as the artist name), never the title's font.
    tw = _draw_baseline(draw, text_x, row1_baseline, display_title, title_font, _rgba(t["color"]))
    sep_x = text_x + tw
    sw = _draw_baseline(draw, sep_x, row1_baseline, t["separator"], artist_font, _rgba(t["separator_color"]))
    _draw_baseline(draw, sep_x + sw, row1_baseline, artist_text, artist_font, _rgba(t["artist_color"]))

    # ── Row 2: stats as plain label + value groups (no dots, no sub-boxes) ─
    values = {
        "released": release_date,
        "peak":     peak,
        "months":   months_on_chart,
        "views":    f"{int(views):,}",
    }
    cx = text_x
    for item in st["items"]:
        key = item["key"]
        value_text = str(values[key])

        lh = _measure(draw, label_font, item["label"])[3]
        lw, _ = _draw_text(draw, cx, row2_center_y - lh / 2, item["label"], label_font, _rgba(st["label_color"]))
        cx += lw + st["value_gap"]

        vh = _measure(draw, value_font, value_text)[3]
        vw, _ = _draw_text(draw, cx, row2_center_y - vh / 2, value_text, value_font, _rgba(item["value_color"]))
        cx += vw

        if key == "views" and views_gained is not None:
            sign = "+" if views_gained >= 0 else ""
            delta_text = f" ({sign}{views_gained:,})"
            up = views_gained >= 0
            delta_color = st["delta_color_up"] if up else st["delta_color_down"]
            dh = _measure(draw, delta_font, delta_text)[3]
            dw, _ = _draw_text(draw, cx, row2_center_y - dh / 2, delta_text, delta_font, _rgba(delta_color))
            cx += dw

        cx += st["group_gap"]

    # ── Entry badge: flat-colored circle, bottom-right ─────────────────────
    badge_types = style.get("badge_types", {})
    if entry_type and entry_type in badge_types:
        bg = style["badge"]
        bt = badge_types[entry_type]
        dia = bg["diameter"]
        center_x = cw - bg["margin_right"] - dia // 2
        center_y = rank_center_y + bg["center_y_offset"]
        circle_x = int(center_x - dia / 2)
        circle_y = int(center_y - dia / 2)

        draw.ellipse([circle_x, circle_y, circle_x + dia, circle_y + dia], fill=_rgba(bt["color"]))
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


def build_song_filter_complex(cw, ch, fps, overlay_enter, overlay_exit, overlay_duration,
                              lead_bare, trail_bare, total_duration):
    """
    One-pass video graph for a full song segment. Scale/pad/fps once, build a
    bare copy and an overlaid copy from that same stream, trim the timeline
    pieces from those copies, wipe overlay on/off with xfade, then concat.

    Keeping this in one graph (instead of separate per-phase encodes) means
    the 24->30 retiming never restarts at an overlay wipe - those phase
    seams were where audio glitched even with a continuous audio mux.

    Inputs: 0 = raw clip already trimmed to this segment's window, 1 = the
    overlay PNG looped for `total_duration`. Video only - audio is muxed
    afterward as one continuous track (see encode_song).

    Relative timeline inside the trimmed input:
        [0, lead_bare)                          bare (edge clips only)
        [lead_bare, lead_bare+od)               wipe bare -> overlay
        [lead_bare+od, total-trail-od)          static overlay
        [total-trail-od, total-trail)           wipe overlay -> bare
        [total-trail, total)                    bare (edge clips only)
    """
    od = overlay_duration
    in_start = lead_bare
    in_end = lead_bare + od
    out_start = total_duration - trail_bare - od
    out_end = out_start + od

    # How many consumers each master stream needs (always the wipe/static
    # pieces; lead/trail only when this is an edge clip).
    bare_n = 2 + (1 if lead_bare else 0) + (1 if trail_bare else 0)
    ov_n = 3

    parts = [
        (
            f"[0:v]scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
            f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"fps={fps},setsar=1,split=2[bare0][base]"
        ),
        # -loop'd image inputs default to 25fps - force match so xfade and
        # concat see identical frame pacing on every branch.
        f"[1:v]fps={fps}[ovsrc]",
        f"[base][ovsrc]overlay=0:0:format=auto[ov0]",
        f"[bare0]split={bare_n}" + "".join(f"[b{i}]" for i in range(bare_n)),
        f"[ov0]split={ov_n}" + "".join(f"[o{i}]" for i in range(ov_n)),
    ]

    bi = oi = 0  # next unused split pad on bare / overlay
    segments = []

    def _take_bare():
        nonlocal bi
        tag = f"b{bi}"
        bi += 1
        return tag

    def _take_ov():
        nonlocal oi
        tag = f"o{oi}"
        oi += 1
        return tag

    def _trim(src, start, end, dst):
        parts.append(f"[{src}]trim=start={start}:end={end},setpts=PTS-STARTPTS[{dst}]")

    if lead_bare:
        src = _take_bare()
        _trim(src, 0, lead_bare, "vlead")
        segments.append("[vlead]")

    bare_in = _take_bare()
    ov_in = _take_ov()
    _trim(bare_in, in_start, in_end, "bare_in")
    _trim(ov_in, in_start, in_end, "ov_in")
    # Cap xfade output to `od` - same reason as build_transition_filter_
    # complex: xfade can emit a few extra frames past duration=.
    parts.append(
        f"[bare_in][ov_in]xfade=transition={overlay_enter}:duration={od}:offset=0,"
        f"trim=duration={od},setpts=PTS-STARTPTS[vin]"
    )
    segments.append("[vin]")

    ov_static = _take_ov()
    _trim(ov_static, in_end, out_start, "vstatic")
    segments.append("[vstatic]")

    ov_out = _take_ov()
    bare_out = _take_bare()
    _trim(ov_out, out_start, out_end, "ov_out")
    _trim(bare_out, out_start, out_end, "bare_out")
    parts.append(
        f"[ov_out][bare_out]xfade=transition={overlay_exit}:duration={od}:offset=0,"
        f"trim=duration={od},setpts=PTS-STARTPTS[vwipeout]"
    )
    segments.append("[vwipeout]")

    if trail_bare:
        src = _take_bare()
        _trim(src, out_end, total_duration, "vtrail")
        segments.append("[vtrail]")

    parts.append(f"{''.join(segments)}concat=n={len(segments)}:v=1:a=0[vout]")
    return ";\n".join(parts)


def build_transition_filter_complex(cw, ch, video_transition, duration, fps=30):
    """
    Pure clip-to-clip crossfade between two adjacent clips' bare footage.
    By the time footage reaches this boundary, each clip has already wiped
    its own overlay off (see build_song_filter_complex) during its own
    encode, using up the `duration` seconds this transition sits between -
    so both sides are plain video here, no overlay involved.

    Inputs: 0 = clip A's video+audio (its last `duration`s), 1 = clip B's
    video+audio (its first `duration`s).
    """
    def _bg(idx, tag):
        # fps is forced here (not just at final encode) because xfade
        # blends clip A's and clip B's video directly - if their source
        # frame rates differ (common across YouTube clips), xfade sees
        # mismatched frame pacing on its two inputs and blends unevenly.
        return (
            f"[{idx}:v]scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
            f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black,fps={fps}[{tag}]"
        )

    return (
        f"{_bg(0, 'bgA')};"
        f"{_bg(1, 'bgB')};"
        # xfade and acrossfade disagree on what happens after the blend
        # window: acrossfade stops sharply at exactly `duration`, but xfade
        # (offset=0) keeps playing clip B's remaining frames past that point
        # instead of ending there. Clip B's demuxed/retimed frame count
        # rarely lands exactly on `duration`, so that tail is usually a few
        # extra frames of unblended clip-B video with no matching audio -
        # audibly, a brief dead-air stutter right at the end of every
        # transition. Trimming [vout] back to `duration` keeps video and
        # audio the same length so the two segments splice cleanly.
        f"[bgA][bgB]xfade=transition={video_transition}:duration={duration}:offset=0,"
        f"trim=duration={duration},setpts=PTS-STARTPTS[vout];"
        f"[0:a][1:a]acrossfade=d={duration}[aout]"
    )


if __name__ == "__main__":
    style = load_style()
    img = build_overlay_image(
        style,
        rank=6, title="설탕 허니 아이스티", artist="BABYMONSTER",
        peak=6, release_date="2023.06.08", months_on_chart=8, views=109_584_467,
        entry_type="new_entry", views_gained=1_234_567, rank_change="—",
    )
    canvas = Image.new("RGB", img.size, (30, 30, 30))
    canvas.paste(img, (0, 0), img)
    canvas.save("assets/templates/_preview.png")
    print("Saved assets/templates/_preview.png")
