import { queryOptions } from '@tanstack/react-query';
import { client, unwrap } from './client';

export const meQuery = queryOptions({ queryKey: ['me'], queryFn: () => unwrap(client.GET('/v1/me')) });

/** One tenant's detail, asked for with that tenant's header (the caller may belong to several). */
export const tenantQuery = (id: string) =>
  queryOptions({
    queryKey: ['tenant', id],
    queryFn: () =>
      unwrap(client.GET('/v1/tenants/{tenant_id}', { params: { path: { tenant_id: id }, header: { 'x-tenant-id': id } } })),
  });
