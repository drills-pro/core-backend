import { useState } from 'react';
import { Badge, Button, Card, Code, Dialog, HStack, Portal, Stack, Switch, Text } from '@chakra-ui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, unwrap, type Schemas } from '@/api/client';
import { packageKeys, useTaskBundles } from '@/api/packages';
import { PACKAGE_ERROR_MESSAGES, PackageStatus } from '@/constants';
import { ConfirmChange, type Change } from '@/pages/ConfirmChange';
import { PackageList } from '@/pages/PackageList';
import { CardGrid, CardSkeletons, CodedError, Empty, ErrorAlert, Section } from '@/ui';

type Bundle = Schemas['TaskBundle'];

/** The parts of a bundle's manifest.json this screen shows (planning/25); the API types it as a free object. */
type Manifest = {
  slug?: string;
  data_kind?: string;
  trainer?: string;
  hyperparameters?: unknown;
  presets?: { name: string; base_model?: string | null; gpu?: number; vram_gb?: number | null }[];
};
const manifestOf = (b: Bundle) => (b.manifest ?? {}) as Manifest;

export function TaskBundlesSection() {
  const qc = useQueryClient();
  const list = useTaskBundles();
  const [change, setChange] = useState<Change | null>(null);
  const [detail, setDetail] = useState<Bundle | null>(null);
  const retry = useMutation({
    mutationFn: (b: Bundle) => unwrap(client.POST('/v1/admin/task-bundles/{slug}/{sha256}/smoke', { params: { path: { slug: b.slug, sha256: b.sha256 } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: packageKeys.bundles }),
  });

  const bySlug = new Map<Schemas['TaskType'], Bundle[]>();
  for (const b of list.data ?? []) bySlug.set(b.slug, [...(bySlug.get(b.slug) ?? []), b]);

  return (
    <Stack gap="4">
      <Text color="fg.muted">
        Task bundles: one per task type, each carrying the trainer, the hyperparameter schema and the presets. Promoting one rewrites that task's row and presets and
        retires the previous active bundle; promoting an older sha is the rollback.
      </Text>
      <ErrorAlert error={list.error} />
      <CodedError error={retry.error} messages={PACKAGE_ERROR_MESSAGES} />
      {[...bySlug].map(([slug, bundles]) => (
        <Section key={slug} title={`task_${slug}`} actions={<AutoPromote slug={slug} />}>
          <PackageList
            packages={bundles}
            retrying={retry.isPending ? retry.variables?.sha256 : undefined}
            onRetry={(p) => retry.mutate(p as Bundle)}
            onAction={(a) => setChange({ ...a, slug, replaces: bundles.find((b) => b.status === PackageStatus.STABLE) })}
            extra={(p) => (
              <Button size="xs" variant="outline" onClick={() => setDetail(p as Bundle)}>
                Manifest
              </Button>
            )}
          />
        </Section>
      ))}
      {list.isPending && <CardSkeletons count={2} />}
      {list.data?.length === 0 && <Empty title="No task bundles registered" />}
      {change && <ConfirmChange change={change} onClose={() => setChange(null)} />}
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
      return qc.invalidateQueries({ queryKey: packageKeys.catalog });
    },
  });
  if (settings.error || set.error) return <ErrorAlert error={settings.error ?? set.error} />;
  if (!settings.data) return null;
  return (
    <Switch.Root size="sm" colorPalette="orange" checked={settings.data.auto_promote} disabled={set.isPending} onCheckedChange={(e) => set.mutate(e.checked)}>
      <Switch.HiddenInput />
      <Switch.Control />
      <Switch.Label>Auto-promote when smoke passes</Switch.Label>
    </Switch.Root>
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
                <CardGrid min="14rem">
                  {m.presets?.map((p) => (
                    <Card.Root key={p.name} size="sm" variant="subtle">
                      <Card.Body gap="1">
                        <Text fontWeight="semibold">{p.name}</Text>
                        <Text fontSize="sm" color="fg.muted">
                          {p.base_model ?? 'no base model'}
                        </Text>
                        <HStack>
                          <Badge>{p.gpu ? `${p.gpu} GPU` : 'CPU'}</Badge>
                          {p.vram_gb != null && <Badge>{p.vram_gb} GB VRAM</Badge>}
                        </HStack>
                      </Card.Body>
                    </Card.Root>
                  ))}
                </CardGrid>
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
