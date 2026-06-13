"""Render the podium-only ``CAT###RS.htm`` page in the native Swiss-Timing /
FSM result-page format.

This is the second renderer over the shared
:class:`~scoremodifier.model.TeamResult` / :class:`~scoremodifier.model.ResultsMeta`
data (the PDF renderer in :mod:`scoremodifier.results` is the first). Unlike the
PDF, this file is meant to drop straight into the official results directory
beside the real ``CAT###RS.htm`` pages, so it reproduces their exact markup
(``../Styles.css`` / ``../Print.css``, the ``evt_header.jpg`` banner, the
``../flags/<ABBR>.GIF`` nation flags and the standard footer) rather than the
branded figureskatingtools styling. Per the publishing convention it lists only
the **podium** competitors (ranks 1-3) with their total points.

The only data-driven parts are the page ``<title>``, the category caption, the
"last update" stamp and the result rows; everything else is the template the
results service emits verbatim.
"""

from __future__ import annotations

from datetime import datetime
from html import escape

from .model import ResultsMeta, TeamResult, podium_teams


def _last_update() -> str:
    """A 'DD.MM.YYYY HH:MM (UTC +HH:MM)' stamp in Finnish local time, matching
    the format the live results service prints (DST-aware via zoneinfo)."""
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("Europe/Helsinki"))
        off = now.strftime("%z")  # e.g. +0300
        off = f"{off[:3]}:{off[3:]}" if len(off) == 5 else off
        return f"{now.strftime('%d.%m.%Y %H:%M')} (UTC {off})"
    except Exception:
        return f"{datetime.utcnow().strftime('%d.%m.%Y %H:%M')} (UTC)"


def _row(t: TeamResult, idx: int) -> str:
    """One result row. Rows alternate Line1White / Line2White; the nation cell
    shows the ``../flags/<ABBR-UPPERCASE>.GIF`` flag next to the abbreviation as
    printed in the report. FPl. and FS both equal the (single-segment) rank."""
    cls = "Line1White" if idx % 2 == 0 else "Line2White"
    abbr = t.club.strip()
    flag = abbr.upper()
    return f"""                            <tr class="{cls}">
                                <td>{t.rank}</td>
                                <td class="CellLeft"><a class="disableBiosLink">{escape(t.name)}</a></td>
                                <td><table><tr class="{cls}"><td><img src="../flags/{escape(flag)}.GIF"></td><td></td><td>{escape(abbr)}</td></tr></table></td>
                                <td>{t.segment_score:.2f}</td>
                                    <td>{t.rank}</td>
                            </tr>"""


def render_results_html(meta: ResultsMeta, teams: list[TeamResult]) -> str:
    """Return the podium-only ``CAT###RS.htm`` page in Swiss-Timing markup."""
    podium = podium_teams(teams)
    rows = "\n".join(_row(t, i) for i, t in enumerate(podium))
    cat = meta.category_full or meta.category  # proper-case name for the caption
    category = escape(cat or meta.title or "")
    title = escape(" - ".join(p for p in (meta.competition, cat) if p) or "Result")
    year = datetime.utcnow().year
    return f"""<html>
<head>
    <title>{title}</title>
    <meta Http-Equiv="refresh" Content="30">
    <link href="../Styles.css" rel="stylesheet" type="text/css" media="screen">
    <link href="../Print.css" rel="stylesheet" type="text/css" media="print">
    <link rel="shortcut icon" type="image/png" href="../favicon_clear.png">
    <style>
        .disableBiosLink {{
            pointer-events: none;
            cursor: default;
            color: inherit;
            text-decoration: inherit;
        }}
    </style>
</head>
<body class="PageBody">
    <script src="/results/jquery.js" type="text/javascript"></script>
    <script src="/results/default.js" type="text/javascript"></script>
    <table class="MainTab" border="0" cellpadding="0" cellspacing="0">
<tr><td><a href="https://www.isu-skating.com/"><img src="evt_header.jpg" border="0"></a></td></tr><tr class="EmptyLine14"><td>&nbsp;</td></tr>
<tr class="caption2"><td>{category}</td></tr>
<tr class="EmptyLine14"><td> &nbsp; </td></tr>
<tr class="caption3"><td>Result</td></tr>
<tr class="EmptyLine14"><td> &nbsp; </td></tr>
<tr>
    <td>
        <table width="70%" border="0" align="center" cellpadding="0" cellspacing="1" bgcolor="#606060">
            <tr>
                <td>
                    <table width="100%" align="center" border="0" cellspacing="1">
                        <tr class="TabHeadWhite">
                            <th>FPl.</th>
                            <th>Name</th>
                                                            <th>Nation</th>
                            <th>Points</th>

                                <th>FS</th>
                        </tr>

{rows}

                    </table>
                </td>
            </tr>
        </table>
    </td>
</tr><tr class="EmptyLine22"><td> &nbsp; </td></tr>
<tr class="Link"><td><a href="index.htm">Back to Event Page</a> &nbsp; &nbsp; <a href="javascript:history.go (-1)">Back</a> &nbsp; &nbsp; <a href="https://www.isu-skating.com/">Back to Home Page</a> &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp;<a href='mailto:figureskating@st-sportservice.com'>Contact result service</a> &nbsp; &nbsp; <a href="http://www.st-sportservice.com">Created by Swiss Timing, Ltd.</a></td></tr>
<tr class="EmptyLine10"><td> &nbsp; </td></tr>
<tr class="LastLine"><td>Last Update: {_last_update()} <br /> &copy; {year} <a href="https://www.isu-skating.com/">International Skating Union</a>. All Rights Reserved.</td></tr>
</table>
</body>
</html>"""
