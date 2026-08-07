# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A figureskatingtools.com tool (sibling of `../fs-judgepapers`, landing site `../figureskatingtools-site`)
that reshapes figure skating result PDFs. Both tools take the same Figure Skating Manager (FSM)
**"Judges Details Per Skater"** export (each page stacks 2–3 skaters/teams under a repeating header):

- **per-skater** rebuilds it so **each skater gets their own page** (rank order), keeping the original
  header + legend, replacing the `Page X / Y` footer with a credit line, and optionally removing the
  rank number for everyone outside the podium.
- **results** (Tulokkaat podium style) builds a polished one-page **"Tulokset"** summary PDF — branded
  header, competition info bar, podium cards (ranks 1–3, ties allowed, with total scores) and a
  two-column "Muut joukkueet" list of everyone else in **skating order** (no rank/score) — **and** a
  podium-only `CAT###RS.htm` HTML page. Competition name/date/venue + the matching `CAT###RS.htm`
  filename are pulled from the competition `index.htm` URL (parsed server-side).

Currently used for the **Tulokkaat (Beginners)** category. The frontend is a single page with two tabs
(one shared PDF upload): per-skater (tick *Include ranks*, off by default) and results summary (paste
the index.htm URL → auto-fill, Generate → PDF + HTML).

> **Architecture note:** the results tool is split into an *extraction layer*
> (`scoremodifier/extract.py` → `model.TeamResult`/`ResultsMeta`) feeding *renderers* (`results.py`
> PDF, `results_html.py` HTML). The PDF is the branded figureskatingtools sheet; the HTML
> (`CAT###RS.htm`) is deliberately **not** branded — it reproduces the **native Swiss-Timing result-page
> markup** (`../Styles.css`/`../Print.css`, `evt_header.jpg`, `../flags/<ABBR-UPPERCASE>.GIF` nation
> flags, standard footer) so it drops straight into the official results directory beside the real
> pages. Its caption uses `ResultsMeta.category_full` (proper-case index name, e.g. "Tulokkaat L1"),
> distinct from `category` (the PDF badge, e.g. "TULOKKAAT"). Planned future renderers (per-team
> SendGrid result emails) reuse the same extracted data instead of re-parsing. `index_meta.py` parses
> the FSM/Swiss-Timing index.htm (stdlib only, no bs4).

## Commands

```bash
# Core PDF logic as a standalone CLI (the canonical implementation)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m scoremodifier per-skater input.pdf -o out.pdf [--hide-non-podium-ranks]
# Results summary PDF + podium-only CAT###RS.htm (name/date/venue + CAT file from the index URL)
python -m scoremodifier results input.pdf -o results.pdf \
  --index-url https://www.figureskatingresults.fi/results/2526/<COMP>/index.htm --category TULOKKAAT \
  [--html-out CAT003RS.htm] [--competition … --date … --venue … --supertitle …]

# Backend (cd infra/functions) — the function imports the repo-root scoremodifier/ package
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
PYTHONPATH=$(git rev-parse --show-toplevel) func start

# Deployment (manual; CI does this automatically — see Branch / Deploy Strategy)
./deploy_infra.sh -g <resource-group> [--proxy-secret <SECRET>]
./deploy_backend.sh -g <resource-group>     # bundles the core package + pip installs, ZIP deploy
```

> **This repo is backend-only.** The UI moved to `figureskatingtools-site`
> (`site/src/scoremodifier/`) and is served at `figureskatingtools.com/scoremodifier/`.
> `frontend/` + `deploy_frontend.sh` are dead weight kept until the old
> `scoremodifier.figureskatingtools.com` Web App is torn down.

No tests and no linter are configured. With the frontend gone, `az bicep build --file infra/main.bicep`
is the only static check in this repo. To exercise the HTTP endpoints locally, run `func start` and
curl them with the proxy headers — see `PROXY-CONTRACT.md`. UI work happens in
`figureskatingtools-site` (`./start_locally.sh` and `frontend/` here are pre-migration leftovers).

> The example FSM PDFs are **not** committed (only `README.md`/source is tracked). Drop a real
> "Judges Details Per Skater" export next to the repo to exercise the tool.

## Architecture

Four pieces:

