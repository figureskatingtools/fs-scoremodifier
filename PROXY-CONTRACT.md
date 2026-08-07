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
| `X-Proxy-Secret` | router `server.js` | Proves the request came through the router. Compared against the `PROXY_SHARED_SECRET` app setting by `_proxy_secret_ok()`. |
| `X-Forwarded-User-Email` | router `server.js` | The signed-in user's email, taken from the router's Easy Auth principal. |

Both are defined in `infra/functions/function_app.py`.

### `X-Proxy-Secret`

- The router holds the same secret in its `PROXY_SHARED_SECRET_SCOREMODIFIER`
  app setting; this Function App holds it as `PROXY_SHARED_SECRET`
  (`infra/modules/function.bicep`, injected by CI from the GitHub environment
  secret `PROXY_SHARED_SECRET`).
- **Enforced only when the app setting is non-empty.** An unset secret fails
  open, so local `func start` and a brief pre-rollout window keep working
  instead of locking everyone out.
- A mismatch makes `get_user_email_from_header()` return `None`, which every
  route turns into **401**.

### `X-Forwarded-User-Email`

The browser never sets this — it would be trivially spoofable without the
shared-secret gate above. The router overwrites any inbound value.

## Header precedence in `get_user_email_from_header()`

Checked in order; the first hit wins:

0. **`_proxy_secret_ok(req)`** — if the shared secret is configured and missing
   or wrong, return `None` immediately (→ 401). This gate runs *before* any
   identity is read.
1. `X-MS-CLIENT-PRINCIPAL-NAME` — injected directly by App Service Easy Auth on
   this Function App. Not used in the current topology (no identity provider is
   configured here) but kept for direct-Easy-Auth setups.
2. `X-Forwarded-User-Email` — **the production path**, set by the router proxy.
3. `x-ms-client-principal` — base64 JSON principal (SWA style); `userDetails` is
   used as the email.
4. `Authorization: Bearer <jwt>` — payload decoded (base64 only, **not**
   signature-verified) and read in order:
   `preferred_username` → `email` → `upn` → `unique_name` → `emails[0]` →
   `name` → `oid`.

If none match, the function logs the non-auth headers and returns `None` → the
route responds **401**.

Header names are matched case-insensitively (each lookup tries both the
canonical and lowercase spelling).

## Local / manual testing

Against a local `func start` (port 7071) with `PROXY_SHARED_SECRET` unset, the
secret header is optional:

```bash
curl -i "http://localhost:7071/api/check_user_permission" \
  -H "x-forwarded-user-email: someone@example.com"
```

Against a deployed Function App (secret enforced):

```bash
FUNC=func-fs-scoremodifier-xxxxxxxx           # deploy-infra output functionAppName
SECRET=…                                      # GitHub environment secret PROXY_SHARED_SECRET

# Auth probe — expect 200
curl -i "https://$FUNC.azurewebsites.net/api/check_user_permission" \
  -H "x-proxy-secret: $SECRET" \
  -H "x-forwarded-user-email: someone@example.com"

# Wrong/absent secret — expect 401
curl -i "https://$FUNC.azurewebsites.net/api/check_user_permission" \
  -H "x-forwarded-user-email: someone@example.com"

# A real workload endpoint (PDF in the body)
curl -i "https://$FUNC.azurewebsites.net/api/generate?includeRanks=false" \
  -H "x-proxy-secret: $SECRET" \
  -H "x-forwarded-user-email: someone@example.com" \
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
an empty `PROXY_SHARED_SECRET` disables the gate entirely.
