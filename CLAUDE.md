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
# Core PDF logic as a standalone CLI (the canonical implementation). Use Python 3.13 —
# the Function App's runtime — so local output matches the backend (uv fetches it:
# `uv venv --python 3.13 .venv`).
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m scoremodifier per-skater input.pdf -o out.pdf [--hide-non-podium-ranks]
# Results summary PDF + podium-only CAT###RS.htm (name/date/venue + CAT file from the index URL)
python -m scoremodifier results input.pdf -o results.pdf \
  --index-url https://www.figureskatingresults.fi/results/2526/<COMP>/index.htm --category TULOKKAAT \
  [--html-out CAT003RS.htm] [--competition … --date … --venue … --supertitle …]

# Backend (cd infra/functions) — the function imports the repo-root scoremodifier/ package.
# Python 3.13 only: azure-functions 2.x does not install on older interpreters. The 3.13
# proxy worker rebuilds sys.path and ignores PYTHONPATH, so copy (or symlink) the package
# next to function_app.py for a local host.
python3.13 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
ln -s ../../scoremodifier scoremodifier && func start   # the symlink is gitignored

# Deployment (manual; CI does this automatically — see Branch / Deploy Strategy)
./deploy_infra.sh -g <resource-group> [--proxy-secret <SECRET>] [--platform-storage <ACCOUNT>] [--orphan-purge true|false]
# --orphan-purge omitted = keep the deployed ORPHAN_PURGE_ENABLED (false if unreadable), never the Bicep default true
./deploy_backend.sh -g <resource-group>     # bundles the core package + pip installs (needs python3.13 or PYTHON=…), ZIP deploy
```

> **This repo is backend-only.** The UI moved to `figureskatingtools-site`
> (`site/src/scoremodifier/`) and is served at `figureskatingtools.com/scoremodifier/`.
> The pre-migration `frontend/`, `deploy_frontend.sh` and `start_locally.sh` were removed once
> the old `scoremodifier.figureskatingtools.com` Web App was torn down.

## Tests

```bash
# pytest suite for the backend handlers + the core package (uv fetches the interpreter)
cd infra/functions && PYTHONPATH=$(git rev-parse --show-toplevel) \
  uv run --python 3.13 --with-requirements requirements-dev.txt python -m pytest tests -q