1. **Core logic** (`scoremodifier/`) — the canonical, deployment-independent implementation. A pure
   `bytes -> bytes` transform: `split_per_skater(src, *, hide_non_podium_ranks, podium_cutoff=3,
   repeat_legend=True, footer_text=…)` in `per_skater.py`, plus a `cli.py` subcommand dispatcher
   (`python -m scoremodifier per-skater …`). Built on **PyMuPDF** (`fitz`, AGPL-3.0). Team blocks, the
   header band and the legend are located at runtime via text anchors (no hardcoded coordinates), so it
   works for any category/segment/team count. Each region is first **trimmed** onto a throwaway page
   (everything outside it redacted) then stamped 1:1 with `show_pdf_page`, preserving the exact original
   appearance while guaranteeing no cross-team text leaks between pages. The input was reverse-engineered
   from FSM exports (PDFsharp; CID font with a +0x1D glyph offset; each team's 3-line column header has
   the `Rank` word on its middle line, so block tops are found `~7pt` above the `Rank` anchor).

2. **Backend** (`infra/functions/`) — Python Azure Functions (Flex Consumption, Python 3.11), HTTP-
   triggered, in `function_app.py`. **`generate`** (POST, body = the PDF,
   `?includeRanks=true|false`, default `false` ⇒ `hide_non_podium_ranks=True`): runs
   `split_per_skater`, derives a name from the PDF (`<segment> — <printed date>`, page-0 text), stores
   `source.pdf` + `output/per-skater.pdf` in blob storage, writes a `competitions` row + a
   `generatedpapers` row with a 5-day read SAS link, and returns the download URL. **`generate_results`**
   (POST, body = the PDF, metadata via query params `competition/date/venue/category/supertitle/catFile/indexUrl`):
   runs `extract_results` → `render_results_pdf` + `render_results_html`, stores `output/results.pdf`
   + `output/<CAT###RS>.htm`, writes a `competitions` row (`Tool="results"`) + a `generatedpapers` row
   per file, returns both URLs. **`parse_index`** (GET `?url=`) fetches + parses the competition
   index.htm (SSRF-guarded to `INDEX_ALLOWED_HOSTS`, default `figureskatingresults.fi`) → name/date/venue
   + category `CAT###RS.htm` list. `check_user_permission` is the frontend auth probe. `list_competitions` / `get_competition_details` / `delete_competition`
   and a daily 30-day auto-delete timer are mirrored from judgepapers for future features / storage
   hygiene (not wired to the current UI). The auth/storage/SAS helpers are reused verbatim from
   judgepapers. **The function imports `scoremodifier.per_skater`**; the canonical package lives at the
   repo root and the build (`deploy_backend.sh` + CI) copies it into the function package — single
   source of truth, no duplication. Locally, `PYTHONPATH=<repo root>` makes the import resolve.

3. **Frontend** — **no longer in this repo.** The UI lives in `figureskatingtools-site`
   (`site/src/scoremodifier/` + `site/scoremodifier/index.html`) and is served at
   `figureskatingtools.com/scoremodifier/` by that repo's single router Web App, which also owns
   Easy Auth, `/userinfo` and the `/scoremodifier/api/*` → Function App proxy. The local `frontend/`
   directory is the pre-migration copy, no longer built or deployed; it is kept only until the old
   `scoremodifier.figureskatingtools.com` Web App is torn down (see `frontend/README.md`).

4. **Infra** (`infra/main.bicep` + `modules/`) — subscription-scoped Bicep, **backend-only**:
   resource group, **own storage account** (`stfsscore…`, container `fs-scoremodifier`, tables
   `competitions` + `generatedpapers`, plus the `app-package` deploy container — no `categories`
   table), Flex Consumption Function App (system-assigned identity, CORS empty), App Insights, and
   RBAC (Blob Data Contributor + Table Data Contributor + Blob Delegator). No Web App, no auth
   managed identity, no DNS and no custom domain — those moved to the site repo. `main.bicep` exports
   `functionAppName` + `functionPrincipalId`, which the site repo consumes as its
   `FUNCTION_APP_URL_SCOREMODIFIER` / `TOOL_PRINCIPAL_ID_SCOREMODIFIER` environment values. Per-env
   params in `infra/parameters/{test,prod}.bicepparam`; CI passes `resourceGroupName` and
   `proxySharedSecret` inline from the GitHub environment.

### Auth chain (important when touching any endpoint) — see `PROXY-CONTRACT.md`

