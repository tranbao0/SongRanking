import json


def load_style(path="assets/templates/style.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _esc(text):
    """
    Escape a value for use as an UNQUOTED ffmpeg drawtext option value.

    Order matters: backslash must be escaped first.
    Commas are escaped as \\, because they are the filter-chain separator.
    Single and double quotes are escaped because they toggle ffmpeg's quoted-
    string mode if encountered in an unquoted value.
    """
    text = str(text)
    text = text.replace("\\", "\\\\")
    text = text.replace("'",  "\\'")
    text = text.replace('"',  '\\"')
    text = text.replace(":",  "\\:")
    text = text.replace(",",  "\\,")
    text = text.replace("%",  "%%")   # drawtext treats % as a format-string prefix
    return text


def _font(path):
    """Escape a Windows font path for the ffmpeg filter graph (C: -> C\\:)."""
    return path.replace(":/", "\\:/")


def build_vf(style, *, rank, title, artist, peak, years_on_chart, views,
             entry_type="", views_gained=None, rank_change=""):
    """
    Build and return the full ffmpeg -vf filter string for the bottom-bar overlay.

    Layout:
      Top row  — RANK.  Title | Artist
      Bot row  — PEAK: X   YEARS ON CHART: X   VIEWS: X,XXX,XXX  (+delta)
      Right    — entry badge (new_entry / re_entry / highest_increase / highest_jump)

    All dynamic text= values are UNQUOTED so that \\, is a recognised escape.
    Fixed label strings (PEAK:, VIEWS:, etc.) keep their existing escaping.
    fontfile= paths stay single-quoted since they never contain commas.
    """
    fb = _font(style["font_bold"])
    fr = _font(style["font_regular"])
    bh = style["bar"]["height"]
    bc = style["bar"]["color"]

    r = style["rank"]
    t = style["title"]
    s = style["stats"]

    rank_esc  = _esc(rank)
    display_title = f"{title} | {artist}"
    if len(display_title) > 60:
        display_title = display_title[:58] + ".."
    title_esc = _esc(display_title)
    peak_esc  = _esc(peak)
    years_esc = _esc(years_on_chart)
    views_esc = _esc(f"{int(views):,}")

    parts = [
        # ── Background bar ──────────────────────────────────────────────────
        f"drawbox=x=0:y=ih-{bh}:w=iw:h={bh}:color={bc}:t=fill",

        # ── Top row ─────────────────────────────────────────────────────────
        (f"drawtext=fontfile='{fb}'"
         f":text={rank_esc}."
         f":x={r['x']}:y=H-{bh}+{r['y_offset']}"
         f":fontsize={r['fontsize']}:fontcolor={r['color']}"),

        (f"drawtext=fontfile='{fb}'"
         f":text={title_esc}"
         f":x={t['x']}:y=H-{bh}+{t['y_offset']}"
         f":fontsize={t['fontsize']}:fontcolor={t['color']}"),

        # ── Stats row ───────────────────────────────────────────────────────
        (f"drawtext=fontfile='{fr}'"
         f":text='PEAK\\:'"
         f":x={s['peak']['label_x']}:y=H-{bh}+{s['y_offset']}"
         f":fontsize={s['peak']['fontsize']}:fontcolor={s['peak']['label_color']}"),
        (f"drawtext=fontfile='{fb}'"
         f":text={peak_esc}"
         f":x={s['peak']['value_x']}:y=H-{bh}+{s['y_offset']}"
         f":fontsize={s['peak']['fontsize']}:fontcolor={s['peak']['value_color']}"),

        (f"drawtext=fontfile='{fr}'"
         f":text='YEARS ON CHART\\:'"
         f":x={s['years']['label_x']}:y=H-{bh}+{s['y_offset']}"
         f":fontsize={s['years']['fontsize']}:fontcolor={s['years']['label_color']}"),
        (f"drawtext=fontfile='{fb}'"
         f":text={years_esc}"
         f":x={s['years']['value_x']}:y=H-{bh}+{s['y_offset']}"
         f":fontsize={s['years']['fontsize']}:fontcolor={s['years']['value_color']}"),

        (f"drawtext=fontfile='{fr}'"
         f":text='VIEWS\\:'"
         f":x={s['views']['label_x']}:y=H-{bh}+{s['y_offset']}"
         f":fontsize={s['views']['fontsize']}:fontcolor={s['views']['label_color']}"),
        (f"drawtext=fontfile='{fb}'"
         f":text={views_esc}"
         f":x={s['views']['value_x']}:y=H-{bh}+{s['y_offset']}"
         f":fontsize={s['views']['fontsize']}:fontcolor={s['views']['value_color']}"),
    ]

    # ── Rank change indicator (↑ / ↓ / −) ───────────────────────────────────
    if rank_change:
        rc = style["rank_change"]
        color_map = {"↑": rc["color_up"], "↓": rc["color_down"], "−": rc["color_same"]}
        rc_color  = color_map.get(rank_change, rc["color_same"])
        parts.append(
            f"drawtext=fontfile='{fb}'"
            f":text={_esc(rank_change)}"
            f":x={rc['x']}:y=H-{bh}+{rc['y_offset']}"
            f":fontsize={rc['fontsize']}:fontcolor={rc_color}"
        )

    # ── Views gained delta ───────────────────────────────────────────────────
    if views_gained is not None:
        vd        = style["views_delta"]
        sign      = "+" if views_gained >= 0 else ""
        delta_esc = _esc(f"{sign}{views_gained:,}")
        parts.append(
            f"drawtext=fontfile='{fb}'"
            f":text={delta_esc}"
            f":x={vd['x']}:y=H-{bh}+{s['y_offset']}"
            f":fontsize={vd['fontsize']}:fontcolor={vd['color']}"
        )

    # ── Entry badge (new_entry / re_entry / highest_increase / highest_jump) ─
    badge_types = style.get("badge_types", {})
    if entry_type and entry_type in badge_types:
        bg  = style["badge"]
        bt  = badge_types[entry_type]

        bx_str = bg["x"]
        by_str = bg["y"]
        bw     = bg["width"]
        bht    = bg["height"]

        bx_num = int(bx_str.replace("iw-", ""))
        by_num = int(by_str.replace("ih-", ""))

        cx  = f"W-{bx_num - bw // 2}"
        ly1 = f"H-{by_num - bht // 4}"
        ly2 = f"H-{by_num // 2 - 4}"

        fs     = bt["fontsize"]
        l1_esc = _esc(bt["line1"])
        l2_esc = _esc(bt["line2"])

        parts += [
            f"drawbox=x={bx_str}:y={by_str}:w={bw}:h={bht}:color={bt['bg_color']}:t=fill",
            (f"drawtext=fontfile='{fb}':text={l1_esc}"
             f":x={cx}-tw/2:y={ly1}"
             f":fontsize={fs}:fontcolor={bg['text_color']}"),
            (f"drawtext=fontfile='{fb}':text={l2_esc}"
             f":x={cx}-tw/2:y={ly2}"
             f":fontsize={fs}:fontcolor={bg['text_color']}"),
        ]

    return ",".join(parts)


if __name__ == "__main__":
    style = load_style()
    vf = build_vf(
        style,
        rank=1, title="NewJeans () 'Super Shy' Official MV", artist="HYBE LABELS",
        peak=1, years_on_chart=1, views=290_731_700, entry_type="highest_jump",
        views_gained=500_501, rank_change="↑",
    )
    for part in vf.split(","):
        print(part)
