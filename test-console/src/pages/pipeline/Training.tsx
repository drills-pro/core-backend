import { useState } from 'react';
import { Button, Code, HStack, Table } from '@chakra-ui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, newKey, unwrap } from '@/api/client';
import { DEFAULT_SEED, POLL_MS, RUN_DONE } from '@/constants';
import { ErrorAlert, Section, Select, short, StatusBadge, when } from '@/ui';
import { versionsQuery } from './Datasets';

export const runsQuery = (projectId: string) => ({
  queryKey: ['runs', projectId],
  queryFn: () => unwrap(client.GET('/v1/projects/{project_id}/training-runs', { params: { path: { project_id: projectId } } })),
});

export function Training({ projectId, datasetId }: { projectId: string; datasetId: string }) {
  const qc = useQueryClient();
  const presets = useQuery({
    queryKey: ['presets', projectId],
    queryFn: () => unwrap(client.GET('/v1/projects/{project_id}/training-presets', { params: { path: { project_id: projectId } } })),
  });
  const versions = useQuery(versionsQuery(projectId, datasetId));
  const runs = useQuery({
    ...runsQuery(projectId),
    refetchInterval: (q) => (q.state.data?.some((r) => !RUN_DONE.includes(r.status)) ? POLL_MS : false),
  });
  const [versionId, setVersionId] = useState('');
  const [preset, setPreset] = useState('');
  const [logsOf, setLogsOf] = useState<string | null>(null);

  const start = useMutation({
    mutationFn: () =>
      unwrap(
        client.POST('/v1/projects/{project_id}/training-runs', {
          params: { path: { project_id: projectId }, header: { 'Idempotency-Key': newKey() } },
          body: { dataset_version_id: versionId, preset, seed: DEFAULT_SEED },
        }),
      ),
    onSettled: () => qc.invalidateQueries({ queryKey: ['runs', projectId] }),
  });
  const cancel = useMutation({
    mutationFn: (runId: string) =>
      unwrap(client.POST('/v1/projects/{project_id}/training-runs/{run_id}/cancel', { params: { path: { project_id: projectId, run_id: runId } } })),
    onSettled: () => qc.invalidateQueries({ queryKey: ['runs', projectId] }),
  });
  const logs = useQuery({
    queryKey: ['logs', logsOf],
    queryFn: () =>
      unwrap(client.GET('/v1/projects/{project_id}/training-runs/{run_id}/logs', { params: { path: { project_id: projectId, run_id: logsOf! } } })),
    enabled: !!logsOf,
    refetchInterval: POLL_MS,
  });

  return (
    <Section title="2. Training">
      <HStack>
        <Select
          value={versionId}
          onChange={setVersionId}
          placeholder={datasetId ? 'Dataset version' : 'Pick a dataset above'}
          options={(versions.data?.items ?? []).map((v) => ({ value: v.id, label: `v${v.number} (${short(v.id)})` }))}
        />
        <Select
          value={preset}
          onChange={setPreset}
          placeholder="Preset"
          options={(presets.data ?? []).map((p) => ({ value: p.name, label: `${p.name} (${p.gpu ? `${p.gpu} GPU` : `${p.cpu} CPU`})` }))}
        />
        <Button size="sm" colorPalette="orange" disabled={!versionId || !preset} loading={start.isPending} onClick={() => start.mutate()}>
          Start run
        </Button>
      </HStack>
      <ErrorAlert error={presets.error ?? start.error ?? cancel.error ?? runs.error} />
      <Table.Root size="sm">
        <Table.Header>
          <Table.Row>
            <Table.ColumnHeader>Run</Table.ColumnHeader>
            <Table.ColumnHeader>Preset</Table.ColumnHeader>
            <Table.ColumnHeader>Status</Table.ColumnHeader>
            <Table.ColumnHeader>Metrics</Table.ColumnHeader>
            <Table.ColumnHeader>Created</Table.ColumnHeader>
            <Table.ColumnHeader />
          </Table.Row>
        </Table.Header>
        <Table.Body>
          {runs.data?.map((r) => (
            <Table.Row key={r.id}>
              <Table.Cell>
                <Code>{short(r.id)}</Code>
              </Table.Cell>
              <Table.Cell>{r.preset}</Table.Cell>
              <Table.Cell>
                <StatusBadge status={r.status} />
              </Table.Cell>
              <Table.Cell>
                <Code fontSize="xs">{r.metrics ? JSON.stringify(r.metrics) : '-'}</Code>
              </Table.Cell>
              <Table.Cell>{when(r.created_at)}</Table.Cell>
              <Table.Cell>
                <HStack justify="end">
                  <Button size="xs" variant="outline" onClick={() => setLogsOf(logsOf === r.id ? null : r.id)}>
                    {logsOf === r.id ? 'Hide logs' : 'Logs'}
                  </Button>
                  {!RUN_DONE.includes(r.status) && (
                    <Button size="xs" variant="outline" onClick={() => cancel.mutate(r.id)}>
                      Cancel
                    </Button>
                  )}
                </HStack>
              </Table.Cell>
            </Table.Row>
          ))}
        </Table.Body>
      </Table.Root>
      {logsOf && (
        <>
          <ErrorAlert error={logs.error} />
          <Code display="block" whiteSpace="pre-wrap" maxH="96" overflow="auto" p="2" fontSize="xs">
            {logs.data?.logs || '(no logs yet)'}
          </Code>
        </>
      )}
    </Section>
  );
}
