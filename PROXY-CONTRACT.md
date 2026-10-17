# Proxy contract

This repo is **backend-only**. The Score Modifier UI is served by the
[`figureskatingtools-site`](https://github.com/figureskatingtools) router Web App at
`https://figureskatingtools.com/scoremodifier/`, which proxies
`/scoremodifier/api/*` to this Function App as `/api/*` (the `/scoremodifier`
prefix is stripped; backend routes are unchanged).

The Function App is publicly reachable and every route is
`func.AuthLevel.ANONYMOUS`, so **all authentication and authorization ride on
request headers set by the router**. Nothing else may call these endpoints.

## Headers

| Header | Set by | Purpose |
|---|---|---|
| `X-Proxy-Secret` | router `server.js` | Proves the request came through the router. Compared against the `PROXY_SHARED_SECRET` app setting by `_proxy_secret_ok()` (`hmac.compare_digest`). |
| `X-Forwarded-User-Tid` | router `server.js` | Entra tenant id (lowercase GUID) of the signed-in principal. |
| `X-Forwarded-User-Oid` | router `server.js` | Entra object id (lowercase GUID) of the signed-in principal. With the tid, **the** identity. |
| `X-Forwarded-User-Email` | router `server.js` | E-mail, lowercased. Display and the `CreatedBy` history only — **never** authorisation. Optional. |
| `X-Forwarded-User-Name` | router `server.js` | Display name, `encodeURIComponent`-encoded (decoded with `urllib.parse.unquote`). Optional. |
| `X-Forwarded-User-Admin` | router `server.js` | `1` only when the router's `isAdminRequest` is true; absent otherwise. |

All are read in `infra/functions/function_app.py` (`get_user()` → `User(tid,
oid, email, name, is_admin)`). The router builds its outbound headers from an
allow-list, so a browser cannot set any of them; the secret is what stops a
direct caller from setting them on the public Function App.

### `X-Proxy-Secret` — fails closed

- The router holds the same secret in its `PROXY_SHARED_SECRET_SCOREMODIFIER`
  app setting; this Function App holds it as `PROXY_SHARED_SECRET`
  (`infra/modules/function.bicep`, injected by CI from the GitHub environment
  secret `PROXY_SHARED_SECRET`).
- **An unset or empty `PROXY_SHARED_SECRET` rejects every request** (401), and
  so does a missing or wrong header. Local `func start` must set a secret too.
- The gate runs before any identity header is read. `GET health` needs neither
  identity nor the secret: it answers `{"status": "ok", "service":
  "fs-scoremodifier"}` without touching storage, for the site's admin
  diagnostics.

### Identity — `x-forwarded-user-*` only

- `X-Forwarded-User-Tid` and `-Oid` must both be GUIDs (trimmed, lowercased);
  missing or malformed → no identity → **401** on every route.
- Nothing else is an identity source any more: `X-MS-CLIENT-PRINCIPAL-NAME`,
  the base64 `X-MS-CLIENT-PRINCIPAL` JSON and `Authorization: Bearer` JWTs are
  ignored, and an e-mail header alone is not enough.
- `is_admin` is exactly `X-Forwarded-User-Admin == "1"` (trusted only because
  the secret gate fails closed).
- Without an identity the function logs the request's header **names** (never
  values) and responds 401.

Header names are matched case-insensitively.

## Competition access

The platform registry in `figureskatingtools-site` decides who may see or
change a competition. `infra/functions/platform_access.py` (**byte-identical**
with fs-judgepapers and fs-protocolgenerator; its docstring is the contract)
maps `(tid, oid)` to the platform user through the platform's
`useridentities` table and reads that user's role from `competitionaccess`
(`owner` > `write` > `read`), both on the account named by
`PLATFORM_STORAGE_ACCOUNT` (Table Data Reader grants from the site repo's
`shared-data-access.bicep`; table names overridable with
`PLATFORM_IDENTITIES_TABLE` / `PLATFORM_ACCESS_TABLE`). Answers are cached for
30 s, so a revoked share stops working within that time. Admins are `owner`
everywhere without a lookup.

