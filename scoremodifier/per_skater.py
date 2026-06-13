"""Split an FSM "JUDGES DETAILS PER SKATER" report into one team per page.

The input is a multi-page Figure Skating Manager (FSM) "JUDGES DETAILS PER
SKATER" PDF where each page stacks 2-3 self-contained team blocks under a
repeating header. This tool slices every team block out of the source and places
it, pixel-identical, onto its own page beneath a repeated copy of the header,
then replaces the original ``Page X / Y`` footer with a horizontal rule and a
right-aligned credit line. Optionally the rank number is removed for teams
outside the podium.

Each placed region is first **trimmed** (everything outside it physically
redacted on a throwaway copy) before being stamped, so an output page contains
*only* its own visible content -- no other team's data lingers as hidden,
extractable text. The public entry point is :func:`split_per_skater`, a pure
``bytes -> bytes`` transform so it can be reused unchanged inside an Azure
Function (blob in, blob out) the way the judgepapers PDF modules are.
"""

from __future__ import annotations

import re

import fitz  # PyMuPDF

DEFAULT_FOOTER_TEXT = (
    "Created with Figureskatingtools.com - Supporting the Figure Skating Community."
)

# --- layout constants (PDF points) -----------------------------------------
_HEADER_ABOVE = 15.0  # how far above a "Rank" anchor to look for the column-header top
_RANK_LINE_BELOW = 7.6  # the "Rank" word sits on the middle header line; include it
_CLIP_TOP_PAD = 1.0  # hairline kept above a block's column header
_BPAD = 3.0  # padding kept below a block's last line of content
_LPAD = 3.0  # padding around the legend box
_FOOTER_RULE_FROM_BOTTOM = 28.0  # y of the footer rule, measured up from the page bottom
_FOOTER_TEXT_GAP = 2.0  # gap between the footer rule and the credit text
_FOOTER_FONTSIZE = 8.0
_FOOTER_RULE_WIDTH = 0.6
_LEGEND_GAP = 8.0  # gap between the legend's bottom and the footer rule
_RANK_REDACT_FILL = (1, 1, 1)  # white

_INT_RE = re.compile(r"^\d+$")
_REDACT_KEEP_LINE_ART = getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0)
_REDACT_KEEP_IMAGE = getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0)


class NotPerSkaterReport(ValueError):
    """Raised when the input doesn't look like a per-skater report."""


def _content_margins(page: "fitz.Page") -> tuple[float, float]:
    """Left/right x of the printed content, used to size the footer rule."""
    words = page.get_text("words")
    if not words:
        return 24.0, page.rect.width - 24.0
    return min(w[0] for w in words), max(w[2] for w in words)


def _header_top(words: list, rank_y: float) -> float:
    """Top y of the 3-line column header whose middle line holds the 'Rank' word."""
    above = [
        w[1]
        for w in words
        if rank_y - _HEADER_ABOVE <= w[1] <= rank_y + _RANK_LINE_BELOW
    ]
    return min(above) if above else rank_y


def _collect(doc: "fitz.Document"):
    """Locate the title band, every team band, and the legend band.

    Returns ``(title_rect, bands, legend)`` where ``bands`` is a list (in rank
    order) of dicts ``{"page", "clip", "rank_bbox"}`` and ``legend`` is
    ``(page_index, rect)`` or ``None``. All rectangles are in source PDF points;
    nothing is hardcoded -- everything is found via text anchors so the tool
    works for any category/segment and any number of teams.
    """
    title_rect: fitz.Rect | None = None
    bands: list[dict] = []
    legend: tuple[int, fitz.Rect] | None = None

    for pno in range(doc.page_count):
        page = doc[pno]
        W, H = page.rect.width, page.rect.height
        words = page.get_text("words")  # (x0, y0, x1, y1, text, block, line, word)

        rank_tops = sorted(r.y0 for r in page.search_for("Rank"))  # one per team
        ee_tops = sorted(r.y0 for r in page.search_for("Executed Elements"))
        footer_top = min(
            (r.y0 for r in page.search_for("Page")), default=H - 30.0
        )  # the "Page X / Y" footer
        if not rank_tops:
            continue

        header_tops = [_header_top(words, r) for r in rank_tops]
        if title_rect is None:
            # everything above the first team's column header = the title band
            title_rect = fitz.Rect(0, 0, W, header_tops[0])

        ends = header_tops[1:] + [footer_top]  # next block's header top, or the footer
        for i, rank_y in enumerate(rank_tops):
            ht, end = header_tops[i], ends[i]
            blk = [w for w in words if ht - 0.5 <= w[1] < end]
            if not blk:
                continue
            content_bottom = max(w[3] for w in blk)
            clip = fitz.Rect(
                0,
                max(0.0, ht - _CLIP_TOP_PAD),
                W,
                min(end - 0.5, content_bottom + _BPAD),
            )
            # rank digit = leftmost bare integer on the summary data row, i.e.
            # between this block's "Rank" header and its "Executed Elements" row
            ee_top = next((y for y in ee_tops if y > rank_y), end)
            ints = [w for w in blk if rank_y + 1.0 < w[1] < ee_top and _INT_RE.match(w[4])]
            rank_bbox = min(ints, key=lambda w: w[0])[:4] if ints else None
            bands.append({"page": pno, "clip": clip, "rank_bbox": rank_bbox})

        if legend is None:
            leg = page.search_for("Legend")
            if leg:
                ltop = min(r.y0 for r in leg)
                lwords = [
                    w for w in words if w[1] >= ltop - 0.5 and w[3] <= footer_top + 0.5
                ]
                lbottom = max((w[3] for w in lwords), default=ltop + 14.0)
                legend = (pno, fitz.Rect(0, ltop - _LPAD, W, lbottom + _LPAD))

    return title_rect, bands, legend


