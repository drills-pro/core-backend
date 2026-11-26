/** Cache keys of the platform-admin screens, so a write invalidates by name instead of by repeated string literals.
 * A key is a prefix of the ones under it: invalidating `tenant(id)` also refetches its members, projects and datasets. */
export const adminKeys = {
  tenants: ['admin-tenants'] as const,
  tenantsSummary: ['admin-tenants-summary'] as const,
  tenant: (id: string) => ['admin-tenant', id] as const,
  planLimits: ['admin-plan-limits'] as const,
  models: ['admin-models'] as const,
  modelsSummary: ['admin-models-summary'] as const,
  modelVersions: (id: string) => ['admin-model-versions', id] as const,
  activity: ['admin-activity'] as const,
  activityStats: ['admin-activity-summary'] as const,
  audit: ['admin-audit-log'] as const,
};
