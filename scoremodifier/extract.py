"""Extract structured results from a 'JUDGES DETAILS PER SKATER' PDF.

This turns the source report into a list of :class:`~scoremodifier.model.TeamResult`
plus the raw segment line. It reuses the text-anchor geometry from
:func:`scoremodifier.per_skater._collect` (which already locates every team's rank
row) so there is a single source of truth for where the data lives.

The summary row under each team's column header reads, left-to-right::

    Rank  Name...  Nation  StartingNumber  SegScore  ElemScore  PCS  Deductions

Team names contain spaces and the club code never does, so the row is parsed
**from the right**: the last four tokens are the score floats, then the starting
number, then the club code, and everything between the rank and the club code is
the name.
"""

from __future__ import annotations

import re

import fitz  # PyMuPDF

from .model import TeamResult
from .per_skater import NotPerSkaterReport, _collect

_FLOAT_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _parse_float(tok: str) -> float:
    return float(tok)


def _parse_row(tokens: list[str]) -> TeamResult:
    """Parse one summary row's words (left-to-right) into a TeamResult.

    Raises ValueError if the row doesn't have the expected shape.
    """
    # Minimum: rank + name(>=1) + club + starting + 4 score columns = 8 tokens.
    if len(tokens) < 8:
        raise ValueError(f"row too short: {tokens!r}")
    rank = int(tokens[0])
    seg, elem, pcs, ded = (_parse_float(t) for t in tokens[-4:])
    starting = int(tokens[-5])
    club = tokens[-6]
    name = " ".join(tokens[1:-6]).strip()
    if not name:
        raise ValueError(f"empty name in row: {tokens!r}")
    return TeamResult(
        rank=rank,
        name=name,
        club=club,
        starting_number=starting,
        segment_score=seg,
        element_score=elem,
        component_score=pcs,
        deductions=ded,
    )


def _segment_line(doc: "fitz.Document") -> str:
    """The segment label = 2nd non-empty line of page 0 (e.g. 'TULOKKAAT L1 FREE SKATING')."""
    lines = [ln.strip() for ln in doc[0].get_text().splitlines() if ln.strip()]
    return lines[1] if len(lines) > 1 else ""


def extract_results(src: bytes) -> tuple[list[TeamResult], str]:
    """Return ``(teams, segment_line)`` parsed from a per-skater report.

    Raises :class:`NotPerSkaterReport` if no team rows can be parsed.
    """
    doc = fitz.open(stream=src, filetype="pdf")
    try:
        _title, bands, _legend = _collect(doc)
        if not bands:
            raise NotPerSkaterReport(
                "Input does not look like a 'JUDGES DETAILS PER SKATER' report "
                "(no team blocks found)."
            )
        segment = _segment_line(doc)
        teams: list[TeamResult] = []
        for b in bands:
            rb = b["rank_bbox"]
            if rb is None:
                continue
            page = doc[b["page"]]
            y0, y1 = rb[1], rb[3]
            # words sitting on the same line as the rank digit = the summary row
            row = [
                w
                for w in page.get_text("words")
                if w[1] < y1 + 1.5 and w[3] > y0 - 1.5 and b["clip"].y0 <= w[1] <= b["clip"].y1
            ]
            row.sort(key=lambda w: w[0])
            tokens = [w[4] for w in row]
            try:
                teams.append(_parse_row(tokens))
            except (ValueError, IndexError):
                continue
        if not teams:
            raise NotPerSkaterReport(
                "Could not read any team result rows from the report."
            )
        return teams, segment
    finally:
        doc.close()
