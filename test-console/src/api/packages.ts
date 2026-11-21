import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, unwrap, type Schemas } from '@/api/client';

/** Every cache key of the package screens, so a change invalidates by name instead of by repeated string literals. */
export const packageKeys = {
  all: ['packages'] as const,
  runtime: (name: Schemas['JobPackageName']) => ['packages', name] as const,
  bundles: ['task-bundles'] as const,
  /** A bundle status change moves the task's row and presets, so the catalog screen refetches too. */
  catalog: ['task-catalog'] as const,
};

/** A runtime package or a task bundle: the fields the package table shows (a bundle also has a slug). */
export type Pkg = Schemas['JobPackage'];
/** The statuses an operator can set (candidate is only ever the starting state); both routes accept these. */
export type PackageStatusBody = Schemas['BundleStatusBody']['status'];

export const useRuntimePackages = (name: Schemas['JobPackageName']) =>
  useQuery({ queryKey: packageKeys.runtime(name), queryFn: () => unwrap(client.GET('/v1/admin/job-packages/{name}', { params: { path: { name } } })) });

export const useTaskBundles = () => useQuery({ queryKey: packageKeys.bundles, queryFn: () => unwrap(client.GET('/v1/admin/task-bundles')) });

/** The one write behind Promote / Retire / Roll back. A promotion retires the previous stable server-side, so every list
 * is refetched rather than patched here: the server is the only place that knows what became of the old one. */
export function useSetPackageStatus() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ pkg, slug, status }: { pkg: Pkg; slug?: Schemas['TaskType']; status: PackageStatusBody }) =>
      slug
        ? unwrap(client.PUT('/v1/admin/task-bundles/{slug}/{sha256}/status', { params: { path: { slug, sha256: pkg.sha256 } }, body: { status } }))
        : unwrap(client.PUT('/v1/admin/job-packages/{name}/{sha256}/status', { params: { path: { name: pkg.name as Schemas['JobPackageName'], sha256: pkg.sha256 } }, body: { status } })),
    onSuccess: () => Promise.all([packageKeys.all, packageKeys.bundles, packageKeys.catalog].map((queryKey) => qc.invalidateQueries({ queryKey }))),
  });
}
