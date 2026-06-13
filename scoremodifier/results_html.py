"""Render a podium-only results page as standalone HTML (the CAT###RS.htm output).

This is the second renderer over the shared
:class:`~scoremodifier.model.TeamResult` / :class:`~scoremodifier.model.ResultsMeta`
data (the PDF renderer in :mod:`scoremodifier.results` is the first). Per the
publishing convention it lists **only podium competitors and their total
points** — no skating-order section.

The output is a single self-contained HTML document (inline CSS, web fonts +
logo by URL) so it can be dropped in beside the official FSM ``CAT###RS.htm``.
"""

from __future__ import annotations

from html import escape

from .model import ResultsMeta, TeamResult, podium_teams

# Brand assets/fonts served from the tool's own domain (keeps the file small;
# the page degrades gracefully if offline).
LOGO_URL = "https://scoremodifier.figureskatingtools.com/logo.png"
_FONTS = (
    "https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,700"
    "&family=Instrument+Sans:wght@400;500;600;700&display=swap"
)

_MEDAL = {1: "#b08d2f", 2: "#9da9b3", 3: "#c48d63"}

_CSS = """
:root{--ink:#0d1f33;--ink-soft:#3c5168;--ink-muted:#64798e;--line:#dde6ee;
--rink:#1271b5;--rink-tint:#e9f1f8;--paper:#fff;--frost:#eef3f7}
*{box-sizing:border-box}
body{margin:0;background:linear-gradient(180deg,#f3f7fa,#eef3f7 38%,#e7eef4);
font-family:'Instrument Sans',system-ui,sans-serif;color:var(--ink);
-webkit-font-smoothing:antialiased;padding:32px 16px}
.sheet{max-width:840px;margin:0 auto;background:var(--paper);border:1px solid var(--line);
border-radius:18px;overflow:hidden;box-shadow:0 12px 32px -8px rgb(13 31 51/.18)}
.bar{height:8px;background:linear-gradient(90deg,#28b4d2,#1271b5 50%,#6d3fb5)}
.pad{padding:34px 40px 40px}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:24px}
.eyebrow{font-size:12px;letter-spacing:.18em;font-weight:700;color:var(--rink);
text-transform:uppercase;margin:0 0 6px}
h1{font-family:'Fraunces',Georgia,serif;font-weight:700;font-size:46px;line-height:1;
margin:0 0 14px;letter-spacing:-.01em}
.logo{height:64px;flex:none}
.pill{display:inline-block;background:linear-gradient(90deg,#105696,#5f37a5);color:#fff;
font-weight:700;font-size:12px;letter-spacing:.12em;text-transform:uppercase;
padding:7px 16px;border-radius:999px}
.info{display:grid;grid-template-columns:repeat(4,1fr);gap:0;border:1px solid var(--line);
border-radius:12px;margin:24px 0 22px;overflow:hidden}
.info div{padding:12px 16px}
.info div+div{border-left:1px solid var(--line)}
.info dt{font-size:10px;letter-spacing:.14em;font-weight:700;color:var(--ink-muted);
text-transform:uppercase;margin:0 0 4px}
.info dd{margin:0;font-weight:700;font-size:15px}
.note{display:flex;gap:12px;background:var(--rink-tint);border-left:4px solid var(--rink);
border-radius:10px;padding:14px 16px;color:var(--ink-soft);font-size:13.5px;line-height:1.45}
.note .i{flex:none;width:20px;height:20px;border-radius:50%;background:var(--rink);color:#fff;
font-weight:700;font-size:13px;display:flex;align-items:center;justify-content:center}
h2{font-family:'Fraunces',Georgia,serif;font-size:24px;margin:26px 0 14px;display:flex;
align-items:center;gap:14px}
h2::after{content:"";flex:1;height:1px;background:var(--line)}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.card{border:1px solid var(--line);border-radius:16px;padding:20px;background:var(--paper);
display:flex;flex-direction:column;min-height:180px}
.card.win{background:linear-gradient(135deg,#105696,#5f37a5);border:none;color:#fff}
.card .head{display:flex;align-items:center;gap:10px;margin-bottom:14px}
.medal{width:30px;height:30px;border-radius:50%;display:flex;align-items:center;
justify-content:center;font-weight:700;font-size:15px;color:#fff}
.place{font-size:12px;letter-spacing:.14em;font-weight:700;text-transform:uppercase;
color:var(--ink-muted)}
.card.win .place{color:#cdd9ee}
.team{font-family:'Fraunces',Georgia,serif;font-weight:700;font-size:22px;line-height:1.12;
margin:0 0 4px}
.club{font-size:14px;color:var(--ink-soft);margin:0}
.card.win .club{color:#cdd9ee}
.score{font-family:'Fraunces',Georgia,serif;font-weight:700;font-size:34px;margin:auto 0 2px}
.plabel{font-size:10px;letter-spacing:.14em;font-weight:700;text-transform:uppercase;
color:var(--ink-muted)}
.card.win .plabel{color:#cdd9ee}
.foot{text-align:center;color:var(--ink-muted);font-size:12.5px;margin:30px 0 0;
padding-top:16px;border-top:1px solid var(--line)}
@media(max-width:640px){.info,.cards{grid-template-columns:1fr}.info div+div{border-left:none;
border-top:1px solid var(--line)}.pad{padding:24px}}
"""

