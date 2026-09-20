"""The scoremodifier package itself on real FSM exports, including a golden
comparison against outputs a deployed backend produced (``<sample>/output/``)."""

import re

import pytest

from conftest import page_sizes, page_texts, rank_anchor_count, stable_lines, teams_or_none

from scoremodifier.extract import extract_results
from scoremodifier.model import ResultsMeta, podium_teams, skating_order
from scoremodifier.per_skater import NotPerSkaterReport, split_per_skater
from scoremodifier.results import render_results_pdf
from scoremodifier.results_html import render_results_html


def by_rank(teams):
    return sorted(teams, key=lambda t: (t.rank, t.starting_number))


def test_not_a_report_is_rejected(synthetic_pdf):
    with pytest.raises(NotPerSkaterReport):
        split_per_skater(synthetic_pdf, hide_non_podium_ranks=True)
    with pytest.raises(NotPerSkaterReport):
        extract_results(synthetic_pdf)


def test_extract_results_is_a_ranked_table(team_sample_pdf):
    teams, segment = extract_results(team_sample_pdf)
    assert segment and teams
    ranks = [t.rank for t in teams]
    assert ranks == sorted(ranks) and ranks[0] == 1
    assert set(ranks) == set(range(1, max(ranks) + 1))  # contiguous, ties allowed
    scores = [t.segment_score for t in teams]
    assert scores == sorted(scores, reverse=True)
    assert len({t.starting_number for t in teams}) == len(teams)
    assert all(t.name and t.club for t in teams)
    assert len(podium_teams(teams)) >= min(3, len(teams))
    assert {t.name for t in podium_teams(teams)} | {t.name for t in skating_order(teams)} == {t.name for t in teams}


def test_split_gives_each_team_its_own_page(sample_pdf):
    out = split_per_skater(sample_pdf, hide_non_podium_ranks=False)
    texts = page_texts(out)
    # One page per block, each page keeps the report title and exactly one column header.
    assert len(texts) == rank_anchor_count(sample_pdf)
    assert rank_anchor_count(out) == len(texts)
    for t in texts:
        assert "JUDGES DETAILS PER SKATER" in t.upper()

    teams = teams_or_none(sample_pdf)
    if teams is None:
        return
    assert len(texts) == len(teams)
    for i, t in enumerate(by_rank(teams)):
        assert t.name in texts[i], f"page {i} should hold {t.name}"
        others = [o.name for o in teams if o.name != t.name]
        assert not any(o in texts[i] for o in others), f"page {i} leaks another team"


def test_hide_non_podium_ranks_only_touches_ranks_above_cutoff(sample_pdf):
    teams = teams_or_none(sample_pdf)
    if teams is None:
        pytest.skip("rank check needs parseable result rows")
    shown = page_texts(split_per_skater(sample_pdf, hide_non_podium_ranks=False))
    hidden = page_texts(split_per_skater(sample_pdf, hide_non_podium_ranks=True))
    for text_s, text_h, t in zip(shown, hidden, by_rank(teams)):
        if t.rank <= 3:
            assert stable_lines(text_s) == stable_lines(text_h)
        else:
            assert len(stable_lines(text_h)) < len(stable_lines(text_s))
            # The rank number is gone from the page, every score is still there.
            assert f"{t.segment_score:.2f}" in text_h


def test_renderers_consume_the_same_extraction(team_sample_pdf):
    teams, segment = extract_results(team_sample_pdf)
    meta = ResultsMeta(competition="Cup", date="01.01.2026", venue="Rink, Town", category="TULOKKAAT",
                       supertitle="X", team_count=len(teams), category_full="Tulokkaat L1")
    pdf = render_results_pdf(meta, teams)
    html = render_results_html(meta, teams)
    assert page_sizes(pdf) == [(595, 842)]
    text = page_texts(pdf)[0]
    assert "TULOKKAAT" in text and "Cup" in text
    assert "<title>Cup - Tulokkaat L1</title>" in html
    # One result row per podium entry, each holding a nested flag-cell <tr> of the same class.
    assert html.count('<tr class="Line') == 2 * len(podium_teams(teams))
    # "DD.MM.YYYY HH:MM (UTC +03:00)" in Finnish local time; plain "(UTC)" where no tzdata is installed.
    assert re.search(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2} \(UTC( [+-]\d{2}:\d{2})?\)", html)


def test_matches_golden_output_from_deployed_backend(sample):
    """Whatever the deployed backend produced for this source must be what we
    produce now: same page count and sizes, same text apart from time stamps."""
    src = (sample / "source.pdf").read_bytes()
    golden = sample / "output" / "per-skater.pdf"
    if not golden.is_file():
        pytest.skip("no per-skater golden for this sample")
    gold = golden.read_bytes()
    candidates = {flag: split_per_skater(src, hide_non_podium_ranks=flag) for flag in (True, False)}
    gold_lines = [stable_lines(t) for t in page_texts(gold)]
    matches = {
        flag: page_sizes(out) == page_sizes(gold) and [stable_lines(t) for t in page_texts(out)] == gold_lines
        for flag, out in candidates.items()
    }
    assert any(matches.values()), (
        f"neither includeRanks setting reproduces the golden output; page counts "
        f"{[len(page_texts(o)) for o in candidates.values()]} vs {len(gold_lines)}"
    )


def test_results_golden_has_every_team(sample):
    golden = sample / "output" / "results.pdf"
    if not golden.is_file():
        pytest.skip("no results golden for this sample")
    teams, _ = extract_results((sample / "source.pdf").read_bytes())
    gold = golden.read_bytes()
    text = "\n".join(page_texts(gold))
    assert page_sizes(gold) == [(595, 842)]
    for t in teams:
        assert t.name in text
