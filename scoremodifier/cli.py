"""Command-line interface for the score modifier tools.

Each tool is a subcommand so more can be added later:

    python -m scoremodifier per-skater example.pdf -o out.pdf [--hide-non-podium-ranks]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .per_skater import DEFAULT_FOOTER_TEXT, split_per_skater


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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
