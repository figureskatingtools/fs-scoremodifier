# fs-scoremodifier

Score Modifier provides tools to reshape figure skating result PDFs. Part of the
[figureskatingtools.com](https://figureskatingtools.com) ecosystem (sibling of `fs-judgepapers`).

**This repo is the backend.** The UI lives in `figureskatingtools-site` and is served at
[figureskatingtools.com/scoremodifier/](https://figureskatingtools.com/scoremodifier/) by that repo's
shared router Web App, which proxies `/scoremodifier/api/*` to this repo's Azure Function App — see
[PROXY-CONTRACT.md](PROXY-CONTRACT.md).

## What it does

Takes a Figure Skating Manager (FSM) **"Judges Details Per Skater"** export — where each page stacks
2–3 skaters/teams under a repeating header — and rebuilds it so **each skater gets their own page**,
in rank order. Every output page keeps the original report header (banner, title, category/segment),
shows that one skater's full score table pixel-identical to the source, repeats the legend, and
replaces the `Page X / Y` footer with a credit line. Optionally the rank number is removed for
everyone outside the podium. Currently used for the **Tulokkaat (Beginners)** category.

## Architecture

| Layer | Technology |
|---|---|
| **Core logic** | Python + [PyMuPDF](https://pymupdf.readthedocs.io/) (`scoremodifier/`, pure `bytes → bytes`) |
| **Frontend** | Elsewhere — `figureskatingtools-site` (`site/src/scoremodifier/`) |
| **Backend** | Python Azure Functions (Flex Consumption, HTTP) — `generate` endpoint calls the core |
| **Auth** | Microsoft Entra ID Easy Auth on the site router; this app trusts `X-Proxy-Secret` + `X-Forwarded-User-Email` |
| **Storage** | Azure Blob Storage (uploaded + generated PDFs) + Table Storage (`competitions`, `generatedpapers`) |
| **Infra** | Azure Bicep (subscription-scoped), own storage account — storage + Function App + RBAC only |

The tool page is a single view: upload the PDF, tick **Include ranks** (off by default — non-podium
ranks are hidden), click **Generate**, download the result. Each run is persisted (source + output
in blob storage, a competition + paper row in tables) so later features can build on the data. The
site router forwards the user's email plus a shared secret; the Function App is anonymous but rejects
requests without the secret — the full contract is in [PROXY-CONTRACT.md](PROXY-CONTRACT.md).

## Core tool (standalone / CLI)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m scoremodifier per-skater input.pdf -o out.pdf [--hide-non-podium-ranks]
```

The Azure Function imports `scoremodifier.per_skater.split_per_skater` directly; the deploy build
copies the repo-root `scoremodifier/` package into the function package (single source of truth).

## Deployment

CI (`.github/workflows/deploy.yml`) deploys infra (Bicep) → backend on **push to `main` (prod)** or
**manual `workflow_dispatch` (test)**. There is no frontend job — the UI ships from
`figureskatingtools-site`.

Manual:
```bash
./deploy_infra.sh -g <resource-group> [--proxy-secret <SECRET>]
./deploy_backend.sh -g <resource-group>
```

### One-time setup
1. Create GitHub Environments `test` and `prod` with:
   - **Secrets:** `AZURE_CLIENT_ID` (OIDC deploy principal), `PROXY_SHARED_SECRET`
   - **Variables:** `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `LOCATION`, `RESOURCE_GROUP_NAME`
2. After the first infra deploy, copy the job summary's `functionAppName` / `functionPrincipalId` into
   the `figureskatingtools-site` repo's matching environment as `FUNCTION_APP_URL_SCOREMODIFIER` /
   `TOOL_PRINCIPAL_ID_SCOREMODIFIER`, and mirror `PROXY_SHARED_SECRET` there as
   `PROXY_SHARED_SECRET_SCOREMODIFIER`.

> **`workflow_dispatch` caveat:** GitHub only exposes manual dispatch for workflows that exist on the
> **default branch** (`main`). Until `test` is promoted to `main`, dispatching the test deploy needs
> `deploy.yml` present on the default branch — e.g. temporarily set the default branch to `test`,
> dispatch, then set it back. The first push to `main` auto-deploys **prod**.

No DNS records, custom domain, Web App or Entra app registration are managed here any more; they moved
to `figureskatingtools-site` with the frontend.

## Local development

```bash
cd infra/functions
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
PYTHONPATH=$(git rev-parse --show-toplevel) func start   # :7071
```

Requires an Azurite/real storage connection in `infra/functions/local.settings.json`. Call the
endpoints with the proxy headers as documented in [PROXY-CONTRACT.md](PROXY-CONTRACT.md). For UI work,
run the dev server in `figureskatingtools-site`.

## Project structure

```
scoremodifier/        # core PDF logic + CLI (canonical; bundled into the function at build time)
infra/
  main.bicep, modules/, parameters/   # subscription-scoped IaC
  functions/          # Python Azure Functions backend (function_app.py)
frontend/             # pre-migration UI copy — not built or deployed (see frontend/README.md)
PROXY-CONTRACT.md     # header contract between the site router and this backend
deploy_infra.sh, deploy_backend.sh
.github/workflows/deploy.yml
```

## License

PyMuPDF is licensed AGPL-3.0.
