import { Badge, Card, Code, HStack, Separator, Stack, Switch, Text } from '@chakra-ui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, unwrap, type Schemas } from '@/api/client';
import { packageKeys } from '@/api/packages';
import { CardGrid, CardSkeletons, Empty, ErrorAlert, Facts, PageHeader, StatusBadge } from '@/ui';

const fetchCatalog = () => unwrap(client.GET('/v1/admin/task-bundles/catalog'));
/** From the call, not Schemas['CatalogTask']: the generated component type and the response type differ in tuple-ness. */
type Entry = Awaited<ReturnType<typeof fetchCatalog>>[number];

const SHA_SHOWN = 12;
const REVISION_SHOWN = 8;

export function TaskCatalogPage() {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: packageKeys.catalog, queryFn: fetchCatalog });
  const refresh = () => qc.invalidateQueries({ queryKey: packageKeys.catalog });
  const toggleTask = useMutation({
    mutationFn: ({ slug, enabled }: { slug: Schemas['TaskType']; enabled: boolean }) =>
      unwrap(client.PUT('/v1/admin/task-bundles/catalog/{slug}/enabled', { params: { path: { slug } }, body: { enabled } })),
    onSuccess: refresh,
  });
  const togglePreset = useMutation({
    mutationFn: ({ slug, name, enabled }: { slug: Schemas['TaskType']; name: string; enabled: boolean }) =>
      unwrap(client.PUT('/v1/admin/task-bundles/catalog/{slug}/presets/{name}/enabled', { params: { path: { slug, name } }, body: { enabled } })),
    onSuccess: refresh,
  });
  const busy = toggleTask.isPending || togglePreset.isPending;

  return (
    <Stack gap="4">
      <PageHeader
        title="Task catalog"
        description="What tenants can train, as the stable task bundles derived it. A disabled task or preset is hidden from tenants and refused for new runs; promoting a bundle leaves the switch as it is."
      />
      <ErrorAlert error={list.error ?? toggleTask.error ?? togglePreset.error} />
      {list.isPending && <CardSkeletons />}
      {list.data?.length === 0 && <Empty title="No tasks yet">Promote a task bundle to add one.</Empty>}
      <CardGrid min="26rem">
        {list.data?.map((e) => (
          <TaskCard
            key={e.task.slug}
            entry={e}
            busy={busy}
            onTask={(enabled) => toggleTask.mutate({ slug: e.task.slug, enabled })}
            onPreset={(name, enabled) => togglePreset.mutate({ slug: e.task.slug, name, enabled })}
          />
        ))}
      </CardGrid>
    </Stack>
  );
}

function EnabledSwitch({ checked, disabled, onChange, label }: { checked: boolean; disabled: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <Switch.Root size="sm" colorPalette="green" checked={checked} disabled={disabled} onCheckedChange={(e) => onChange(e.checked)}>
      <Switch.HiddenInput aria-label={label} />
      <Switch.Control />
    </Switch.Root>
  );
}

function TaskCard({ entry, onTask, onPreset, busy }: { entry: Entry; onTask: (enabled: boolean) => void; onPreset: (name: string, enabled: boolean) => void; busy: boolean }) {
  const { task, presets, bundle, auto_promote } = entry;
  return (
    <Card.Root variant="outline" opacity={task.enabled ? 1 : 0.8}>
      <Card.Header>
        <HStack justify="space-between">
          <Card.Title>{task.slug}</Card.Title>
          <EnabledSwitch checked={task.enabled} disabled={busy} onChange={onTask} label={`${task.slug} enabled`} />
        </HStack>
        <HStack>
          <Badge colorPalette={task.enabled ? 'green' : 'gray'}>{task.enabled ? 'enabled' : 'disabled'}</Badge>
          <Badge variant="surface">{task.servable ? 'servable' : 'not servable'}</Badge>
        </HStack>
      </Card.Header>
      <Card.Body gap="3">
        <Facts
          items={[
            ['Data kind', <Code key="k">{task.data_kind}</Code>],
            ['Trainer', <Code key="t">{task.trainer ?? '-'}</Code>],
            ['Auto-promote', auto_promote ? 'on' : 'off'],
            [
              'Bundle',
              bundle ? (
                <HStack key="b">
                  <Code>{bundle.sha256.slice(0, SHA_SHOWN)}</Code>
                  <StatusBadge status={bundle.smoke_status} />
                </HStack>
              ) : (
                <Text key="b" color="fg.muted">
                  none (a task from before bundles)
                </Text>
              ),
            ],
          ]}
        />
        <Separator />
        <Text fontSize="sm" fontWeight="semibold">
          Presets ({presets.length})
        </Text>
        <Stack gap="2">
          {presets.map((p) => (
            <HStack key={p.name} justify="space-between" p="2" rounded="md" bg="bg.subtle" opacity={p.enabled ? 1 : 0.7}>
              <Stack gap="0">
                <Text fontWeight="medium" fontSize="sm">
                  {p.name}
                </Text>
                <Text fontSize="xs" color="fg.muted">
                  {p.base_model ? `${p.base_model.repo_id}@${p.base_model.revision.slice(0, REVISION_SHOWN)}` : 'no base model'} · {p.gpu ? `${p.gpu} GPU` : 'CPU'}
                </Text>
              </Stack>
              <EnabledSwitch checked={p.enabled} disabled={busy} onChange={(v) => onPreset(p.name, v)} label={`${p.name} enabled`} />
            </HStack>
          ))}
        </Stack>
      </Card.Body>
    </Card.Root>
  );
}