```

`infra/functions/tests/` runs the real `function_app` handlers against dict-backed blob/table
fakes (`conftest.py`), the SAS path with a dummy account key, the proxy auth contract, the
auto-delete sweep and e-mail scrubbing, the platform retention clock (`test_platform_clock.py`,
against an in-memory platform registry from the `platform` fixture; the clock is off in every
other test), and the `scoremodifier` package on real FSM exports. Sample exports are **not
committed** (skaters' names): the suite reads `SCOREMODIFIER_SAMPLE_DIR` (default
`../fs-samples/scoremodifier`, one directory per competition holding `source.pdf` and optionally
`output/per-skater.pdf` / `output/results.pdf` as goldens — exactly the tool's own storage layout,
so `az storage blob download-batch` from the test account produces it) and skips those tests when
it is missing. Singles-category exports split fine but have no parseable team row, so the
results-tool tests skip them (known limitation, not a bug). `pytest.ini` turns
`DeprecationWarning` into errors for our own code. No linter is configured;
`az bicep build --file infra/main.bicep` is the only other static check. The deploy bundle strips
`tests/`, `requirements-dev.txt` and `pytest.ini`. To exercise the HTTP endpoints against a real host, run
`func start` and curl them with the proxy headers — see `PROXY-CONTRACT.md`. UI work happens in
`figureskatingtools-site` (run its router + Vite dev server pointed at this `func start` instance).

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

2. **Backend** (`infra/functions/`) — Python Azure Functions (Flex Consumption, Python 3.13,
   `azure-functions` 2.x — the 2.x line is 3.13+ only and API-identical to 1.x), HTTP-
   triggered, in `function_app.py`. **`generate`** (POST, body = the PDF,
   `?includeRanks=true|false`, default `false` ⇒ `hide_non_podium_ranks=True`): runs
   `split_per_skater`, derives a name from the PDF (`<segment> — <printed date>`, page-0 text), stores
   `source.pdf` + `output/per-skater.pdf` in blob storage, writes a `competitions` row + a
   `generatedpapers` row (the blob name — no link is stored), and returns a fresh 15-minute read SAS URL. **`generate_results`**
   (POST, body = the PDF, metadata via query params `competition/date/venue/category/supertitle/catFile/indexUrl`):
   runs `extract_results` → `render_results_pdf` + `render_results_html`, stores `output/results.pdf`
   + `output/<CAT###RS>.htm`, writes a `competitions` row (`Tool="results"`) + a `generatedpapers` row
   per file, returns fresh 15-minute URLs for both. Both generators **require** a **`platformId`** query param (the
   site's active competition GUID; normalized; missing/malformed → 400 `platform_id_required`),
   stored as `PlatformId` on the row — see *Retention* below. **`parse_index`** (GET `?url=`) fetches + parses the competition
   index.htm (SSRF-guarded to `INDEX_ALLOWED_HOSTS`, default `figureskatingresults.fi`) → name/date/venue
   + category `CAT###RS.htm` list. `check_user_permission` is the frontend auth probe. `list_competitions` / `get_competition_details` / `delete_competition`
   and the daily auto-delete timer are mirrored from judgepapers for
   future features / storage hygiene (not wired to the current UI). The auth/storage/SAS helpers are reused verbatim from
   judgepapers. **The function imports `scoremodifier.per_skater`**; the canonical package lives at the
   repo root and the build (`deploy_backend.sh` + CI) copies it into the function package — single
   source of truth, no duplication. Locally, `PYTHONPATH=<repo root>` makes the import resolve.

3. **Frontend** — **no longer in this repo.** The UI lives in `figureskatingtools-site`
   (`site/src/scoremodifier/` + `site/scoremodifier/index.html`) and is served at
   `figureskatingtools.com/scoremodifier/` by that repo's single router Web App, which also owns
   Easy Auth, `/userinfo` and the `/scoremodifier/api/*` → Function App proxy.

4. **Infra** (`infra/main.bicep` + `modules/`) — subscription-scoped Bicep, **backend-only**:
   resource group, **own storage account** (`stfsscore…`, container `fs-scoremodifier`, tables
   `competitions` + `generatedpapers`, plus the `app-package` deploy container — no `categories`
   table), Flex Consumption Function App (system-assigned identity, CORS empty, `PLATFORM_STORAGE_ACCOUNT`
   app setting from the `platformStorageAccountName` param, `ORPHAN_PURGE_ENABLED` from
   `orphanPurgeEnabled`), App Insights on a 90-day Log Analytics workspace, and
   RBAC (Blob Data Contributor + Table Data Contributor + Blob Delegator). No Web App, no auth
   managed identity, no DNS and no custom domain — those moved to the site repo. `main.bicep` exports
   `functionAppName` + `functionPrincipalId`, which the site repo consumes as its
   `FUNCTION_APP_URL_SCOREMODIFIER` / `TOOL_PRINCIPAL_ID_SCOREMODIFIER` environment values. Per-env
   params in `infra/parameters/{test,prod}.bicepparam`; CI passes `resourceGroupName` and
   `proxySharedSecret` inline from the GitHub environment.

### Auth chain (important when touching any endpoint) — see `PROXY-CONTRACT.md`

All function routes are `AuthLevel.ANONYMOUS`; real auth is Entra ID Easy Auth on the **router Web App
in the site repo**. The router decodes the Easy Auth principal and forwards it as
`x-forwarded-user-tid` / `-oid` (the identity, lowercase GUIDs), `-email` (display/`CreatedBy` only),
`-name` (URI-encoded) and `-admin: 1` for admins → `get_user()` builds a `User` from those headers
**only** (no `X-MS-CLIENT-PRINCIPAL*`, no Bearer JWT; missing/non-GUID tid or oid → `None`). Every
endpoint but `health` calls it and returns 401 on `None`. **The Function App itself must be
`AllowAnonymous`** (`function.bicep` `globalValidation`), not `requireAuthentication`, or Easy Auth
would 401 the proxied requests; no identity provider is configured on it at all any more. Because the
function is public, a **shared secret** stops spoofing: the router holds
`PROXY_SHARED_SECRET_SCOREMODIFIER` and sends it as `X-Proxy-Secret`, and `_proxy_secret_ok()` compares
it with `hmac.compare_digest` against this app's `PROXY_SHARED_SECRET` — **fail closed**: an unset/empty
secret rejects everything (local `func start` needs one too). `is_user_allowed()`
allows all authenticated users (hook for a future allowlist).

**Competition access** (`platform_access.py`, **byte-identical** with fs-judgepapers and
fs-protocolgenerator — its docstring is the contract): `role_for(platformId, tid, oid, is_admin)` reads
the platform's `useridentities` + `competitionaccess` tables (30 s in-process cache; admin = owner, no
lookup). `generate` / `generate_results` need **write** on `platformId` (checked right after the
400 `platform_id_required` check, before the upload is read); `get_competition_details` needs **read**
(it lists the generated files with a fresh 15-minute read SAS each, minted after the check — `_ReadLinks`,
one user-delegation key per response) and `delete_competition` (**POST only**, id from `?id=` or JSON
`{"id"}`) **owner** on the record's `PlatformId`; `list_competitions` returns only records
the caller can read (admin: all); records without a `PlatformId` are admin-only; `parse_index` is
sign-in only. No role / unknown record → 404 `platform_not_found`, read on a write route → 403
`platform_read_only`, read/write on an owner route → 403 `platform_owner_required`, tables unreadable (incl. `PLATFORM_STORAGE_ACCOUNT` unset) → 503
`platform_unavailable` — JSON `{"error": …}`. Tests: `tests/test_auth.py`, `tests/test_access.py` (the
autouse `access` fixture in `conftest.py` fakes both tables; by default the test user owns everything).

