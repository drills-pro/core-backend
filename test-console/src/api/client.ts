import createClient from 'openapi-fetch';
import { MutationCache, QueryCache, QueryClient } from '@tanstack/react-query';
import { getToken } from '@/auth';
import { cfg } from '@/config';
import type { components, paths } from './schema';

export type Schemas = components['schemas'];

/** `status` 0 means no answer (offline, CORS, DNS). `detail` is core-api's message: staff see it, unlike customers. */
export class ApiError extends Error {
  constructor(
    public status: number,
    public code?: string,
    public detail?: string,
  ) {
    super(`${status || 'network'}${code ? ` ${code}` : ''}${detail ? `: ${detail}` : ''}`);
  }
}

/** The active test tenant, sent as X-Tenant-Id. Set by the tenant layout. */
let tenantId: string | undefined;
export const setApiTenant = (id?: string) => {
  tenantId = id;
};

export const client = createClient<paths>({ baseUrl: cfg.apiUrl });
client.use({
  async onRequest({ request }) {
    const token = await getToken();
    if (token) request.headers.set('Authorization', `Bearer ${token}`);
    if (tenantId && !request.headers.has('X-Tenant-Id')) request.headers.set('X-Tenant-Id', tenantId);
    return request;
  },
});

/** Resolve to the response body, or throw an ApiError. */
export async function unwrap<T>(req: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  let r;
  try {
    r = await req;
  } catch {
    throw new ApiError(0);
  }
  if (!r.response.ok) {
    const e = r.error as { code?: string; detail?: unknown } | undefined;
    throw new ApiError(r.response.status, e?.code, typeof e?.detail === 'string' ? e.detail : JSON.stringify(e?.detail));
  }
  return r.data as T;
}

export const newKey = () => crypto.randomUUID();

export function createQueryClient(onUnauthorized: () => void) {
  const on401 = (e: unknown) => {
    if (e instanceof ApiError && e.status === 401) onUnauthorized();
  };
  return new QueryClient({
    queryCache: new QueryCache({ onError: on401 }),
    mutationCache: new MutationCache({ onError: on401 }),
    defaultOptions: { queries: { staleTime: 5_000, retry: false } },
  });
}
