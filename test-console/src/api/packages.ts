import { useMutation, useQueryClient } from '@tanstack/react-query';
import { client, unwrap, type Schemas } from '@/api/client';

/** Every cache key of the package and task screens, so a change invalidates by name instead of by repeated string literals. */
export const packageKeys = {
  all: ['packages'] as const,
  runtime: (name: Schemas['JobPackageName']) => ['packages', name] as const,
  bundles: ['task-bundles'] as const,
  /** The task catalog: a bundle status change moves a task's row and presets, so it refetches too. */
  catalog: ['task-catalog'] as const,
};

/** A runtime package, or a task bundle (the same shape plus its slug). */
export type Pkg = Schemas['JobPackage'];
export type Bundle = Schemas['TaskBundle'];
export type RuntimeName = Schemas['JobPackageName'];
export type TaskSlug = Schemas['TaskType'];
/** The statuses an operator can set (candidate is only ever the starting state); both status routes accept these. */
export type PackageStatusBody = Schemas['BundleStatusBody']['status'];

/** Where a package lives: a runtime package by its name, a task bundle by its slug (its name is task_<slug>). */
export type Target = { name: string; sha256: string; slug?: TaskSlug };

export const fetchRuntime = (name: RuntimeName) => unwrap(client.GET('/v1/admin/job-packages/{name}', { params: { path: { name } } }));
export const fetchBundles = () => unwrap(client.GET('/v1/admin/task-bundles'));

export const runtimeQuery = (name: RuntimeName) => ({ queryKey: packageKeys.runtime(name), queryFn: () => fetchRuntime(name) });
export const bundlesQuery = { queryKey: packageKeys.bundles, queryFn: fetchBundles };

const statusCall = (t: Target, status: PackageStatusBody) =>
  t.slug
    ? client.PUT('/v1/admin/task-bundles/{slug}/{sha256}/status', { params: { path: { slug: t.slug, sha256: t.sha256 } }, body: { status } })
    : client.PUT('/v1/admin/job-packages/{name}/{sha256}/status', { params: { path: { name: t.name as RuntimeName, sha256: t.sha256 } }, body: { status } });

const smokeCall = (t: Target) =>
  t.slug
    ? client.POST('/v1/admin/task-bundles/{slug}/{sha256}/smoke', { params: { path: { slug: t.slug, sha256: t.sha256 } } })
    : client.POST('/v1/admin/job-packages/{name}/{sha256}/smoke', { params: { path: { name: t.name as RuntimeName, sha256: t.sha256 } } });

/** A promotion retires the previous stable and a publish adds a candidate, so every package list refetches: the server knows
 * what became of the others, the screen does not guess. */
function useRefreshPackages() {
  const qc = useQueryClient();
  return () => Promise.all([packageKeys.all, packageKeys.bundles, packageKeys.catalog].map((queryKey) => qc.invalidateQueries({ queryKey })));
}

/** Promote, retire or roll back: the one status write behind every card. */
export function useSetPackageStatus() {
  const refresh = useRefreshPackages();
  return useMutation({
    mutationFn: ({ target, status }: { target: Target; status: PackageStatusBody }) => unwrap(statusCall(target, status)),
    onSuccess: refresh,
  });
}

/** Smoke a candidate again after its smoke failed (the API refuses anything else with 409). */
export function useRetrySmoke() {
  const refresh = useRefreshPackages();
  return useMutation({ mutationFn: (target: Target) => unwrap(smokeCall(target)), onSuccess: refresh });
}

/** Stages the code this core-api image ships with and registers it as a candidate, or the known row when it already is one. */
export function usePublishRuntime() {
  const refresh = useRefreshPackages();
  return useMutation({
    mutationFn: (name: RuntimeName) => unwrap(client.POST('/v1/admin/job-packages/{name}/publish', { params: { path: { name } } })),
    onSuccess: refresh,
  });
}
