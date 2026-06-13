# fs-scoremodifier

Score Modifier provides tools to reshape figure skating result PDFs. Part of the
[figureskatingtools.com](https://figureskatingtools.com) ecosystem (sibling of `fs-judgepapers`),
deployed to its own subdomain (`scoremodifier.figureskatingtools.com`).

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
| **Frontend** | TypeScript + Vite single-page tool, served by a zero-dep Node.js proxy (`server.js`) |
| **Backend** | Python Azure Functions (Flex Consumption, HTTP) — `generate` endpoint calls the core |
| **Auth** | Microsoft Entra ID via App Service Easy Auth (silent SSO from figureskatingtools.com) |
| **Storage** | Azure Blob Storage (uploaded + generated PDFs) + Table Storage (`competitions`, `generatedpapers`) |
| **Infra** | Azure Bicep (subscription-scoped), own storage account |

The frontend is a single page: upload the PDF, tick **Include ranks** (off by default — non-podium
ranks are hidden), click **Generate**, download the result. Each run is persisted (source + output
in blob storage, a competition + paper row in tables) so later features can build on the data. The
Web App proxies `/api/*` to the Function App, forwarding the user's email plus a shared secret; the
Function App is anonymous but rejects requests without the secret (see `fs-judgepapers/CLAUDE.md` for
the full auth chain — it is identical here).

## Core tool (standalone / CLI)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m scoremodifier per-skater input.pdf -o out.pdf [--hide-non-podium-ranks]
```

The Azure Function imports `scoremodifier.per_skater.split_per_skater` directly; the deploy build
copies the repo-root `scoremodifier/` package into the function package (single source of truth).

## Deployment

CI (`.github/workflows/deploy.yml`) deploys infra (Bicep) → backend → frontend on **push to `main`
(prod)** or **manual `workflow_dispatch` (test)**, mirroring `fs-judgepapers`.

Manual:
```bash
./deploy_infra.sh --client-id <ENTRA_CLIENT_ID> [--proxy-secret <SECRET>]
./deploy_backend.sh -g <resource-group>
./deploy_frontend.sh -g <resource-group>
```

### One-time setup
1. `./create_auth_app.sh "ScoreModifier" scoremodifier.figureskatingtools.com` → note Client ID + Object ID.
2. Create GitHub Environments `test` and `prod` with:
   - **Secrets:** `AZURE_CLIENT_ID` (OIDC deploy principal), `AUTH_CLIENT_ID`, `AUTH_APP_OBJECT_ID`, `PROXY_SHARED_SECRET`
   - **Variables:** `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `LOCATION`, `RESOURCE_GROUP_NAME`, `CUSTOM_DOMAIN`
3. Grant admin consent for the new app registration (Enterprise Applications → Permissions).
4. The deploy workflow creates the federated identity credential (no client secret) and patches redirect URIs automatically.

The custom domain CNAME + `asuid` TXT are created by this deployment in the shared
`figureskatingtools.com` DNS zone (`rg-fs-dns`, owned by the landing-page repo).

## Local development

```bash
./start_locally.sh   # Functions :7071 + Vite :5173 + SWA emulator :4280 (sets PYTHONPATH for the core import)
```

Requires an Azurite/real storage connection in `infra/functions/local.settings.json` and a GitHub
token with `read:packages` for the `@figureskatingtools/shared-ui` install
(`gh auth refresh -s read:packages`).

## Project structure

```
scoremodifier/        # core PDF logic + CLI (canonical; bundled into the function at build time)
infra/
  main.bicep, modules/, parameters/   # subscription-scoped IaC
  functions/          # Python Azure Functions backend (function_app.py)
frontend/             # single-page Vite tool + Node proxy (server.js)
deploy_*.sh, create_auth_app.sh, start_locally.sh
.github/workflows/deploy.yml
```

## License

PyMuPDF is licensed AGPL-3.0.
