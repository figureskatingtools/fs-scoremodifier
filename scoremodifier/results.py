"""Render a polished single-page 'Tulokset' results PDF (Tulokkaat podium style).

Hand-drawn with PyMuPDF primitives (the only PDF dependency, self-contained
wheels) so it deploys unchanged on Azure Functions Flex Consumption. The layout
mirrors the design sketch: a gradient top bar + logo, a title block with a
category pill, a four-cell competition info bar, an info callout, podium cards
(rank <= 3, ties allowed) and a two-column "Muut joukkueet" list of everyone
else in skating order.

The renderer consumes the shared :class:`~scoremodifier.model.ResultsMeta` /
:class:`~scoremodifier.model.TeamResult` data so the HTML renderer and any
future renderer stay in lock-step with it.
"""

from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF

from .model import ResultsMeta, TeamResult, podium_teams, skating_order
from .per_skater import DEFAULT_FOOTER_TEXT

# --- brand palette (matches frontend style.css), 0..1 RGB ------------------
INK = (0.051, 0.122, 0.200)
INK_SOFT = (0.235, 0.318, 0.408)
INK_MUTED = (0.392, 0.475, 0.557)
RINK = (0.071, 0.443, 0.710)
RINK_DEEP = (0.051, 0.353, 0.573)
RINK_TINT = (0.914, 0.945, 0.973)
GOLD = (0.690, 0.553, 0.184)
SILVER = (0.620, 0.667, 0.714)
BRONZE = (0.769, 0.553, 0.388)
LINE = (0.867, 0.902, 0.933)
PAPER = (1.0, 1.0, 1.0)
WHITE = (1.0, 1.0, 1.0)

# gradient stops (0..255 RGB) — cyan -> rink -> violet, the sketch's signature sweep
_GRAD = [(0.0, (40, 180, 210)), (0.5, (18, 113, 181)), (1.0, (109, 63, 181))]
_CARD_GRAD = [(0.0, (16, 86, 150)), (1.0, (95, 55, 165))]  # 1st-place card

# fonts (PyMuPDF built-ins; brand TTFs can be embedded later without API change)
F_BOLD = "hebo"  # Helvetica-Bold (display / headings)
F_REG = "helv"  # Helvetica (body)

# Finnish UI strings — grouped for future i18n.
T_TITLE = "Tulokset"
T_PODIUM = "Palkintosijat"
T_OTHERS = "Muut joukkueet"
T_ORDER = "LUISTELUJÄRJESTYKSESSÄ"
T_PLACE = "SIJA"
T_POINTS = "KOKONAISPISTEET"
T_LBL_COMP = "KILPAILU"
T_LBL_DATE = "PÄIVÄMÄÄRÄ"
T_LBL_VENUE = "PAIKKAKUNTA"
T_LBL_TEAMS = "JOUKKUEITA"
T_NOTE = (
    "Tässä sarjassa julkaistaan ainoastaan palkintosijat (1.-3.) ja niiden "
    "kokonaispisteet. Muut joukkueet on lueteltu luistelujärjestyksessä ilman "
    "sijoituksia ja pisteitä."
)

_LOGO = Path(__file__).with_name("assets") / "logo.png"
_PAGE_W, _PAGE_H = 595.28, 841.89
_M = 46.0  # page margin


