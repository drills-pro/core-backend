import { useState } from 'react';
import { Button, Heading, Stack, Text } from '@chakra-ui/react';
import { useQueries, useQuery } from '@tanstack/react-query';
import { bundlesQuery, runtimeQuery, usePublishRuntime, useRetrySmoke, type Bundle, type TaskSlug, type Target } from '@/api/packages';
import { PACKAGE_ERROR_MESSAGES, PACKAGE_NAMES } from '@/constants';
import { ChangeDialog, type ChangeRequest } from '@/pages/packages/ChangeDialog';
import { PackageGroup } from '@/pages/packages/PackageCard';
import { CardSkeletons, CodedError, Empty, PageHeader } from '@/ui';

/** Every package the platform runs, as cards: the runtime code (one active package per name) and the task bundles (one
 * active bundle per task). Each card carries its own actions for its state. */
export function PackagesPage() {
  const runtime = useQueries({ queries: PACKAGE_NAMES.map(runtimeQuery) });
  const bundles = useQuery(bundlesQuery);
  const publish = usePublishRuntime();
  const retry = useRetrySmoke();
  const [change, setChange] = useState<ChangeRequest | null>(null);

  const bySlug = new Map<TaskSlug, Bundle[]>();
  for (const b of bundles.data ?? []) bySlug.set(b.slug, [...(bySlug.get(b.slug) ?? []), b]);
  const error = runtime.find((r) => r.error)?.error ?? bundles.error ?? publish.error ?? retry.error;
  const open = (target: Target, action: ChangeRequest['action'], pkg: ChangeRequest['pkg'], replaces?: ChangeRequest['pkg']) =>
    setChange({ target, action, pkg, replaces });

  return (
    <Stack gap="8">
      <PageHeader
        title="Packages"
        description="Changes here apply to every tenant. Promoting a package retires the one it replaces, and a manual promotion sticks until CI registers a newer version. A candidate can be promoted only after its smoke job passed."
      />
      <CodedError error={error} messages={PACKAGE_ERROR_MESSAGES} />

      <Stack gap="2">
        <Heading size="lg">Runtime packages</Heading>
        <Text color="fg.muted" fontSize="sm">
          The code every run uses. Publish this build stages the code in the running core-api image as a candidate; code merged after that image was built is
          not in it until the image is redeployed.
        </Text>
      </Stack>
      <Stack gap="8">
        {PACKAGE_NAMES.map((name, i) => (
          <PackageGroup
            key={name}
            title={name}
            description="One active package. Promoting another retires the current one."
            packages={runtime[i].data ?? []}
            headerAction={
              <Button
                size="sm"
                variant="outline"
                title="Stages the code in the running core-api image as a candidate"
                loading={publish.isPending && publish.variables === name}
                onClick={() => publish.mutate(name)}
              >
                Publish this build
              </Button>
            }
            retryingSha={retry.isPending ? retry.variables?.sha256 : undefined}
            onAction={open}
            onRetry={(t) => retry.mutate(t)}
          />
        ))}
      </Stack>

      <Stack gap="2">
        <Heading size="lg">Task bundles</Heading>
        <Text color="fg.muted" fontSize="sm">
          The trainer, serving functions and presets of each task. Promoting a bundle rewrites its task's row and presets from its manifest; the task catalog
          holds the switches and settings that survive a promotion.
        </Text>
      </Stack>
      {bundles.isPending && <CardSkeletons count={2} />}
      {bundles.data?.length === 0 && <Empty title="No task bundles registered" />}
      <Stack gap="8">
        {[...bySlug].map(([slug, list]) => (
          <PackageGroup
            key={slug}
            title={`task_${slug}`}
            description="One active bundle for this task."
            packages={list}
            slug={slug}
            retryingSha={retry.isPending ? retry.variables?.sha256 : undefined}
            onAction={open}
            onRetry={(t) => retry.mutate(t)}
          />
        ))}
      </Stack>

      {change && <ChangeDialog request={change} onClose={() => setChange(null)} />}
    </Stack>
  );
}
