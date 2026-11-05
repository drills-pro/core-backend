import { UserManager } from 'oidc-client-ts';
import { cfg } from '@/config';

// Zitadel hosted login, as in customer-frontend. The `:aud` scope puts the project in the token's audience (core-api
// requires it) and the roles scope adds the project roles, so /v1/admin routes see platform_admin.
export const manager = new UserManager({
  authority: cfg.issuer,
  client_id: cfg.clientId,
  redirect_uri: `${location.origin}/auth/callback`,
  post_logout_redirect_uri: `${location.origin}/login`,
  response_type: 'code',
  scope: `openid profile email urn:zitadel:iam:org:project:id:${cfg.projectId}:aud urn:zitadel:iam:org:projects:roles`,
  automaticSilentRenew: false, // ponytail: a 401 sends you back to login; add offline_access + renew if that annoys.
});

let callback: ReturnType<typeof manager.signinCallback> | null = null; // StrictMode runs effects twice; the code is single-use.
export const finishLogin = () => (callback ??= manager.signinCallback());

export async function getToken() {
  const u = await manager.getUser();
  return u && !u.expired ? u.access_token : null;
}

export const signIn = (returnTo = '/') => void manager.signinRedirect({ state: returnTo });
export const signOut = async () => {
  const u = await manager.getUser();
  await manager.signoutRedirect({ id_token_hint: u?.id_token });
};
