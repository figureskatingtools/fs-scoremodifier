"""Shared data model for the results tools.

Everything the renderers need is captured here once, so the PDF renderer
(:mod:`scoremodifier.results`), the HTML renderer (:mod:`scoremodifier.results_html`)
and any future renderer (per-team e-mails, full category pages) all consume the
*same* extracted data and never re-parse the source PDF.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TeamResult:
    """One team/skater row from a 'JUDGES DETAILS PER SKATER' report."""

    rank: int
    name: str
    club: str  # club/nation abbreviation as printed (e.g. "EVT"); full names are unavailable
    starting_number: int
    segment_score: float
    element_score: float = 0.0
    component_score: float = 0.0
    deductions: float = 0.0


@dataclass
class ResultsMeta:
    """Competition-level metadata for the results page.

    ``competition``/``date``/``venue`` come from the competition ``index.htm``;
    ``category``/``supertitle`` are derived from the PDF segment line (editable by
    the operator). ``team_count`` is derived from the extracted teams.
    """

    competition: str
    date: str
    venue: str
    category: str  # PDF pill text, e.g. "TULOKKAAT"
    supertitle: str  # e.g. "MUODOSTELMALUISTELU · VAPAAOHJELMA"
    team_count: int = 0
    title: str = "Tulokset"
    # Proper-case category name from the index (e.g. "Tulokkaat L1"), used for
    # the native CAT###RS.htm caption; falls back to ``category`` when unset.
    category_full: str = ""


def podium_teams(teams: list[TeamResult], cutoff: int = 3) -> list[TeamResult]:
    """Teams on the podium (rank <= cutoff), rank order. Handles shared ranks:
    ties simply yield more (or fewer) than three entries."""
    return sorted((t for t in teams if t.rank <= cutoff), key=lambda t: (t.rank, t.starting_number))


def skating_order(teams: list[TeamResult], cutoff: int = 3) -> list[TeamResult]:
    """Non-podium teams (rank > cutoff) in skating order (starting number asc)."""
    return sorted((t for t in teams if t.rank > cutoff), key=lambda t: t.starting_number)
