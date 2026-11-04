import { useState } from 'react';
import { Button, Code, Dialog, HStack, Input, Portal, Stack, Table, Text } from '@chakra-ui/react';
import { useMutation, useQueries, useQueryClient } from '@tanstack/react-query';
import { client, unwrap, type Schemas } from '@/api/client';
import { PACKAGE_NAMES, PackageStatus, SHA_CONFIRM_CHARS } from '@/constants';
import { ErrorAlert, Section, StatusBadge, when } from '@/ui';

type Pkg = Schemas['JobPackage'];
type Change = { pkg: Pkg; status: Schemas['PackageStatus'] };

const phrase = (pkg: Pkg) => `${pkg.name} ${pkg.sha256.slice(0, SHA_CONFIRM_CHARS)}`;

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
          <ErrorAlert error={lists[i].error} />
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
                        <Button size="xs" colorPalette="green" onClick={() => setChange({ pkg, status: PackageStatus.STABLE })}>
                          Promote
                        </Button>
                      )}
                      {pkg.status !== PackageStatus.RETIRED && (
                        <Button size="xs" variant="outline" colorPalette="red" onClick={() => setChange({ pkg, status: PackageStatus.RETIRED })}>
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
      {change && <ConfirmChange change={change} onClose={() => setChange(null)} onDone={() => qc.invalidateQueries({ queryKey: ['packages'] })} />}
    </Stack>
  );
}

/** Typing the package name and the start of its sha back is the guard: a misclick cannot promote the wrong package. */
function ConfirmChange({ change, onClose, onDone }: { change: Change; onClose: () => void; onDone: () => void }) {
  const [typed, setTyped] = useState('');
  const { pkg, status } = change;
  const save = useMutation({
    mutationFn: () =>
      unwrap(client.PUT('/v1/admin/job-packages/{name}/{sha256}/status', { params: { path: { name: pkg.name, sha256: pkg.sha256 } }, body: { status } })),
    onSuccess: () => {
      onDone();
      onClose();
    },
  });
  return (
    <Dialog.Root open onOpenChange={(e) => !e.open && onClose()}>
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content>
            <Dialog.Header>
              <Dialog.Title>
                Set {pkg.name} to {status}
              </Dialog.Title>
            </Dialog.Header>
            <Dialog.Body>
              <Stack gap="3">
                <Text>
                  Type <Code>{phrase(pkg)}</Code> to confirm.
                </Text>
                <Input autoFocus value={typed} onChange={(e) => setTyped(e.target.value)} />
                <ErrorAlert error={save.error} />
              </Stack>
            </Dialog.Body>
            <Dialog.Footer>
              <Button variant="outline" onClick={onClose}>
                Cancel
              </Button>
              <Button
                colorPalette={status === PackageStatus.STABLE ? 'green' : 'red'}
                disabled={typed !== phrase(pkg)}
                loading={save.isPending}
                onClick={() => save.mutate()}
              >
                Set {status}
              </Button>
            </Dialog.Footer>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