### Storage layout & data model

Storage account is dedicated to this tool. Container `fs-scoremodifier`:
- `{sanitized-name}-{id}/source.pdf` — the uploaded report
- `{sanitized-name}-{id}/output/per-skater.pdf` — the generated result
Tables: `competitions` (PartitionKey `GLOBAL`, RowKey = 8-char hex id; `Name`, `FolderPath`, `Segment`,
`PrintedDate`, `IncludeRanks`, `OutputPages`, `Visible`, `CreatedBy/CreatedDate`, `DeletionDate`,
counters, optional `PlatformId`) and `generatedpapers` (PartitionKey = competition id; `BlobName`, `FileName`,
`FileSize`, `Description` — no links: they are minted per response, valid 15 minutes; older rows' stored
5-day `Url` is never returned, only its blob name is used). Names are auto-
derived from the PDF and are **not** unique — the id is the identifier. Every storage helper supports
two credential modes: managed identity when `AzureWebJobsStorage__accountName` is set (prod; SAS via
user-delegation key), connection string via `AzureWebJobsStorage` otherwise (local; SAS via account key).

### Retention (one platform clock, no tool clock)

The platform registry in `figureskatingtools-site` owns every competition's deletion date (created
+ 60 days, extended a week at a time **only on the site**). This tool has no retention period or
extend action of its own. `infra/functions/platform_clock.py` reads the platform's **`retentionclock`**
table (PK `COMPETITION`, RK GUID; `DeletionDate`, `Status`, `PurgedUtc` — no personal data) on the
platform storage account named by `PLATFORM_STORAGE_ACCOUNT` — **keep that file byte-identical with
fs-judgepapers and fs-protocolgenerator** (its docstring is the contract).

- **New rows:** `platformId` is required (`normalize_platform_id`; 400 `platform_id_required`) and the
  binding is confirmed against the platform before anything is stored: unreadable (outage, missing
  table) → 503 `platform_unavailable`; unknown → 404 `platform_not_found`; deleted/purged → 410
  `platform_deleted`. `DeletionDate` is only a **cached copy** of the platform date (absent while the
  platform has none). With `PLATFORM_STORAGE_ACCOUNT` unset (local dev only) generation proceeds
  without a date, and such rows are never deleted.
- **Sweep** (`auto_delete_expired_competitions` → `_run_cleanup_sweep`, **04:00 UTC**, after the
  platform's 03:00 sweep): one platform table client per run, one read per GUID. Bound rows are
  deleted **only on the platform's verdict** (`platform_says_delete`: `Status == "deleted"` or
  `PurgedUtc` — never a date alone), otherwise their cached date is refreshed; platform unreadable
  or `PLATFORM_STORAGE_ACCOUNT` unset → **skipped with `logging.error`, never deleted**. Legacy rows
  without a `PlatformId` are deleted on their stored date (old 30-day rows keep it;
  `_ensure_deletion_date` backfills `LEGACY_DELETION_DATE` on legacy rows only, in list, details and
  the sweep alike).
- **Orphans** (the platform can no longer decide about them): a live row with no `PlatformId` (or a
  non-GUID one), or whose clock row a *successful* read did not find, is deleted with the same
  `_delete_competition_data` flow (files gone, `CreatedBy` blanked, statistics kept) once it is
  **more than 60 days old** (`CreatedDate`; without one, the newest of `DeletedDate`/the Table
  `Timestamp` — `platform_clock.record_age_reference`); younger orphans are kept with a warning, and
  a read error is never "not found" (skipped). Safety rails: one probe of the clock table per run
  (`platform_clock.orphan_sweep_allowed`) — unreadable, unconfigured, or **zero rows while the tool
  has bound rows** (wrong table/account) → the orphan step is aborted for that run with an error log;
  `ORPHAN_PURGE_ENABLED` (Bicep param `orphanPurgeEnabled`, default on; GitHub var
  `ORPHAN_PURGE_ENABLED=false` = kill switch) off → it only logs "would delete". The sweep returns and
  logs the per-run counts (`deleted`, `skipped`, `failed`, `orphaned`, `orphan_kept`,
  `orphan_dry_run`, `orphan_skipped`).
