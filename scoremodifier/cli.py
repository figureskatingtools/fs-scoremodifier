"""Command-line interface for the score modifier tools.

Each tool is a subcommand so more can be added later:

    python -m scoremodifier per-skater example.pdf -o out.pdf [--hide-non-podium-ranks]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .extract import extract_results
from .index_meta import fetch_index_html, match_category, parse_index_html
from .model import ResultsMeta
from .per_skater import DEFAULT_FOOTER_TEXT, split_per_skater
from .results import render_results_pdf
from .results_html import render_results_html


def _cmd_per_skater(args: argparse.Namespace) -> int:
    src = Path(args.input).read_bytes()
    result = split_per_skater(
        src,
        hide_non_podium_ranks=args.hide_non_podium_ranks,
        podium_cutoff=args.podium_cutoff,
        repeat_legend=not args.no_legend,
        footer_text=args.footer_text,
    )
    out = Path(args.output)
    out.write_bytes(result)
    print(f"Wrote {out} ({len(result):,} bytes)")
    return 0


def _cmd_results(args: argparse.Namespace) -> int:
    src = Path(args.input).read_bytes()
    teams, segment = extract_results(src)

    competition, date, venue, category = args.competition, args.date, args.venue, args.category
    cat_file = args.cat_file
    category_full = ""
    if args.index_url:
        idx = parse_index_html(fetch_index_html(args.index_url))
        competition = competition or idx.competition
        date = date or idx.date
        venue = venue or idx.venue
        matched = match_category(idx, segment)
        if matched:
            category = category or matched.name
            cat_file = cat_file or matched.cat_file
        selected = next((c for c in idx.categories if c.cat_file == cat_file), None) or matched
        if selected:
            category_full = selected.name

    meta = ResultsMeta(
        competition=competition or "",
        date=date or "",
        venue=venue or "",
        category=category or segment,
        supertitle=args.supertitle,
        team_count=len(teams),
        category_full=category_full,
    )

    out = Path(args.output)
    out.write_bytes(render_results_pdf(meta, teams))
    print(f"Wrote {out} ({out.stat().st_size:,} bytes)")

    html_out = args.html_out or (cat_file if cat_file else None)
    if html_out:
        hp = Path(html_out)
        hp.write_text(render_results_html(meta, teams), encoding="utf-8")
        print(f"Wrote {hp} ({hp.stat().st_size:,} bytes)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scoremodifier",
        description="Tools to reshape figure skating result PDFs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ps = sub.add_parser(
        "per-skater",
        help="Split a 'JUDGES DETAILS PER SKATER' report into one team per page.",
    )
    ps.add_argument("input", help="Path to the source PDF (e.g. example.pdf)")
    ps.add_argument(
        "-o",
        "--output",
        required=True,
        help="Path to write the generated PDF",
    )
    ps.add_argument(
        "--hide-non-podium-ranks",
        action="store_true",
        help="Remove the rank number for teams ranked beyond the podium cutoff.",
    )
    ps.add_argument(
        "--podium-cutoff",
        type=int,
        default=3,
        help="Highest rank that keeps its number when hiding ranks (default: 3).",
    )
    ps.add_argument(
        "--no-legend",
        action="store_true",
        help="Do not repeat the source legend box on each page.",
    )
    ps.add_argument(
        "--footer-text",
        default=DEFAULT_FOOTER_TEXT,
        help="Right-aligned credit line drawn under the footer rule.",
    )
    ps.set_defaults(func=_cmd_per_skater)

    rs = sub.add_parser(
        "results",
        help="Build a polished podium 'Tulokset' results page (PDF + optional CAT###RS.htm).",
    )
    rs.add_argument("input", help="Path to the source per-skater PDF")
    rs.add_argument("-o", "--output", required=True, help="Path to write the results PDF")
    rs.add_argument("--competition", default="", help="Competition name (else from --index-url)")
    rs.add_argument("--date", default="", help="Competition date (else from --index-url)")
    rs.add_argument("--venue", default="", help="Venue/location (else from --index-url)")
    rs.add_argument("--category", default="", help="Category pill text, e.g. 'TULOKKAAT'")
    rs.add_argument(
        "--supertitle",
        default="MUODOSTELMALUISTELU · VAPAAOHJELMA",
        help="Eyebrow line above the title.",
    )
    rs.add_argument(
        "--index-url",
        default="",
        help="Competition index.htm URL to auto-fill name/date/venue + match the CAT page.",
    )
    rs.add_argument(
        "--html-out",
        default="",
        help="Write a podium-only HTML page here (default: the matched CAT###RS.htm name).",
    )
    rs.add_argument(
        "--cat-file",
        default="",
        help="Override the CAT###RS.htm filename for the HTML output.",
    )
    rs.set_defaults(func=_cmd_results)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
