import { createRootRouteWithContext, createRoute, createRouter, Outlet, redirect } from '@tanstack/react-router';
import type { QueryClient } from '@tanstack/react-query';
import { createQueryClient, setApiTenant } from '@/api/client';
import { tenantQuery } from '@/api/queries';
import { getToken, manager } from '@/auth';
import { INTERNAL_PLAN_TIER } from '@/constants';
import { CallbackPage, LoginPage } from '@/pages/Auth';
import { CatalogModelsPage } from '@/pages/CatalogModels';
import { Layout } from '@/pages/Layout';
import { PackagesPage } from '@/pages/Packages';
import { PipelinePage } from '@/pages/Pipeline';
import { ProjectsPage } from '@/pages/Projects';
import { TenantsPage } from '@/pages/Tenants';
import { ErrorAlert } from '@/ui';

const rootRoute = createRootRouteWithContext<{ queryClient: QueryClient }>()({
  component: Outlet,
  errorComponent: ({ error }) => <ErrorAlert error={error} />,
});

export const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/login',
  // Only in-app paths may be returned to.
  validateSearch: (s: Record<string, unknown>): { return_to?: string } => ({
    return_to: typeof s.return_to === 'string' && s.return_to.startsWith('/') ? s.return_to : undefined,
  }),
  component: LoginPage,
});
const callbackRoute = createRoute({ getParentRoute: () => rootRoute, path: '/auth/callback', component: CallbackPage });

const appRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: 'app',
  beforeLoad: async ({ location }) => {
    if (!(await getToken())) throw redirect({ to: '/login', search: { return_to: location.href } });
  },
  component: Layout,
});
const tenantsRoute = createRoute({ getParentRoute: () => appRoute, path: '/', component: TenantsPage });
const packagesRoute = createRoute({ getParentRoute: () => appRoute, path: '/packages', component: PackagesPage });
const modelCatalogRoute = createRoute({ getParentRoute: () => appRoute, path: '/model-catalog', component: CatalogModelsPage });

/** The guard: the console only ever acts in an internal tenant, so a staff member who also belongs to a customer
 * tenant cannot train or deploy there by mistake. The API would allow it; this page will not. */
export const tenantRoute = createRoute({
  getParentRoute: () => appRoute,
  path: '/t/$tenantId',
  beforeLoad: async ({ params, context }) => {
    const tenant = await context.queryClient.ensureQueryData(tenantQuery(params.tenantId));
    if (tenant.plan_tier !== INTERNAL_PLAN_TIER)
      throw new Error(`${tenant.name} is a ${tenant.plan_tier} tenant; this console only acts in internal tenants`);
    setApiTenant(params.tenantId);
    return { tenant };
  },
  component: Outlet,
});
export const projectsRoute = createRoute({ getParentRoute: () => tenantRoute, path: '/', component: ProjectsPage });
export const pipelineRoute = createRoute({ getParentRoute: () => tenantRoute, path: '/p/$projectId', component: PipelinePage });

const routeTree = rootRoute.addChildren([
  loginRoute,
  callbackRoute,
  appRoute.addChildren([tenantsRoute, packagesRoute, modelCatalogRoute, tenantRoute.addChildren([projectsRoute, pipelineRoute])]),
]);

export const queryClient = createQueryClient(() => {
  void manager.removeUser();
  void router.navigate({ to: '/login', search: { return_to: router.state.location.href } });
});

export const router = createRouter({ routeTree, context: { queryClient } });

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router;
  }
}