# --- gradient helpers -------------------------------------------------------
def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _color_at(stops: list, t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    for i in range(len(stops) - 1):
        p0, c0 = stops[i]
        p1, c1 = stops[i + 1]
        if p0 <= t <= p1:
            f = 0.0 if p1 == p0 else (t - p0) / (p1 - p0)
            return tuple(int(round(_lerp(c0[k], c1[k], f))) for k in range(3))
    return stops[-1][1]


def _gradient_pixmap(stops: list, w: int = 320, h: int = 6) -> "fitz.Pixmap":
    """Opaque horizontal gradient (stretched to fit by insert_image)."""
    cols = [_color_at(stops, x / (w - 1)) for x in range(w)]
    rowbytes = bytes(b for c in cols for b in c)
    return fitz.Pixmap(fitz.csRGB, w, h, rowbytes * h, False)


def _rounded_gradient_pixmap(
    w: int, h: int, stops: list, radius: int
) -> "fitz.Pixmap":
    """RGBA horizontal gradient with rounded-corner transparency, so it reads as
    a rounded card when dropped on the white page."""
    buf = bytearray(w * h * 4)
    cols = [_color_at(stops, x / (w - 1)) for x in range(w)]
    for y in range(h):
        for x in range(w):
            r, g, b = cols[x]
            a = 255
            # rounded-corner alpha mask
            cx = radius if x < radius else (w - 1 - radius if x > w - 1 - radius else x)
            cy = radius if y < radius else (h - 1 - radius if y > h - 1 - radius else y)
            dx, dy = x - cx, y - cy
            if dx * dx + dy * dy > radius * radius:
                a = 0
            o = (y * w + x) * 4
            buf[o], buf[o + 1], buf[o + 2], buf[o + 3] = r, g, b, a
    return fitz.Pixmap(fitz.csRGB, w, h, bytes(buf), True)


# --- text helpers -----------------------------------------------------------
# The built-in Helvetica only encodes Latin-1, so map common typographic glyphs
# down and replace anything else rather than emit a .notdef box.
_TRANS = str.maketrans(
    {"–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"', "…": "...", " ": " "}
)


def _safe(s) -> str:
    return str(s).translate(_TRANS).encode("latin-1", "replace").decode("latin-1")


def _text(page, point, s, **kw):
    return page.insert_text(point, _safe(s), **kw)


def _tbox(page, rect, s, **kw):
    return page.insert_textbox(rect, _safe(s), **kw)


def _fit_size(s: str, font: str, max_w: float, start: float, floor: float = 6.0) -> float:
    size = start
    while size > floor and fitz.get_text_length(s, fontname=font, fontsize=size) > max_w:
        size -= 0.5
    return size


def _draw_value(page, rect, s, font, start, floor, color):
    """Draw wrapping text shrunk to the largest size that fully fits ``rect``.

    insert_textbox renders nothing when the text overflows, so probe on a scratch
    page (greedy wrapping is unpredictable) and step the size down until it fits.
    """
    s = _safe(s)
    size = start
    while size > floor:
        tmp = fitz.open()
        tp = tmp.new_page(width=_PAGE_W, height=_PAGE_H)
        rc = tp.insert_textbox(rect, s, fontname=font, fontsize=size)
        tmp.close()
        if rc >= 0:
            break
        size -= 0.5
    page.insert_textbox(rect, s, fontname=font, fontsize=size, color=color)


def _ellipsize(s: str, font: str, size: float, max_w: float) -> str:
    if fitz.get_text_length(s, fontname=font, fontsize=size) <= max_w:
        return s
    while s and fitz.get_text_length(s + "...", fontname=font, fontsize=size) > max_w:
        s = s[:-1]
    return s + "..."


def _line(page, x0, y0, x1, y1, color=LINE, width=0.8):
    page.draw_line((x0, y0), (x1, y1), color=color, width=width)


def _center_text(page, cx, cy, s, font, size, color):
    """Draw a single line centered on (cx, cy) via baseline placement.

    insert_textbox silently drops short lines when the box is only ~1.4x the
    font size tall, so place by baseline instead for medal digits / pills.
    """
    s = _safe(s)
    w = fitz.get_text_length(s, fontname=font, fontsize=size)
    page.insert_text((cx - w / 2, cy + size * 0.35), s, fontname=font, fontsize=size, color=color)


def _label(page, x, y, s, size=7.0, color=INK_MUTED, font=F_BOLD, tracking=None) -> float:
    """Small uppercase label with controlled letter-tracking; returns the end x."""
    s = _safe(s.upper())
    track = size * 0.14 if tracking is None else tracking
    cx = x
    for ch in s:
        page.insert_text((cx, y), ch, fontname=font, fontsize=size, color=color)
        cx += fitz.get_text_length(ch, fontname=font, fontsize=size) + track
    return cx - track


# --- sections ---------------------------------------------------------------
def _draw_header(page, meta: ResultsMeta) -> float:
    # gradient top bar — keep_proportion=False so the narrow pixmap stretches
    # edge to edge instead of being centered with white gaps on the sides
    page.insert_image(fitz.Rect(0, 0, _PAGE_W, 8), pixmap=_gradient_pixmap(_GRAD),
                      keep_proportion=False)

    # logo top-right
    try:
        logo = fitz.Pixmap(str(_LOGO))
        lw = 116.0
        lh = lw * logo.height / logo.width
        page.insert_image(fitz.Rect(_PAGE_W - _M - lw, 34, _PAGE_W - _M, 34 + lh), pixmap=logo)
    except Exception:
        pass

    _label(page, _M, 52, meta.supertitle, size=8.0, color=RINK)
    _text(page, (_M, 96), meta.title, fontname=F_BOLD, fontsize=42, color=INK)

    # category pill (gradient, rounded)
    if meta.category:
        cat = meta.category.upper()
        pw = fitz.get_text_length(cat, fontname=F_BOLD, fontsize=10.5) + 30
        ph = 24.0
        py = 108.0
        pix = _rounded_gradient_pixmap(int(pw * 2), int(ph * 2), _CARD_GRAD, int(ph))
        page.insert_image(fitz.Rect(_M, py, _M + pw, py + ph), pixmap=pix)
        _center_text(page, _M + pw / 2, py + ph / 2, cat, F_BOLD, 10.5, WHITE)
    return 150.0


def _draw_info_bar(page, meta: ResultsMeta, y: float) -> float:
    h = 56.0
    box = fitz.Rect(_M, y, _PAGE_W - _M, y + h)
    page.draw_rect(box, color=LINE, fill=None, width=1.0, radius=0.06)
    cells = [
        (T_LBL_COMP, meta.competition),
        (T_LBL_DATE, meta.date),
        (T_LBL_VENUE, meta.venue),
        (T_LBL_TEAMS, str(meta.team_count)),
    ]
    cw = (box.width) / len(cells)
    for i, (lbl, val) in enumerate(cells):
        cx = box.x0 + i * cw
        if i:
            page.draw_line((cx, y + 9), (cx, y + h - 9), color=LINE, width=0.8)  # divider
        _label(page, cx + 12, y + 16, lbl, size=6.5)
        _draw_value(page, fitz.Rect(cx + 12, y + 22, cx + cw - 8, y + h - 4),
                    val or "-", F_BOLD, 11.5, 6.5, INK)
    return y + h + 16


def _draw_callout(page, y: float) -> float:
    pad = 12.0
    inner_w = _PAGE_W - 2 * _M - 22 - pad
    # measure wrapped height
    tmp = fitz.open()
    tp = tmp.new_page(width=_PAGE_W, height=_PAGE_H)
    used = tp.insert_textbox(
        fitz.Rect(0, 0, inner_w, 200), T_NOTE, fontname=F_REG, fontsize=8.5, color=INK_SOFT
    )
    tmp.close()
    text_h = 200 - used
    h = max(40.0, text_h + 2 * pad)
    box = fitz.Rect(_M, y, _PAGE_W - _M, y + h)
    page.draw_rect(box, color=None, fill=RINK_TINT, radius=0.12)
    page.draw_rect(fitz.Rect(_M, y, _M + 3.5, y + h), color=None, fill=RINK)  # accent bar
    page.draw_circle((box.x0 + 16, y + pad + 6), 6.5, color=None, fill=RINK)
    _text(page, (box.x0 + 13.7, y + pad + 9), "i", fontname=F_BOLD, fontsize=9, color=WHITE)
    _tbox(page, fitz.Rect(box.x0 + 30, y + pad, box.x1 - pad, box.y1 - 2),
          T_NOTE, fontname=F_REG, fontsize=8.5, color=INK_SOFT)
    return y + h + 22


def _section_heading(page, y: float, title: str, suffix: str = "") -> float:
    _text(page, (_M, y), title, fontname=F_BOLD, fontsize=15, color=INK)
    tw = fitz.get_text_length(title, fontname=F_BOLD, fontsize=15)
    x = _M + tw + 12
    if suffix:
        x = _label(page, x, y - 1, suffix, size=7.5, color=INK_MUTED) + 12
    _line(page, x, y - 4, _PAGE_W - _M, y - 4, color=LINE, width=1.0)
    return y + 14


def _medal_color(rank: int) -> tuple:
    return {1: GOLD, 2: SILVER, 3: BRONZE}.get(rank, RINK)


def _draw_podium(page, teams: list[TeamResult], y: float) -> float:
    if not teams:
        return y
    n = len(teams)
    cols = min(n, 3)
    gap = 14.0
    cw = (_PAGE_W - 2 * _M - (cols - 1) * gap) / cols
    ch = 156.0
    for i, t in enumerate(teams):
        col = i % cols
        row = i // cols
        x = _M + col * (cw + gap)
        cy = y + row * (ch + gap)
        card = fitz.Rect(x, cy, x + cw, cy + ch)
        highlight = t.rank == 1
        if highlight:
            pix = _rounded_gradient_pixmap(int(cw * 2), int(ch * 2), _CARD_GRAD, 22)
            page.insert_image(card, pixmap=pix)
            fg, sub, faint = WHITE, (0.85, 0.89, 0.96), (0.78, 0.83, 0.92)
        else:
            page.draw_rect(card, color=LINE, fill=PAPER, width=1.0, radius=0.08)
            fg, sub, faint = INK, INK_SOFT, INK_MUTED
        pad = 16.0
        # medal circle + place
        page.draw_circle((x + pad + 11, cy + pad + 11), 13, color=None, fill=_medal_color(t.rank))
        _center_text(page, x + pad + 11, cy + pad + 11, str(t.rank), F_BOLD, 13,
                     WHITE if t.rank != 2 else INK)
        _label(page, x + pad + 32, cy + pad + 15, T_PLACE, size=8.0, color=faint)
        # team name (wrap up to 2 lines, shrink to fit)
        name_size = _fit_size(t.name, F_BOLD, cw - 2 * pad, 17.0, floor=11.0)
        _tbox(page, fitz.Rect(x + pad, cy + 48, x + cw - pad, cy + 96),
              t.name, fontname=F_BOLD, fontsize=name_size, color=fg)
        _text(page, (x + pad, cy + 110), t.club, fontname=F_REG, fontsize=9.5, color=sub)
        # score + label
        _text(page, (x + pad, cy + ch - 24), f"{t.segment_score:.2f}",
              fontname=F_BOLD, fontsize=24, color=fg)
        _label(page, x + pad, cy + ch - 11, T_POINTS, size=6.5, color=faint)
    rows = (n + cols - 1) // cols
    return y + rows * ch + (rows - 1) * gap + 24


def _draw_others(page, teams: list[TeamResult], y: float) -> float:
    if not teams:
        return y
    gap = 22.0
    cw = (_PAGE_W - 2 * _M - gap) / 2
    rows = (len(teams) + 1) // 2
    rh = 23.0
    for i, t in enumerate(teams):
        # column-major: fill the left column top-to-bottom, then the right one,
        # so skating order reads down each column rather than zig-zagging
        col = i // rows
        row = i % rows
        x = _M + col * (cw + gap)
        ry = y + row * rh
        if row % 2 == 0:
            page.draw_rect(fitz.Rect(x - 4, ry - 2, x + cw + 4, ry + rh - 4),
                           color=None, fill=(0.972, 0.980, 0.988))
        num = str(t.starting_number)
        _text(page, (x, ry + 12), num, fontname=F_BOLD, fontsize=10.5, color=RINK)
        nx = x + 26
        club_w = fitz.get_text_length(t.club, fontname=F_REG, fontsize=9) + 4
        name = _ellipsize(t.name, F_BOLD, 10.5, cw - 26 - club_w - 6)
        _text(page, (nx, ry + 12), name, fontname=F_BOLD, fontsize=10.5, color=INK)
        _tbox(page, fitz.Rect(x, ry, x + cw, ry + 16), t.club,
              fontname=F_REG, fontsize=9, color=INK_MUTED, align=fitz.TEXT_ALIGN_RIGHT)
    return y + rows * rh + 12


def _draw_footer(page):
    y = _PAGE_H - 34
    _line(page, _M, y, _PAGE_W - _M, y, color=LINE, width=0.8)
    _tbox(page, fitz.Rect(_M, y + 6, _PAGE_W - _M, y + 22),
          DEFAULT_FOOTER_TEXT, fontname=F_REG, fontsize=8, color=INK_MUTED,
          align=fitz.TEXT_ALIGN_CENTER)


def render_results_pdf(meta: ResultsMeta, teams: list[TeamResult]) -> bytes:
    """Render the one-page results summary PDF and return its bytes."""
    if not meta.team_count:
        meta.team_count = len(teams)
    doc = fitz.open()
    page = doc.new_page(width=_PAGE_W, height=_PAGE_H)

    y = _draw_header(page, meta)
    y = _draw_info_bar(page, meta, y)
    y = _draw_callout(page, y)
    y = _section_heading(page, y, T_PODIUM)
    y = _draw_podium(page, podium_teams(teams), y)
    y = _section_heading(page, y, T_OTHERS, T_ORDER)
    _draw_others(page, skating_order(teams), y)
    _draw_footer(page)

    out = doc.tobytes(garbage=4, deflate=True)
    doc.close()
    return out