def _trim_page(doc: "fitz.Document", page_index: int, keep: "fitz.Rect") -> "fitz.Document":
    """Return a 1-page doc = copy of ``page_index`` with everything outside ``keep`` removed.

    The full-width strips above and below ``keep`` are redacted away (text,
    graphics and images), so the resulting page carries *only* the kept region's
    content -- nothing else survives as hidden text once it is stamped.
    """
    tmp = fitz.open()
    tmp.insert_pdf(doc, from_page=page_index, to_page=page_index)
    page = tmp[0]
    W, H = page.rect.width, page.rect.height
    for strip in (fitz.Rect(0, 0, W, keep.y0), fitz.Rect(0, keep.y1, W, H)):
        if strip.height > 0.1:
            page.add_redact_annot(strip)
    page.apply_redactions()
    return tmp


def split_per_skater(
    src: bytes,
    *,
    hide_non_podium_ranks: bool = False,
    podium_cutoff: int = 3,
    repeat_legend: bool = True,
    footer_text: str = DEFAULT_FOOTER_TEXT,
) -> bytes:
    """Return a new PDF with one team per page.

    Args:
        src: Raw bytes of the source "JUDGES DETAILS PER SKATER" PDF.
        hide_non_podium_ranks: If True, remove the rank number for every team
            ranked beyond ``podium_cutoff`` (kept for the podium). Pages stay in
            rank order; all scores are kept.
        podium_cutoff: Highest rank that keeps its number (default 3).
        repeat_legend: If True, repeat the source legend box on every page.
        footer_text: Right-aligned credit line drawn under the footer rule.

    Returns:
        The generated PDF as bytes.
    """
    doc = fitz.open(stream=src, filetype="pdf")
    scratch: list[fitz.Document] = []  # trimmed docs kept alive until tobytes()
    try:
        title_rect, bands, legend = _collect(doc)
        if title_rect is None or not bands:
            raise NotPerSkaterReport(
                "Input does not look like a 'JUDGES DETAILS PER SKATER' report "
                "(no team blocks found)."
            )

        W, H = doc[0].rect.width, doc[0].rect.height
        left, right = _content_margins(doc[0])

        # Redact the rank digit on the source pages BEFORE trimming, so the number
        # is truly removed (not just covered) in the copied content. Keep table
        # rules / images intact -- only the glyph goes.
        if hide_non_podium_ranks:
            per_page: dict[int, list] = {}
            for idx, b in enumerate(bands):
                if idx + 1 > podium_cutoff and b["rank_bbox"] is not None:
                    per_page.setdefault(b["page"], []).append(b["rank_bbox"])
            for pno, boxes in per_page.items():
                page = doc[pno]
                for box in boxes:
                    page.add_redact_annot(fitz.Rect(box), fill=_RANK_REDACT_FILL)
                page.apply_redactions(
                    images=_REDACT_KEEP_IMAGE, graphics=_REDACT_KEEP_LINE_ART
                )

        # Trim the shared header and legend once.
        header_doc = _trim_page(doc, 0, title_rect)
        scratch.append(header_doc)
        legend_doc = None
        if repeat_legend and legend is not None:
            legend_doc = _trim_page(doc, legend[0], legend[1])
            scratch.append(legend_doc)

        out = fitz.open()
        title_h = title_rect.height
        footer_rule_y = H - _FOOTER_RULE_FROM_BOTTOM
        legend_rect = legend[1] if legend is not None else None

        for b in bands:
            clip = b["clip"]
            band_doc = _trim_page(doc, b["page"], clip)
            scratch.append(band_doc)

            op = out.new_page(width=W, height=H)
            # repeated title, 1:1 at the original top position
            op.show_pdf_page(title_rect, header_doc, 0, clip=title_rect)
            # the team band, placed right under the title (1:1 -> identical pixels)
            target = fitz.Rect(clip.x0, title_h, clip.x1, title_h + clip.height)
            op.show_pdf_page(target, band_doc, 0, clip=clip)
            # legend anchored just above the footer rule
            if legend_doc is not None and legend_rect is not None:
                lh = legend_rect.height
                ly0 = footer_rule_y - _LEGEND_GAP - lh
                ltarget = fitz.Rect(legend_rect.x0, ly0, legend_rect.x1, ly0 + lh)
                op.show_pdf_page(ltarget, legend_doc, 0, clip=legend_rect)
            # new footer: horizontal rule + right-aligned credit
            op.draw_line(
                (left, footer_rule_y),
                (right, footer_rule_y),
                color=(0, 0, 0),
                width=_FOOTER_RULE_WIDTH,
            )
            op.insert_textbox(
                fitz.Rect(
                    left,
                    footer_rule_y + _FOOTER_TEXT_GAP,
                    right,
                    footer_rule_y + _FOOTER_TEXT_GAP + 14.0,
                ),
                footer_text,
                fontsize=_FOOTER_FONTSIZE,
                align=fitz.TEXT_ALIGN_RIGHT,
                color=(0, 0, 0),
            )

        return out.tobytes(garbage=4, deflate=True)
    finally:
        for d in scratch:
            d.close()
        doc.close()
