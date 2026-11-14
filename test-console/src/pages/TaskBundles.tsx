import { useState } from 'react';
import { Button, Code, Dialog, HStack, Portal, Stack, Table, Text } from '@chakra-ui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, unwrap, type Schemas } from '@/api/client';
import { PACKAGE_ERROR_MESSAGES, PackageStatus, SmokeStatus } from '@/constants';
import { ConfirmChange, type Change } from '@/pages/ConfirmChange';
import { CodedError, ErrorAlert, Section, StatusBadge, when } from '@/ui';

type Bundle = Schemas['TaskBundle'];
type StatusBody = Schemas['BundleStatusBody']['status'];

/** The parts of a bundle's manifest.json this screen shows (planning/25); the API types it as a free object. */
type Manifest = {
  slug?: string;
  data_kind?: string;
  trainer?: string;
  hyperparameters?: unknown;
  presets?: { name: string; base_model?: string | null; gpu?: number; vram_gb?: number | null }[];
};
const manifestOf = (b: Bundle) => (b.manifest ?? {}) as Manifest;

const BUNDLES_KEY = ['task-bundles'];
/** A status change moves the task's row and presets, so the catalog screen refetches too. */
const CATALOG_KEY = ['task-catalog'];

export function TaskBundlesSection() {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: BUNDLES_KEY, queryFn: () => unwrap(client.GET('/v1/admin/task-bundles')) });
  const [change, setChange] = useState<(Change & { slug: Schemas['TaskType'] }) | null>(null);
  const [detail, setDetail] = useState<Bundle | null>(null);
  const refresh = () => Promise.all([qc.invalidateQueries({ queryKey: BUNDLES_KEY }), qc.invalidateQueries({ queryKey: CATALOG_KEY })]);
  const retry = useMutation({
    mutationFn: (b: Bundle) => unwrap(client.POST('/v1/admin/task-bundles/{slug}/{sha256}/smoke', { params: { path: { slug: b.slug, sha256: b.sha256 } } })),
    onSuccess: refresh,
  });

  const bySlug = new Map<Schemas['TaskType'], Bundle[]>();
  for (const b of list.data ?? []) bySlug.set(b.slug, [...(bySlug.get(b.slug) ?? []), b]);

  return (
    <Stack gap="4">
      <Text color="fg.muted">
        Task bundles: one per task type, each carrying the trainer, the hyperparameter schema and the presets. Promoting one rewrites that task's row and presets;
        promoting an older sha is the rollback.
      </Text>
      <ErrorAlert error={list.error} />
      <CodedError error={retry.error} messages={PACKAGE_ERROR_MESSAGES} />
      {[...bySlug].map(([slug, bundles]) => {
        const stable = bundles.find((b) => b.status === PackageStatus.STABLE);
        return (
          <Section key={slug} title={`task_${slug}`} actions={<AutoPromote slug={slug} />}>
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
                {bundles.map((b) => {
                  // ponytail: a retired bundle is a former stable one (or a retired candidate); promoting it is labelled the rollback.
                  const rollback = !!stable && b.status === PackageStatus.RETIRED;
                  const ask = (status: StatusBody, isRollback = false) => setChange({ slug, name: b.name, sha256: b.sha256, status, rollback: isRollback });
                  return (
                    <Table.Row key={b.sha256}>
                      <Table.Cell>
                        <Code>{b.sha256.slice(0, 12)}</Code>
                      </Table.Cell>
                      <Table.Cell>
                        <StatusBadge status={b.status} />
                      </Table.Cell>
                      <Table.Cell>
                        <StatusBadge status={b.smoke_status} />
                        {b.smoke_job_id && (
                          <Code ml="1" fontSize="xs">
                            {b.smoke_job_id}
                          </Code>
                        )}
                      </Table.Cell>
                      <Table.Cell>{b.published_by}</Table.Cell>
                      <Table.Cell>
                        {when(b.updated_at)} by {b.updated_by}
                      </Table.Cell>
                      <Table.Cell>
                        <HStack justify="end">
                          <Button size="xs" variant="outline" onClick={() => setDetail(b)}>
                            Manifest
                          </Button>
                          {b.status === PackageStatus.CANDIDATE && b.smoke_status === SmokeStatus.FAILED && (
                            <Button size="xs" variant="outline" loading={retry.isPending && retry.variables?.sha256 === b.sha256} onClick={() => retry.mutate(b)}>
                              Retry smoke
                            </Button>
                          )}
                          {b.status !== PackageStatus.STABLE && b.smoke_status === SmokeStatus.PASSED && (
                            <Button size="xs" colorPalette="green" onClick={() => ask(PackageStatus.STABLE, rollback)}>
                              {rollback ? 'Roll back to this' : 'Promote'}
                            </Button>
                          )}
                          {b.status === PackageStatus.CANDIDATE && (
                            <Button size="xs" variant="outline" colorPalette="red" onClick={() => ask(PackageStatus.RETIRED)}>
                              Retire
                            </Button>
                          )}
                        </HStack>
                      </Table.Cell>
                    </Table.Row>
                  );
                })}
              </Table.Body>
            </Table.Root>
          </Section>
        );
      })}
      {list.data?.length === 0 && <Text color="fg.muted">No task bundles registered.</Text>}
      {change && (
        <ConfirmChange
          change={change}
          request={() =>
            unwrap(
              client.PUT('/v1/admin/task-bundles/{slug}/{sha256}/status', {
                params: { path: { slug: change.slug, sha256: change.sha256 } },
                body: { status: change.status as StatusBody },
              }),
            )
          }
          onClose={() => setChange(null)}
          onDone={refresh}
        />
      )}
      {detail && <ManifestDialog bundle={detail} onClose={() => setDetail(null)} />}
    </Stack>
  );
}

