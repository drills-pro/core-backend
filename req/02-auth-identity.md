# Auth / identity

## Decision: Zitadel

Recommendation is Zitadel over Authentik for this product, reversible if the reasoning below stops holding.

| Criterion | Zitadel | Authentik |
|---|---|---|
| Multi-tenancy model | Organization/project/application as first-class API resources, purpose-built for B2B SaaS | No native org-as-tenant primitive; would be bolted on via groups/policies |
| API-first / IaC | Strong Terraform provider, API described as intentional for multi-tenant setups | API-first, but oriented around flows/policies not tenant objects |
| Ecosystem/integrations | Smaller community, fewer pre-built integrations | Larger community (~20k+ GitHub stars), 200+ integrations, richer for legacy/LDAP/proxy scenarios |
| Custom auth flows | Less flexible flow customization | Flow Engine allows deep customization of login/registration steps |
| K8s-native deployment | Kubernetes operator, Go single-binary, lighter footprint | Python/Go hybrid, Postgres-based since 2025.10 (Redis dependency removed) |
| Consensus read | <cite index="12-1">Best open source option when multi-tenancy is the primary requirement</cite>; <cite index="15-1">purpose-built for SaaS multi-tenancy, B2B scenarios, Kubernetes-first deployments</cite> | Best choice when the requirement is broadest IdP feature coverage (LDAP, proxy mode, SAML edge cases) rather than tenant modeling |

Since tenant = org is the core requirement here (not LDAP federation or legacy app proxying), Zitadel's data model removes a layer of custom-build work. Revisit if a customer requires SAML/LDAP federation Zitadel doesn't cover well — Authentik or Keycloak would be the fallback.

## Requirements
- FR1: OIDC authorization code flow (PKCE) for the no-code UI (SPA). No password handling in app code — delegate entirely to Zitadel hosted or self-hosted login UI.
- FR2: Every Zitadel "organization" maps 1:1 to a platform tenant (see 01-multi-tenancy.md).
- FR3: Roles: `org_owner`, `org_admin`, `project_editor`, `project_viewer` at minimum — provisioned via Zitadel project roles, read into app JWT claims.
- FR4: Service-to-service auth (app backend → Ray/MLflow/internal APIs) uses Zitadel machine-to-machine (client credentials) tokens, not shared static API keys.
- FR5: Support "invite user to org" flow without requiring platform team intervention (self-serve).
- NFR1: Self-hosted Zitadel runs on Postgres (or CockroachDB) already in the data layer — no new DB technology introduced solely for auth.
- NFR2: Session/token validation must not add >20ms p95 to API gateway request path — cache JWKS, don't call Zitadel per-request.

## Open questions
- Managed Zitadel Cloud (free to 25K MAU) vs self-hosted from day one — self-hosting avoids vendor dependency but adds ops surface before there's a paying customer to justify it. Lean managed for phase 0/1, self-host at phase 2 (see 00-overview phasing).
