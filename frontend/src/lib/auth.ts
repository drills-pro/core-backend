// Login is Zitadel's hosted OIDC flow (authorization code + PKCE), run in the browser.
// core-api only verifies the access token and handles signup (POST /v1/auth/signup).
import { API_BASE, ZITADEL_CLIENT_ID, ZITADEL_ISSUER, ZITADEL_PROJECT_ID } from './config'

export interface AuthTokens {
  access_token: string
  id_token: string
  /** Epoch milliseconds, derived from the token response's expires_in. */
  expires_at: number
}

const STORAGE_KEY = 'drills.auth'

export function getAuth(): AuthTokens | null {
  const raw = localStorage.getItem(STORAGE_KEY)
  if (!raw) return null
  let auth: AuthTokens
  try {
    auth = JSON.parse(raw)
  } catch {
    localStorage.removeItem(STORAGE_KEY)
    return null
  }
  if (!auth?.access_token || auth.expires_at <= Date.now()) {
    localStorage.removeItem(STORAGE_KEY)
    return null
  }
  return auth
}

export function setAuth(auth: AuthTokens): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(auth))
}

export function logout(): void {
  localStorage.removeItem(STORAGE_KEY)
  // Full navigation rather than a router push: every cached query in the
  // app was fetched with the old token, so the cleanest reset is a reload.
  window.location.assign('/login')
}

/** Bearer header for core-api calls, or {} when signed out. */
export function authHeader(): Record<string, string> {
  const auth = getAuth()
  return auth ? { Authorization: `Bearer ${auth.access_token}` } : {}
}

/**
 * Claims out of the id_token, for display only (email/name in the sidebar).
 * Signature verification is core-api's job — app/auth.py checks it against
 * Zitadel's JWKS on every request; nothing here is trusted for access.
 */
export function decodeIdToken(idToken: string): Record<string, unknown> {
  const payload = idToken.split('.')[1]
  if (!payload) return {}
  try {
    const json = atob(payload.replace(/-/g, '+').replace(/_/g, '/'))
    // Handle non-ASCII claims (names) that atob leaves as raw bytes.
    const decoded = decodeURIComponent(
      Array.from(json, (c) => `%${c.charCodeAt(0).toString(16).padStart(2, '0')}`).join(''),
    )
    return JSON.parse(decoded)
  } catch {
    return {}
  }
}

async function post(path: string, body: unknown, headers: Record<string, string> = {}): Promise<unknown> {
  const resp = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify(body),
  })
  const text = await resp.text()
  const data = text ? JSON.parse(text) : null
  if (!resp.ok) {
    const detail = (data as { detail?: unknown } | null)?.detail
    throw new Error(typeof detail === 'string' ? detail : `${resp.status} ${resp.statusText}`)
  }
  return data
}

const PKCE_KEY = 'drills.pkce'
// The login page doubles as the OIDC redirect target (register this exact URL in Zitadel).
const redirectUri = () => `${window.location.origin}/login`

function b64url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/** Redirects to Zitadel's login. `loginHint` pre-fills the email (used right after signup). */
export async function startLogin(loginHint?: string): Promise<void> {
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)))
  const challenge = b64url(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))))
  const state = b64url(crypto.getRandomValues(new Uint8Array(16)))
  sessionStorage.setItem(PKCE_KEY, JSON.stringify({ verifier, state }))
  const params = new URLSearchParams({
    client_id: ZITADEL_CLIENT_ID,
    redirect_uri: redirectUri(),
    response_type: 'code',
    // project aud: core-api's audience check; projects:roles: the roles claim; resourceowner: tenant org.
    scope: [
      'openid', 'profile', 'email',
      `urn:zitadel:iam:org:project:id:${ZITADEL_PROJECT_ID}:aud`,
      'urn:zitadel:iam:org:projects:roles',
      'urn:zitadel:iam:user:resourceowner',
    ].join(' '),
    code_challenge: challenge,
    code_challenge_method: 'S256',
    state,
  })
  if (loginHint) params.set('login_hint', loginHint)
  window.location.assign(`${ZITADEL_ISSUER}/oauth/v2/authorize?${params}`)
}

/** Finishes the redirect: swaps the code for tokens and stores them. */
export async function completeLogin(code: string, state: string): Promise<AuthTokens> {
  const saved = JSON.parse(sessionStorage.getItem(PKCE_KEY) ?? 'null') as { verifier: string; state: string } | null
  sessionStorage.removeItem(PKCE_KEY)
  if (!saved || saved.state !== state) throw new Error('login state mismatch, please try again')
  const resp = await fetch(`${ZITADEL_ISSUER}/oauth/v2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'authorization_code',
      code,
      redirect_uri: redirectUri(),
      client_id: ZITADEL_CLIENT_ID,
      code_verifier: saved.verifier,
    }),
  })
  if (!resp.ok) throw new Error(`login failed: ${resp.status}`)
  const tokens = await resp.json()
  const auth: AuthTokens = {
    access_token: tokens.access_token,
    id_token: tokens.id_token,
    expires_at: Date.now() + tokens.expires_in * 1000,
  }
  setAuth(auth)
  return auth
}

/**
 * POST /v1/auth/signup — creates a new organisation and its owner account, no session yet.
 * Reuse the same idempotencyKey when retrying one form submission so the server resumes
 * instead of creating a second org.
 */
export async function signup(
  orgName: string,
  email: string,
  password: string,
  firstName: string,
  lastName: string,
  idempotencyKey: string,
): Promise<{ tenant_id: string; org_id: string }> {
  return (await post(
    '/v1/auth/signup',
    { org_name: orgName, email, password, first_name: firstName, last_name: lastName },
    { 'Idempotency-Key': idempotencyKey },
  )) as { tenant_id: string; org_id: string }
}
