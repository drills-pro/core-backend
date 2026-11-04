const env = import.meta.env;

/** Build-time settings. Defaults are production; local overrides live in .env.local (see .env.example). */
export const cfg = {
  issuer: (env.VITE_ZITADEL_ISSUER ?? 'https://auth.drills.pro') as string,
  /** The console's own Zitadel SPA application (code + PKCE, JWT access tokens). Required. */
  clientId: (env.VITE_ZITADEL_CLIENT_ID ?? '') as string,
  /** Project the API checks in `aud`; its roles (platform_admin) come with the roles scope. */
  projectId: (env.VITE_ZITADEL_PROJECT_ID ?? '395337035725930916') as string,
  apiUrl: (env.VITE_API_URL ?? 'https://api.drills.pro') as string,
};