_NOTE = (
    "Tässä sarjassa julkaistaan ainoastaan palkintosijat (1.–3.) ja niiden "
    "kokonaispisteet."
)
_FOOTER = "Created with Figureskatingtools.com — Supporting the Figure Skating Community."


def _card(t: TeamResult) -> str:
    win = " win" if t.rank == 1 else ""
    return (
        f'<div class="card{win}">'
        f'<div class="head"><span class="medal" style="background:{_MEDAL.get(t.rank, "#1271b5")}">'
        f"{t.rank}</span><span class=\"place\">Sija</span></div>"
        f'<p class="team">{escape(t.name)}</p>'
        f'<p class="club">{escape(t.club)}</p>'
        f'<div class="score">{t.segment_score:.2f}</div>'
        f'<div class="plabel">Kokonaispisteet</div>'
        f"</div>"
    )


def render_results_html(meta: ResultsMeta, teams: list[TeamResult]) -> str:
    """Return a self-contained podium-only HTML results page (CAT###RS.htm)."""
    if not meta.team_count:
        meta.team_count = len(teams)
    podium = podium_teams(teams)
    cards = "".join(_card(t) for t in podium)
    info = "".join(
        f"<div><dt>{escape(lbl)}</dt><dd>{escape(str(val) or '—')}</dd></div>"
        for lbl, val in (
            ("Kilpailu", meta.competition),
            ("Päivämäärä", meta.date),
            ("Paikkakunta", meta.venue),
            ("Joukkueita", meta.team_count),
        )
    )
    title = escape(f"{meta.title} — {meta.category} — {meta.competition}".strip(" —"))
    return f"""<!DOCTYPE html>
<html lang="fi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="{_FONTS}" rel="stylesheet">
<style>{_CSS}</style></head>
<body><div class="sheet"><div class="bar"></div><div class="pad">
<div class="top"><div>
<p class="eyebrow">{escape(meta.supertitle)}</p>
<h1>{escape(meta.title)}</h1>
<span class="pill">{escape(meta.category)}</span>
</div><img class="logo" src="{LOGO_URL}" alt="Figureskatingtools.com"></div>
<dl class="info">{info}</dl>
<div class="note"><span class="i">i</span><span>{escape(_NOTE)}</span></div>
<h2>{escape("Palkintosijat")}</h2>
<div class="cards">{cards}</div>
<p class="foot">{escape(_FOOTER)}</p>
</div></div></body></html>"""
