import { useState } from 'react';
import { Button, Code, HStack, Stack, Table, Text } from '@chakra-ui/react';
import { useQueries, useQueryClient } from '@tanstack/react-query';
import { client, unwrap, type Schemas } from '@/api/client';
import { PACKAGE_ERROR_MESSAGES, PACKAGE_NAMES, PackageStatus } from '@/constants';
import { ConfirmChange, type Change } from '@/pages/ConfirmChange';
import { TaskBundlesSection } from '@/pages/TaskBundles';
import { CodedError, Section, StatusBadge, when } from '@/ui';


export function PackagesPage() {
  const qc = useQueryClient();
  const lists = useQueries({
    queries: PACKAGE_NAMES.map((name) => ({
      queryKey: ['packages', name],
      queryFn: () => unwrap(client.GET('/v1/admin/job-packages/{name}', { params: { path: { name } } })),
    })),
  });
  const [change, setChange] = useState<Change | null>(null);

  return (
    <Stack gap="4">
      <Text color="fg.muted">
        Changes here apply to every tenant: new runs use the newest stable package. A candidate becomes stable only after its smoke job passed.
      </Text>
      {PACKAGE_NAMES.map((name, i) => (
        <Section key={name} title={name}>
          <CodedError error={lists[i].error} messages={PACKAGE_ERROR_MESSAGES} />
          <Table.Root size="sm">
            <Table.Header>
              <Table.Row>
                <Table.ColumnHeader>sha256</Table.ColumnHeader>
                <Table.ColumnHeader>Status</Table.ColumnHeader>
                <Table.ColumnHeader>Smoke</Table.ColumnHeader>
                <Table.ColumnHeader>Published by</Table.ColumnHeader>
                <Table.ColumnHeader>Updated</Table.ColumnHeader>
                <Table.ColumnHeader />
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {lists[i].data?.map((pkg) => (
                <Table.Row key={pkg.sha256}>
                  <Table.Cell>
                    <Code>{pkg.sha256.slice(0, 12)}</Code>
                  </Table.Cell>
                  <Table.Cell>
                    <StatusBadge status={pkg.status} />
                  </Table.Cell>
                  <Table.Cell>
                    <StatusBadge status={pkg.smoke_status} />
                    {pkg.smoke_job_id && <Code ml="1" fontSize="xs">{pkg.smoke_job_id}</Code>}
                  </Table.Cell>
                  <Table.Cell>{pkg.published_by}</Table.Cell>
                  <Table.Cell>
                    {when(pkg.updated_at)} by {pkg.updated_by}
                  </Table.Cell>
                  <Table.Cell>
                    <HStack justify="end">
                      {pkg.status === PackageStatus.CANDIDATE && (
                        <Button size="xs" colorPalette="green" onClick={() => setChange({ name: pkg.name, sha256: pkg.sha256, status: PackageStatus.STABLE })}>
                          Promote
                        </Button>
                      )}
                      {pkg.status !== PackageStatus.RETIRED && (
                        <Button size="xs" variant="outline" colorPalette="red" onClick={() => setChange({ name: pkg.name, sha256: pkg.sha256, status: PackageStatus.RETIRED })}>
                          Retire
                        </Button>
                      )}
                    </HStack>
                  </Table.Cell>
                </Table.Row>
              ))}
            </Table.Body>
          </Table.Root>
        </Section>
      ))}
      {change && (
        <ConfirmChange
          change={change}
          request={() =>
            unwrap(
              client.PUT('/v1/admin/job-packages/{name}/{sha256}/status', {
                params: { path: { name: change.name as Schemas['JobPackageName'], sha256: change.sha256 } },
                body: { status: change.status as Schemas['StatusBody']['status'] },
              }),
            )
          }
          onClose={() => setChange(null)}
          onDone={() => qc.invalidateQueries({ queryKey: ['packages'] })}
        />
      )}
      <TaskBundlesSection />
    </Stack>
  );
}
