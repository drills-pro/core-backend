import { Badge, Stack, Tabs } from '@chakra-ui/react';
import { useQueries } from '@tanstack/react-query';
import { client, unwrap } from '@/api/client';
import { packageKeys, type Pkg } from '@/api/packages';
import { PACKAGE_ERROR_MESSAGES, PACKAGE_NAMES, PackageStatus } from '@/constants';
import { PackageList } from '@/pages/PackageList';
import { TaskBundlesSection } from '@/pages/TaskBundles';
import { CodedError, PageHeader, Section } from '@/ui';

export function PackagesPage() {
  const lists = useQueries({
    queries: PACKAGE_NAMES.map((name) => ({
      queryKey: packageKeys.runtime(name),
      queryFn: () => unwrap(client.GET('/v1/admin/job-packages/{name}', { params: { path: { name } } })),
    })),
  });

  return (
    <Stack gap="4">
      <PageHeader
        title="Packages"
        description="Read-only. CI sends each new version; once its smoke job passes, it becomes the active package for new runs and the one it replaces is retired. Rolling back is a manual run of the publish workflow."
      />
      <Tabs.Root defaultValue="runtime" variant="line" colorPalette="orange" lazyMount>
        <Tabs.List>
          <Tabs.Trigger value="runtime">Runtime packages</Tabs.Trigger>
          <Tabs.Trigger value="bundles">Task bundles</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content colorPalette="gray" value="runtime">
          <Stack gap="4">
            {PACKAGE_NAMES.map((name, i) => {
              const packages: Pkg[] = lists[i].data ?? [];
              const active = packages.find((p) => p.status === PackageStatus.STABLE);
              return (
                <Section key={name} title={name} actions={active ? <Badge colorPalette="green">active {active.sha256.slice(0, 8)}</Badge> : <Badge colorPalette="orange">none active</Badge>}>
                  <CodedError error={lists[i].error} messages={PACKAGE_ERROR_MESSAGES} />
                  <PackageList packages={packages} />
                </Section>
              );
            })}
          </Stack>
        </Tabs.Content>
        <Tabs.Content colorPalette="gray" value="bundles">
          <TaskBundlesSection />
        </Tabs.Content>
      </Tabs.Root>
    </Stack>
  );
}