- **No e-mail addresses on deleted rows:** deletion merges `CreatedBy=""` and
  `DeletedBy="user"|"auto-cleanup"`; every sweep also runs `_scrub_hidden_rows()`, blanking any
  other `CreatedBy`/`DeletedBy` value on `Visible=false` rows. `CreatedBy` is the only user
  identifier this tool stores (no blob metadata, nothing in `generatedpapers`). Logs never carry
  addresses (the no-identity warning lists header names only) and are kept **90 days**:
  `function.bicep` links App Insights to its own Log Analytics workspace
  `log-fs-scoremodifier-<env>` (`retentionInDays: 90`) and sets `RetentionInDays: 90` on the
  component (`logRetentionDays`).

## Site integration

`figureskatingtools-site` owns everything user-facing: the tool's UI (`site/src/scoremodifier/`), its
nav entry (`DEFAULT_TOOLS` in `packages/shared-ui/src/nav.ts`, path `/scoremodifier/`), and the
changelog feed entry in `site/public/changelog-sources.json` (`tool: "Score Modifier"`, with a
`.changelog-badge--score-modifier` rule in `site/src/style.css`).

This repo only has to hand two values to the site repo's GitHub environments after an infra deploy
(both are printed in the workflow's job summary): `FUNCTION_APP_URL_SCOREMODIFIER` (from
`functionAppName`) and `TOOL_PRINCIPAL_ID_SCOREMODIFIER` (from `functionPrincipalId`). The latter is
what grants this app's identity read access to the platform `retentionclock` table (and the
`competition-data` container), so the retention clock works only once the site's infra has been
deployed with it. The
`PROXY_SHARED_SECRET` value must match the site repo's `PROXY_SHARED_SECRET_SCOREMODIFIER` for the
same environment.

## Branch / Deploy Strategy

**Flow (no long-lived `test` branch):** branch from `main` → dispatch `deploy.yml` on that
branch with `environment=test` → PR to `main` → merge (squash) → push to `main` deploys prod →
the branch is auto-deleted on merge. `main` is guarded by a ruleset: PR required, code-owner
review (`.github/CODEOWNERS`: `* @mmaraa`), squash only, the `pytest` check
(`.github/workflows/ci.yml`, every PR) must pass, no force-push/deletion; repository admins can
bypass, which is how the owner merges their own PRs. The `prod` GitHub environment only accepts
deployments from `main`; `test` accepts any branch and holds whichever was deployed last.

`.github/workflows/deploy.yml` runs the **pytest suite
first** (the `test` job; both deploy jobs depend on it, so a red suite blocks the deploy), then
deploys **infra (Bicep) then backend (Functions)** to the matching GitHub environment:
**push to `main` auto-deploys prod**;
**`test` is manual-only** via `workflow_dispatch` (run the workflow from the branch whose code you
want, pick the environment). There is no frontend job any more — the UI ships from
`figureskatingtools-site`.
The runner image is pinned (`runs-on: ubuntu-26.04`, never `ubuntu-latest`) — see the header
comment in the workflow; bump every job together and dispatch to test first. Dependabot
(`.github/dependabot.yml`, read from `main` only) watches `infra/functions` pip and GitHub Actions,
grouped weekly, with PRs against `main` like any feature branch.

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
   `RESOURCE_GROUP_NAME`, `PLATFORM_STORAGE_ACCOUNT` (the site's `stfsplat*` account for that
   environment — same value as fs-judgepapers'; empty = bound rows are never deleted), optional
   `ORPHAN_PURGE_ENABLED` (`false` = the nightly orphan step only logs; unset = on). (`AUTH_CLIENT_ID`, `AUTH_APP_OBJECT_ID` and `CUSTOM_DOMAIN` are obsolete —
   delete them once the old Web App is gone.)
2. Run the deploy workflow (`test` via `workflow_dispatch`).
3. Copy the job summary's `functionAppName` / `functionPrincipalId` into the `figureskatingtools-site`
   repo's matching environment as `FUNCTION_APP_URL_SCOREMODIFIER` / `TOOL_PRINCIPAL_ID_SCOREMODIFIER`,
   and mirror `PROXY_SHARED_SECRET` there as `PROXY_SHARED_SECRET_SCOREMODIFIER`.
