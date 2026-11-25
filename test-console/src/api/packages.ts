import { useQuery } from '@tanstack/react-query';
import { client, unwrap, type Schemas } from '@/api/client';

/** Every cache key of the package screens, so a change invalidates by name instead of by repeated string literals. */
export const packageKeys = {
  all: ['packages'] as const,
  runtime: (name: Schemas['JobPackageName']) => ['packages', name] as const,
  bundles: ['task-bundles'] as const,
  /** A bundle's target moves the task's row and presets, so the catalog screen shows it too. */
  catalog: ['task-catalog'] as const,
};

/** A runtime package or a task bundle: the fields the package table shows (a bundle also has a slug). */
export type Pkg = Schemas['JobPackage'];

export const useRuntimePackages = (name: Schemas['JobPackageName']) =>
  useQuery({ queryKey: packageKeys.runtime(name), queryFn: () => unwrap(client.GET('/v1/admin/job-packages/{name}', { params: { path: { name } } })) });

export const useTaskBundles = () => useQuery({ queryKey: packageKeys.bundles, queryFn: () => unwrap(client.GET('/v1/admin/task-bundles')) });
