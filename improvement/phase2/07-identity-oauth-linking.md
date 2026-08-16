# Identity — email-anchored accounts, OAuth linking, profile settings

## The constraint that drives this whole doc
"Same email registered to Gmail must link as 1 account, not a separate
one; username is optional." This is exactly the problem Zitadel's own
identity model is built to solve — it is not a gap Phase 1 left, it's a
Zitadel feature (`Identity Providers` + account linking by verified
email) that was never turned on because Phase 1 only wired the
embedded-password login flow.

## How Zitadel already solves it
FR1: Zitadel supports registering external IdPs (Google, GitHub as OIDC/
OAuth providers) on the org, with a linking policy: if a login via an
external IdP presents an email that matches an existing Zitadel user's
*verified* email, Zitadel links the external identity to that existing
user rather than creating a second one. This is configuration
(`scripts/zitadel-bootstrap.sh` gains an IdP-registration step calling
Zitadel's Admin API), not new application code.
FR2: core-api's embedded-auth module (`app/embedded_auth.py`) already
does the "no redirect to Zitadel's domain" trick for password login by
driving the Session API server-side. OAuth linking is different in kind:
it *requires* a redirect to the provider (Google/GitHub) to obtain
consent — that's how OAuth works, not something to route around. The
"no cross-domain redirect" constraint from Phase 1's embedded-auth work
applies to the platform's own login form, not to a user's explicit
"link my Google account" action, which is expected to leave the app
momentarily.
FR3: Flow: user clicks "Connect Google" from account settings (already
logged in) → core-api requests Zitadel's IdP-intent URL → redirect to
Google → callback → Zitadel completes the link on the *current* logged-in
user's account (Zitadel's "IdP intent" API is built for exactly this
already-authenticated-user linking case, distinct from IdP-as-login).

## Profile settings (new UI surface, thin backend)
FR4: A `/settings/profile` page: display name, avatar (stored as a
presigned-URL-backed upload like everything else — reuse
`object_storage.py`'s bucket pattern, a `platform-assets` bucket, not
LakeFS since avatars aren't versioned data), and the list of linked login
methods (password / Google / GitHub) with a "disconnect" action per
non-password method (password itself can't be the last method removed
without a password already set — Zitadel enforces "you must always be
able to log in").
FR5: Optional username: stored on the platform's own `users` table
(new — Phase 1 has no user-profile table at all, only `zitadel_org_id`
on `tenants`), not on Zitadel — username is a display/vanity concept
here, email is the actual identity anchor per the stated requirement, so
it doesn't need to live in the IdP.

## What this doc does NOT propose
A second, home-grown OAuth client implementation, a session/JWT scheme
independent of Zitadel's, or a "merge accounts" flow for two *already
separately created* accounts with the same email (Zitadel's linking
policy prevents that state from occurring going forward; retroactively
merging pre-existing duplicate accounts, if any exist from Phase 1
testing, is a one-time data cleanup script, not a product feature).
