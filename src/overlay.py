import json


def load_style(path="assets/templates/style.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _esc(text):
    """Escape a value for use inside an ffmpeg drawtext filter string."""
    text = str(text)
    text = text.replace("\\", "\\\\")
    text = text.replace("'",  "\\'")
    text = text.replace(":",  "\\:")
    return text


def _font(path):
    """Escape a Windows font path for the ffmpeg filter graph (C: -> C\\:)."""
    return path.replace(":/", "\\:/")


def build_vf(style, *, rank, title, artist, peak, years_on_chart, views, is_new_entry=False):
    """
    Build and return the full ffmpeg -vf filter string for the bottom-bar overlay.

    Layout (matches reference screenshot):
      Top row  — RANK.  Title | Artist
      Bot row  — PEAK: X   YEARS ON CHART: X   VIEWS: X,XXX,XXX
      Right    — NEW ENTRY badge (when is_new_entry=True)
    """
    fb = _font(style["font_bold"])
    fr = _font(style["font_regular"])
    bh = style["bar"]["height"]
    bc = style["bar"]["color"]

    r  = style["rank"]
    t  = style["title"]
    s  = style["stats"]
    b  = style["new_entry_badge"]

    # Pre-escape all dynamic text values
    rank_esc  = _esc(rank)
    title_esc = _esc(f"{title} | {artist}")
    peak_esc  = _esc(peak)
    years_esc = _esc(years_on_chart)
    views_esc = _esc(f"{int(views):,}")

    parts = [
        # ── Background bar ──────────────────────────────────────────────────
        f"drawbox=x=0:y=ih-{bh}:w=iw:h={bh}:color={bc}:t=fill",

        # ── Top row ─────────────────────────────────────────────────────────
        # Rank number (gold)
        (f"drawtext=fontfile='{fb}'"
         f":text='{rank_esc}.'"
         f":x={r['x']}:y=H-{bh}+{r['y_offset']}"
         f":fontsize={r['fontsize']}:fontcolor={r['color']}"),

        # Title | Artist (white)
        (f"drawtext=fontfile='{fb}'"
         f":text='{title_esc}'"
         f":x={t['x']}:y=H-{bh}+{t['y_offset']}"
         f":fontsize={t['fontsize']}:fontcolor={t['color']}"),

        # ── Stats row ───────────────────────────────────────────────────────
        # PEAK label + value
        (f"drawtext=fontfile='{fr}'"
         f":text='PEAK\\:'"
         f":x={s['peak']['label_x']}:y=H-{bh}+{s['y_offset']}"
         f":fontsize={s['peak']['fontsize']}:fontcolor={s['peak']['label_color']}"),
        (f"drawtext=fontfile='{fb}'"
         f":text='{peak_esc}'"
         f":x={s['peak']['value_x']}:y=H-{bh}+{s['y_offset']}"
         f":fontsize={s['peak']['fontsize']}:fontcolor={s['peak']['value_color']}"),

        # YEARS ON CHART label + value
        (f"drawtext=fontfile='{fr}'"
         f":text='YEARS ON CHART\\:'"
         f":x={s['years']['label_x']}:y=H-{bh}+{s['y_offset']}"
         f":fontsize={s['years']['fontsize']}:fontcolor={s['years']['label_color']}"),
        (f"drawtext=fontfile='{fb}'"
         f":text='{years_esc}'"
         f":x={s['years']['value_x']}:y=H-{bh}+{s['y_offset']}"
         f":fontsize={s['years']['fontsize']}:fontcolor={s['years']['value_color']}"),

        # VIEWS label + value
        (f"drawtext=fontfile='{fr}'"
         f":text='VIEWS\\:'"
         f":x={s['views']['label_x']}:y=H-{bh}+{s['y_offset']}"
         f":fontsize={s['views']['fontsize']}:fontcolor={s['views']['label_color']}"),
        (f"drawtext=fontfile='{fb}'"
         f":text='{views_esc}'"
         f":x={s['views']['value_x']}:y=H-{bh}+{s['y_offset']}"
         f":fontsize={s['views']['fontsize']}:fontcolor={s['views']['value_color']}"),
    ]

    # ── NEW ENTRY badge ──────────────────────────────────────────────────────
    # Rendered as a solid rectangle + two text lines.
    # Swap in a PNG overlay later for the circle look.
    if is_new_entry:
        bx_str = b["x"]   # e.g. "iw-210"
        by_str = b["y"]   # e.g. "ih-130"
        bw     = b["width"]   # 190
        bht    = b["height"]  # 130

        # Pre-compute numeric offsets in Python so the ffmpeg expression is a
        # simple linear form like "iw-115-tw/2" — avoids the parser treating
        # a leading "(" as a function-call token.
        bx_num = int(bx_str.replace("iw-", ""))  # 210
        by_num = int(by_str.replace("ih-", ""))  # 130

        # drawtext uses W/H for video dims and w/h for text dims — not iw/ih/tw/th
        cx   = f"W-{bx_num - bw // 2}"      # horizontal centre: "W-115"
        ly1  = f"H-{by_num - bht // 4}"     # upper line:  "H-98"
        ly2  = f"H-{by_num // 2 - 4}"       # lower line:  "H-61"

        parts += [
            f"drawbox=x={bx_str}:y={by_str}:w={bw}:h={bht}:color={b['bg_color']}:t=fill",
            (f"drawtext=fontfile='{fb}':text='NEW'"
             f":x={cx}-w/2:y={ly1}"
             f":fontsize={b['fontsize']}:fontcolor={b['text_color']}"),
            (f"drawtext=fontfile='{fb}':text='ENTRY'"
             f":x={cx}-tw/2:y={ly2}"
             f":fontsize={b['fontsize']}:fontcolor={b['text_color']}"),
        ]

    return ",".join(parts)


if __name__ == "__main__":
    style = load_style()
    vf = build_vf(
        style,
        rank=216, title="Magnetic Moon", artist="Tiffany Young",
        peak=216, years_on_chart=1, views=8_416_794, is_new_entry=True,
    )
    # Print each filter on its own line for readability
    for part in vf.split(","):
        print(part)
