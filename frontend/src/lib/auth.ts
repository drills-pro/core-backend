// Embedded email login against core-api's app/embedded_auth.py — the
// browser never navigates to Zitadel's own domain; core-api does the
// password check / code exchange server-side and hands back real tokens.
import { API_BASE } from './config'

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

async function post(path: string, body: unknown): Promise<unknown> {
  const resp = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
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

export interface LoginResponse {
  access_token: string
  id_token: string
  expires_in: number
}

/** POST /v1/auth/login — stores the tokens on success. */
export async function login(email: string, password: string): Promise<AuthTokens> {
  const tokens = (await post('/v1/auth/login', { email, password })) as LoginResponse
  const auth: AuthTokens = {
    access_token: tokens.access_token,
    id_token: tokens.id_token,
    expires_at: Date.now() + tokens.expires_in * 1000,
  }
  setAuth(auth)
  return auth
}

/** POST /v1/auth/register — creates a real Zitadel account, no session yet. */
export async function register(
  email: string,
  password: string,
  firstName: string,
  lastName: string,
): Promise<{ userId: string }> {
  return (await post('/v1/auth/register', {
    email,
    password,
    first_name: firstName,
    last_name: lastName,
  })) as { userId: string }
}
