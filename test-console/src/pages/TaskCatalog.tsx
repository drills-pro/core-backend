import { Badge, Button, Code, HStack, Stack, Table, Text } from '@chakra-ui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, unwrap, type Schemas } from '@/api/client';
import { ErrorAlert, Section, StatusBadge } from '@/ui';

const fetchCatalog = () => unwrap(client.GET('/v1/admin/task-bundles/catalog'));
/** From the call, not Schemas['CatalogTask']: the generated component type and the response type differ in tuple-ness. */
type Entry = Awaited<ReturnType<typeof fetchCatalog>>[number];

const KEY = ['task-catalog'];
const SHA_SHOWN = 12;

const EnabledBadge = ({ enabled }: { enabled: boolean }) => <Badge colorPalette={enabled ? 'green' : 'gray'}>{enabled ? 'enabled' : 'disabled'}</Badge>;

export function TaskCatalogPage() {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: KEY, queryFn: fetchCatalog });
  const refresh = () => qc.invalidateQueries({ queryKey: KEY });
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

  return (
    <Stack gap="4">
      <Text color="fg.muted">
        What tenants can train, as the stable task bundles derived it. A disabled task or preset is hidden from tenants and refused for new runs; promoting a bundle
        leaves the switch as it is.
      </Text>
      <ErrorAlert error={list.error ?? toggleTask.error ?? togglePreset.error} />
      {list.data?.map((e) => (
        <TaskCard
          key={e.task.slug}
          entry={e}
          onTask={(enabled) => toggleTask.mutate({ slug: e.task.slug, enabled })}
          onPreset={(name, enabled) => togglePreset.mutate({ slug: e.task.slug, name, enabled })}
          busy={toggleTask.isPending || togglePreset.isPending}
        />
      ))}
    </Stack>
  );
}

function TaskCard({ entry, onTask, onPreset, busy }: { entry: Entry; onTask: (enabled: boolean) => void; onPreset: (name: string, enabled: boolean) => void; busy: boolean }) {
  const { task, presets, bundle, auto_promote } = entry;
  return (
    <Section
      title={task.slug}
      actions={
        <HStack>
          <EnabledBadge enabled={task.enabled} />
          <Button size="xs" variant="outline" disabled={busy} onClick={() => onTask(!task.enabled)}>
            {task.enabled ? 'Disable' : 'Enable'}
          </Button>
        </HStack>
      }
    >
      <HStack wrap="wrap" gap="3">
        <Text>
          data kind <Code>{task.data_kind}</Code>
        </Text>
        <Text>
          trainer <Code>{task.trainer ?? '-'}</Code>
        </Text>
        <Text>{task.servable ? 'servable' : 'not servable'}</Text>
        <Text>auto-promote {auto_promote ? 'on' : 'off'}</Text>
        {bundle ? (
          <HStack>
            <Text>bundle</Text>
            <Code>{bundle.sha256.slice(0, SHA_SHOWN)}</Code>
            <StatusBadge status={bundle.smoke_status} />
          </HStack>
        ) : (
          <Text color="fg.muted">no bundle (a task from before bundles)</Text>
        )}
      </HStack>
      <Table.Root size="sm">
        <Table.Header>
          <Table.Row>
            <Table.ColumnHeader>Preset</Table.ColumnHeader>
            <Table.ColumnHeader>Base model</Table.ColumnHeader>
            <Table.ColumnHeader>GPU</Table.ColumnHeader>
            <Table.ColumnHeader>Status</Table.ColumnHeader>
            <Table.ColumnHeader />
          </Table.Row>
        </Table.Header>
        <Table.Body>
          {presets.map((p) => (
            <Table.Row key={p.name}>
              <Table.Cell>{p.name}</Table.Cell>
              <Table.Cell>{p.base_model ? `${p.base_model.repo_id}@${p.base_model.revision.slice(0, 8)}` : '-'}</Table.Cell>
              <Table.Cell>{p.gpu}</Table.Cell>
              <Table.Cell>
                <EnabledBadge enabled={p.enabled} />
              </Table.Cell>
              <Table.Cell>
                <HStack justify="end">
                  <Button size="xs" variant="outline" disabled={busy} onClick={() => onPreset(p.name, !p.enabled)}>
                    {p.enabled ? 'Disable' : 'Enable'}
                  </Button>
                </HStack>
              </Table.Cell>
            </Table.Row>
          ))}
        </Table.Body>
      </Table.Root>
    </Section>
  );
}