/** Whether a bundle whose smoke passed goes stable without a person (core-api's poller), per task. */
function AutoPromote({ slug }: { slug: Schemas['TaskType'] }) {
  const qc = useQueryClient();
  const key = ['task-bundle-settings', slug];
  const settings = useQuery({ queryKey: key, queryFn: () => unwrap(client.GET('/v1/admin/task-bundles/{slug}/settings', { params: { path: { slug } } })) });
  const set = useMutation({
    mutationFn: (auto_promote: boolean) => unwrap(client.PUT('/v1/admin/task-bundles/{slug}/settings', { params: { path: { slug } }, body: { auto_promote } })),
    onSuccess: (data) => {
      qc.setQueryData(key, data);
      return qc.invalidateQueries({ queryKey: CATALOG_KEY });
    },
  });
  if (settings.error || set.error) return <ErrorAlert error={settings.error ?? set.error} />;
  if (!settings.data) return null;
  const on = settings.data.auto_promote;
  return (
    <Button size="xs" variant="outline" loading={set.isPending} onClick={() => set.mutate(!on)}>
      Auto-promote: {on ? 'on' : 'off'}
    </Button>
  );
}

function ManifestDialog({ bundle, onClose }: { bundle: Bundle; onClose: () => void }) {
  const m = manifestOf(bundle);
  return (
    <Dialog.Root open size="xl" scrollBehavior="inside" onOpenChange={(e) => !e.open && onClose()}>
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content>
            <Dialog.Header>
              <Dialog.Title>
                {bundle.name} <Code>{bundle.sha256.slice(0, 12)}</Code>
              </Dialog.Title>
            </Dialog.Header>
            <Dialog.Body>
              <Stack gap="3">
                <Text>
                  Slug <Code>{m.slug ?? '-'}</Code> | data kind <Code>{m.data_kind ?? '-'}</Code> | trainer <Code>{m.trainer ?? '-'}</Code>
                </Text>
                <Text fontWeight="semibold">Hyperparameters (JSON Schema)</Text>
                <Code display="block" whiteSpace="pre" overflow="auto" p="2">
                  {JSON.stringify(m.hyperparameters ?? {}, null, 2)}
                </Code>
                <Text fontWeight="semibold">Presets</Text>
                <Table.Root size="sm">
                  <Table.Header>
                    <Table.Row>
                      <Table.ColumnHeader>Name</Table.ColumnHeader>
                      <Table.ColumnHeader>Base model</Table.ColumnHeader>
                      <Table.ColumnHeader>GPU</Table.ColumnHeader>
                      <Table.ColumnHeader>VRAM (GB)</Table.ColumnHeader>
                    </Table.Row>
                  </Table.Header>
                  <Table.Body>
                    {m.presets?.map((p) => (
                      <Table.Row key={p.name}>
                        <Table.Cell>{p.name}</Table.Cell>
                        <Table.Cell>{p.base_model ?? '-'}</Table.Cell>
                        <Table.Cell>{p.gpu ?? '-'}</Table.Cell>
                        <Table.Cell>{p.vram_gb ?? '-'}</Table.Cell>
                      </Table.Row>
                    ))}
                  </Table.Body>
                </Table.Root>
              </Stack>
            </Dialog.Body>
            <Dialog.Footer>
              <Button variant="outline" onClick={onClose}>
                Close
              </Button>
            </Dialog.Footer>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