All function routes are `AuthLevel.ANONYMOUS`; real auth is Entra ID Easy Auth on the **router Web App
in the site repo**. Easy Auth injects `X-MS-CLIENT-PRINCIPAL*` there → the router forwards the email as
`X-Forwarded-User-Email` to this Function App → `get_user_email_from_header()` resolves it (direct
header → forwarded header → base64 principal → Bearer JWT). Every endpoint calls it and returns 401 on
`None`. **The Function App itself must be `AllowAnonymous`** (`function.bicep` `globalValidation`), not
`requireAuthentication`, or Easy Auth would 401 the proxied requests (which carry only the email header,
no bearer token); no identity provider is configured on it at all any more. Because the function is
public, a **shared secret** stops spoofing: the router holds `PROXY_SHARED_SECRET_SCOREMODIFIER` and
sends it as `X-Proxy-Secret`, and `_proxy_secret_ok()` rejects mismatches against this app's
`PROXY_SHARED_SECRET`. Enforced only when the env var is set (local/dev fail open). `is_user_allowed()`
allows all authenticated users (hook for a future allowlist).

### Storage layout & data model

Storage account is dedicated to this tool. Container `fs-scoremodifier`:
- `{sanitized-name}-{id}/source.pdf` — the uploaded report
- `{sanitized-name}-{id}/output/per-skater.pdf` — the generated result
Tables: `competitions` (PartitionKey `GLOBAL`, RowKey = 8-char hex id; `Name`, `FolderPath`, `Segment`,
`PrintedDate`, `IncludeRanks`, `OutputPages`, `Visible`, `CreatedBy/CreatedDate`, `DeletionDate`,
counters) and `generatedpapers` (PartitionKey = competition id; SAS download links). Names are auto-
derived from the PDF and are **not** unique — the id is the identifier. Every storage helper supports
two credential modes: managed identity when `AzureWebJobsStorage__accountName` is set (prod; SAS via
user-delegation key), connection string via `AzureWebJobsStorage` otherwise (local; SAS via account key).

## Site integration

`figureskatingtools-site` owns everything user-facing: the tool's UI (`site/src/scoremodifier/`), its
nav entry (`DEFAULT_TOOLS` in `packages/shared-ui/src/nav.ts`, path `/scoremodifier/`), and the
changelog feed entry in `site/public/changelog-sources.json` (`tool: "Score Modifier"`, with a
`.changelog-badge--score-modifier` rule in `site/src/style.css`).

This repo only has to hand two values to the site repo's GitHub environments after an infra deploy
(both are printed in the workflow's job summary): `FUNCTION_APP_URL_SCOREMODIFIER` (from
`functionAppName`) and `TOOL_PRINCIPAL_ID_SCOREMODIFIER` (from `functionPrincipalId`). The
`PROXY_SHARED_SECRET` value must match the site repo's `PROXY_SHARED_SECRET_SCOREMODIFIER` for the
same environment.

## Branch / Deploy Strategy

`test` → `main` promote via PRs (squash). `.github/workflows/deploy.yml` deploys **infra (Bicep) then
backend (Functions)** to the matching GitHub environment: **push to `main` auto-deploys prod**;
**`test` is manual-only** via `workflow_dispatch` (run the workflow from the branch whose code you
want, pick the environment). There is no frontend job any more — the UI ships from
`figureskatingtools-site`. `main` is protected (PR required); `test` is protected from deletion.

> **`workflow_dispatch` lives on the default branch.** GitHub only exposes manual dispatch for workflows
> present on the **default branch** (`main`). The whole project currently lives on `test`; `main` is just
> the initial commit, so `gh workflow run` 404s until `test` is promoted. Bootstrap workaround: temporarily
> set the repo default branch to `test`, dispatch, then set it back to `main` (the dispatch trick is only
> needed until the first `test`→`main` promotion, which is itself the first **prod** deploy).

> **No Entra config lives here any more.** Redirect URIs, the federated identity credential and the
> auth app registration all belong to the router Web App in `figureskatingtools-site`.
> `create_auth_app.sh` is a leftover from the standalone era and is not used.

### One-time setup (per environment)

1. GitHub Environment (`test` / `prod`) **secrets**: `AZURE_CLIENT_ID` (OIDC deploy principal — the
   judgepapers deploy app can be reused by adding an `fs-scoremodifier` federated-credential subject),
   `PROXY_SHARED_SECRET`. **Variables**: `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `LOCATION`,
   `RESOURCE_GROUP_NAME`. (`AUTH_CLIENT_ID`, `AUTH_APP_OBJECT_ID` and `CUSTOM_DOMAIN` are obsolete —
   delete them once the old Web App is gone.)
2. Run the deploy workflow (`test` via `workflow_dispatch`).
3. Copy the job summary's `functionAppName` / `functionPrincipalId` into the `figureskatingtools-site`
   repo's matching environment as `FUNCTION_APP_URL_SCOREMODIFIER` / `TOOL_PRINCIPAL_ID_SCOREMODIFIER`,
   and mirror `PROXY_SHARED_SECRET` there as `PROXY_SHARED_SECRET_SCOREMODIFIER`.
