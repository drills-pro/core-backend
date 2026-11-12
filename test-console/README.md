# test-console

Staff console for testing the pipeline **on prod inside an internal test tenant**: import a dataset (Hugging Face or a
file upload), train, deploy, send a test call, and promote or retire job packages. Served at `console.drills.pro`.

It looks deliberately unlike the customer app: Chakra's default theme with an orange header showing the API host.

## How it is fenced

- An internal tenant is a tenant with `plan_tier = internal` (core-api `PlanTier.INTERNAL`). It has its own row in
  `plan_limits` (a copy of enterprise at migration time; change it with `PUT /v1/admin/plan-limits/internal`) and is left
  out of `GET /v1/admin/tenants` and `/summary` unless you ask for `plan_tier=internal`.
- The console calls the ordinary `/v1` user routes as a **member** of that tenant: prod runs the code customers run, and
  there is no impersonation path. It lists only your internal tenants and refuses to open any other one.
- Job package status changes ask you to type the package name and the first 8 characters of its sha. The API still
  requires `platform_admin` and refuses `stable` until the smoke job passed (409 `smoke_not_passed`).

## Set up once

1. Create the internal tenant as a platform admin; the caller becomes its owner. Other staff join through the tenant's invitations.

   ```sh
   curl -X POST https://api.drills.pro/v1/admin/tenants -H "Authorization: Bearer $ACCESS" \
     -H 'Content-Type: application/json' -d '{"name": "QA", "plan_tier": "internal"}'
   ```

2. In Zitadel project `Drills.Pro`, add a **User Agent (SPA)** app: PKCE, auth token type **JWT**, redirect URIs
   `https://console.drills.pro/auth/callback` and `http://localhost:5174/auth/callback`, post-logout `<origin>/login`.
   Turn on "assert roles on authentication" so `platform_admin` reaches the token. Put its client id in the GitHub repo
   variable `TEST_CONSOLE_ZITADEL_CLIENT_ID` and in `.env.local` for local runs.
3. Uploads go from the browser straight to the staging bucket: its CORS must allow `PUT` from `https://console.drills.pro`.
4. k8s: `apps/frontend-admin` in the k8s repo serves it at console.drills.pro. Point DNS for `console.drills.pro` at the ingress. Each push to `main` that touches `test-console/` builds the image and bumps `frontend-admin` through `bump-image.yml` (`.github/workflows/test-console-image.yml`). The first time, run the workflow by hand and replace the `REPLACE_WITH_THE_FIRST_IMAGE_DIGEST` placeholder in `apps/frontend-admin/app/kustomization.yaml` with its digest before pushing the k8s repo.

## Develop

```sh
cp .env.example .env.local   # set VITE_ZITADEL_CLIENT_ID; VITE_API_URL to point elsewhere than prod
pnpm install
pnpm dev                     # http://localhost:5174
pnpm build                   # typecheck + bundle
pnpm api:gen                 # regenerate src/api/schema.d.ts from the API (API_SPEC_URL for a local spec file)
```
