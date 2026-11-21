import { useState } from 'react';
import { Stack, Text } from '@chakra-ui/react';
import { useQueries } from '@tanstack/react-query';
import { client, unwrap } from '@/api/client';
import { packageKeys, type Pkg } from '@/api/packages';
import { PACKAGE_ERROR_MESSAGES, PACKAGE_NAMES, PackageStatus } from '@/constants';
import { ConfirmChange, type Change } from '@/pages/ConfirmChange';
import { PackageTable } from '@/pages/PackageTable';
import { TaskBundlesSection } from '@/pages/TaskBundles';
import { CodedError, Section } from '@/ui';

export function PackagesPage() {
  const lists = useQueries({
    queries: PACKAGE_NAMES.map((name) => ({
      queryKey: packageKeys.runtime(name),
      queryFn: () => unwrap(client.GET('/v1/admin/job-packages/{name}', { params: { path: { name } } })),
    })),
  });
  const [change, setChange] = useState<Change | null>(null);

  return (
    <Stack gap="4">
      <Text color="fg.muted">
        Changes here apply to every tenant. Each name has one active (stable) package, the one new runs use; promoting another retires the current one automatically.
        A candidate can be promoted only after its smoke job passed.
      </Text>
      {PACKAGE_NAMES.map((name, i) => {
        const packages: Pkg[] = lists[i].data ?? [];
        return (
          <Section key={name} title={name}>
            <CodedError error={lists[i].error} messages={PACKAGE_ERROR_MESSAGES} />
            <PackageTable
              packages={packages}
              onAction={(a) => setChange({ ...a, replaces: packages.find((p) => p.status === PackageStatus.STABLE) })}
            />
          </Section>
        );
      })}
      {change && <ConfirmChange change={change} onClose={() => setChange(null)} />}
      <TaskBundlesSection />
    </Stack>
  );
}
