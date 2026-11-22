import { useState } from 'react';
import { Badge, Button, Card, Dialog, HStack, Portal, Stack, Text } from '@chakra-ui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, newKey, unwrap } from '@/api/client';
import { lineageQuery } from '@/api/queries';
import { DEFAULT_SEED, POLL_MS, RUN_DONE, RunStatus } from '@/constants';
import { CardGrid, Empty, ErrorAlert, Facts, Id, Section, Select, short, StatusBadge, when } from '@/ui';
import { versionsQuery } from './Datasets';

export const runsQuery = (projectId: string) => ({
  queryKey: ['runs', projectId],
  queryFn: () => unwrap(client.GET('/v1/projects/{project_id}/training-runs', { params: { path: { project_id: projectId } } })),
});

/** The deployment a succeeded run ended up in, from the lineage join. */
function RunDeployment({ projectId, runId }: { projectId: string; runId: string }) {
  const { data } = useQuery(lineageQuery(projectId, runId));
  if (!data?.deployment) return <>-</>;
  return (
    <HStack>
      <Id value={data.deployment.id} />
      <StatusBadge status={data.deployment.status} />
    </HStack>
  );
}

export function Training({ projectId, datasetId }: { projectId: string; datasetId: string }) {
  const qc = useQueryClient();
  const presets = useQuery({
    queryKey: ['presets', projectId],
    queryFn: () => unwrap(client.GET('/v1/projects/{project_id}/training-presets', { params: { path: { project_id: projectId } } })),
  });
  const versions = useQuery(versionsQuery(projectId, datasetId));
  const runs = useQuery({
    ...runsQuery(projectId),
    refetchInterval: (q) => (q.state.data?.items.some((r) => !RUN_DONE.includes(r.status)) ? POLL_MS : false),
  });
  const [versionId, setVersionId] = useState('');
  const [preset, setPreset] = useState('');
  const [stepsOf, setStepsOf] = useState<string | null>(null);

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
  const steps = useQuery({
    queryKey: ['steps', stepsOf],
    queryFn: () =>
      unwrap(client.GET('/v1/projects/{project_id}/training-runs/{run_id}/steps', { params: { path: { project_id: projectId, run_id: stepsOf! } } })),
    enabled: !!stepsOf,
    refetchInterval: POLL_MS,
  });

  const run = runs.data?.items.find((r) => r.id === stepsOf);
  return (
    <Stack gap="4">
      <Section title="Start a run" description="Train a preset on one dataset version.">
        <HStack wrap="wrap">
          <Select
            value={versionId}
            onChange={setVersionId}
            placeholder={datasetId ? 'Dataset version' : 'Pick a dataset on the Dataset tab'}
            options={(versions.data?.items ?? []).map((v) => ({ value: v.id, label: `v${v.number} (${short(v.id)})` }))}
          />
          <Select
            value={preset}
            onChange={setPreset}
            placeholder="Preset"
            options={(presets.data ?? []).map((p) => ({ value: p.name, label: `${p.name} (${p.gpu ? `${p.gpu} GPU` : `${p.cpu} CPU`})` }))}
          />
          <Button colorPalette="orange" disabled={!versionId || !preset} loading={start.isPending} onClick={() => start.mutate()}>
            Start run
          </Button>
        </HStack>
        <ErrorAlert error={presets.error ?? start.error ?? cancel.error ?? runs.error} />
      </Section>

      <Section title="Runs">
        {runs.data?.items.length === 0 && <Empty title="No runs yet" />}
        <CardGrid min="22rem">
          {runs.data?.items.map((r) => (
            <Card.Root key={r.id} size="sm" variant="subtle">
              <Card.Header>
                <HStack justify="space-between">
                  <HStack>
                    <Id value={r.id} />
                    {r.preset && <Badge variant="surface">{r.preset}</Badge>}
                  </HStack>
                  <StatusBadge status={r.status} />
                </HStack>
              </Card.Header>
              <Card.Body gap="2">
                <Metrics metrics={r.metrics} />
                <Facts
                  items={[
                    ['Deployment', r.status === RunStatus.SUCCEEDED ? <RunDeployment key="d" projectId={projectId} runId={r.id} /> : '-'],
                    ['Created', when(r.created_at)],
                  ]}
                />
              </Card.Body>
              <Card.Footer justifyContent="flex-end">
                <Button size="xs" variant="outline" onClick={() => setStepsOf(r.id)}>
                  Steps
                </Button>
                {!RUN_DONE.includes(r.status) && (
                  <Button size="xs" variant="outline" colorPalette="red" loading={cancel.isPending && cancel.variables === r.id} onClick={() => cancel.mutate(r.id)}>
                    Cancel
                  </Button>
                )}
              </Card.Footer>
            </Card.Root>
          ))}
        </CardGrid>
      </Section>

      {stepsOf && (
        <Dialog.Root open size="lg" scrollBehavior="inside" onOpenChange={(e) => !e.open && setStepsOf(null)}>
          <Portal>
            <Dialog.Backdrop />
            <Dialog.Positioner>
              <Dialog.Content>
                <Dialog.Header>
                  <Dialog.Title>Steps of run {short(stepsOf)}</Dialog.Title>
                  {run && <StatusBadge status={run.status} />}
                </Dialog.Header>
                <Dialog.Body>
                  <Stack gap="2">
                    <ErrorAlert error={steps.error} />
                    {steps.data?.steps.length === 0 && <Empty title="No steps yet" />}
                    {steps.data?.steps.map((s) => (
                      <HStack key={s.key} justify="space-between" align="start" p="2" rounded="md" bg="bg.subtle">
                        <Stack gap="0">
                          <Text fontWeight="medium" fontSize="sm">
                            {s.key}
                          </Text>
                          {s.epochs ? (
                            <Text fontSize="xs" color="fg.muted">
                              epoch {s.epoch ?? 0}/{s.epochs}
                            </Text>
                          ) : null}
                          {s.failure && (
                            <Text fontSize="xs" color="fg.error">
                              {s.failure}
                            </Text>
                          )}
                        </Stack>
                        <StatusBadge status={s.state} />
                      </HStack>
                    ))}
                  </Stack>
                </Dialog.Body>
                <Dialog.Footer>
                  <Button variant="outline" onClick={() => setStepsOf(null)}>
                    Close
                  </Button>
                </Dialog.Footer>
              </Dialog.Content>
            </Dialog.Positioner>
          </Portal>
        </Dialog.Root>
      )}
    </Stack>
  );
}

/** A run's metrics as labelled chips instead of raw JSON. */
function Metrics({ metrics }: { metrics: unknown }) {
  const entries = metrics && typeof metrics === 'object' ? Object.entries(metrics as Record<string, unknown>) : [];
  if (entries.length === 0) return <Text fontSize="sm" color="fg.muted">No metrics yet</Text>;
  return (
    <HStack wrap="wrap" gap="1">
      {entries.map(([k, v]) => (
        <Badge key={k} variant="outline">
          {k} {typeof v === 'number' ? Number(v.toPrecision(4)) : String(v)}
        </Badge>
      ))}
    </HStack>
  );
}