| Route | Needs |
|---|---|
| `POST generate?platformId=` | write on `platformId` |
| `POST generate_results?platformId=` | write on `platformId` |
| `GET get_competition_details?id=` | read on the record's `PlatformId` |
| `POST delete_competition` (id from `?id=` or JSON body `{"id"}`; other methods → 405) | **owner** on the record's `PlatformId` (admin counts as owner) |
| `GET list_competitions` | lists only records whose `PlatformId` the caller can read (admin: all) |
| `GET parse_index?url=`, `check_user_permission` | sign-in only (no competition) |
| `GET health` | nothing |

Records without a (valid) `PlatformId` are legacy rows: **admin only**.

| Situation | Response (JSON) |
|---|---|
| No role: no grant, unknown platform competition or identity, unknown record id, legacy record for a non-admin | 404 `{"error": "platform_not_found"}` — existence is never revealed |
| Read-only caller on a write route | 403 `{"error": "platform_read_only"}` |
| Read or write caller on an owner route | 403 `{"error": "platform_owner_required"}` |
| Access tables unreadable (`PLATFORM_STORAGE_ACCOUNT` unset, table missing, RBAC, network) | 503 `{"error": "platform_unavailable"}` |

Download links (`downloadUrl`/`htmlUrl` from the generators,
`generatedFiles[].url` from `get_competition_details`) are read-only SAS URLs
minted fresh for each response, after the access check, and valid 15 minutes
(`expiration`); none is stored.

The access check runs right after `platformId` validation (400
`platform_id_required`) and before the upload is read; the retention-clock
binding (404/410/503, see `CLAUDE.md` → Retention) follows it.

## Local / manual testing

The secret is enforced everywhere, so set one for a local `func start` too
(`PROXY_SHARED_SECRET` in `local.settings.json`). Without
`PLATFORM_STORAGE_ACCOUNT` the access tables are unreadable, so competition
routes answer 503 — except for an admin (`x-forwarded-user-admin: 1`), who
needs no lookup.

```bash
FUNC=func-fs-scoremodifier-xxxxxxxx           # deploy-infra output functionAppName (or localhost:7071)
SECRET=…                                      # GitHub environment secret PROXY_SHARED_SECRET
WHO=(-H "x-proxy-secret: $SECRET"
     -H "x-forwarded-user-tid: $TENANT_ID"    # your Entra tenant id
     -H "x-forwarded-user-oid: $OBJECT_ID"    # your Entra object id
     -H "x-forwarded-user-email: someone@example.com")

# Auth probe — expect 200
curl -i "https://$FUNC.azurewebsites.net/api/check_user_permission" "${WHO[@]}"

# Wrong/absent secret, or no tid/oid — expect 401
curl -i "https://$FUNC.azurewebsites.net/api/check_user_permission" \
  -H "x-forwarded-user-email: someone@example.com"

# A real workload endpoint (PDF in the body). platformId = a platform
# competition GUID you have write access to (required; 400
# platform_id_required without it, 404 platform_not_found without access or
# for an unknown competition, 403 platform_read_only with read access, 410
# platform_deleted, 503 platform_unavailable when the platform tables cannot
# be read — nothing stored).
curl -i "https://$FUNC.azurewebsites.net/api/generate?includeRanks=false&platformId=$PLATFORM_ID" \
  "${WHO[@]}" \
  -H "content-type: application/pdf" \
  --data-binary @judges-details-per-skater.pdf
```

## Rotating the secret

The value lives in two places per environment: the `PROXY_SHARED_SECRET` secret
in this repo's GitHub environment, and `PROXY_SHARED_SECRET_SCOREMODIFIER` in
the site repo's matching environment. The backend compares against exactly one
value, so any rotation has a short window where the two sides disagree and calls
401. Update both GitHub secrets, then redeploy the backend and the site
back-to-back during a quiet period. Do **not** rotate by clearing the setting —
an empty `PROXY_SHARED_SECRET` now rejects every request (the gate fails
closed), which is an outage, not a bypass.
