import { createFileRoute } from '@tanstack/react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Fragment, useEffect, useState } from 'react'
import {
  VStack, HStack, Button, Input, Table, Badge, Text, Code, NativeSelect, Accordion, Dialog, Portal, Box,
} from '@chakra-ui/react'
import { api, uuid } from '../lib/api'
import { ProjectNav } from '../components/ProjectNav'
import { RunMetricsDetail, EpochChart } from '../components/RunCharts'

const INFLIGHT_STATUSES = ['submitted', 'running']

function LiveRunPanel({ projectId, runId }: { projectId: string; runId: string }) {
  // FR: live metrics + log tail while a run is still in flight, not just
  // the final result — Ray's job-logs endpoint returns whatever a
  // still-running job has printed so far.
  const { data } = useQuery({
    queryKey: ['training-run-logs', projectId, runId],
    queryFn: () => api.getTrainingRunLogs(projectId, runId),
    refetchInterval: (q) => (INFLIGHT_STATUSES.includes(q.state.data?.status ?? 'running') ? 3000 : false),
  })
  if (!data) return <Text fontSize="xs" color="gray.500">Loading…</Text>

  const scalarMetrics = Object.entries(data.metrics)
  const epochHistory = data.extra_metrics.epoch_history

  return (
    <VStack align="stretch" gap={3}>
      {(scalarMetrics.length > 0 || epochHistory?.length) && (
        <HStack align="start" gap={6} wrap="wrap">
          {scalarMetrics.length > 0 && (
            <HStack gap={1} wrap="wrap">
              {scalarMetrics.map(([k, v]) => <Badge key={k} size="sm" variant="outline">{k}: {v.toFixed(3)}</Badge>)}
            </HStack>
          )}
          {epochHistory && epochHistory.length > 0 && (
            <Box>
              <Text fontSize="xs" fontWeight="medium" mb={1}>Live loss / accuracy</Text>
              <EpochChart history={epochHistory} />
            </Box>
          )}
        </HStack>
      )}
      <Box>
        <Text fontSize="xs" fontWeight="medium" mb={1}>Log tail</Text>
        <Box as="pre" fontSize="2xs" bg="gray.900" color="gray.100" p={3} borderRadius="md" maxH="240px" overflowY="auto" whiteSpace="pre-wrap">
          {data.logs || '(no output yet)'}
        </Box>
      </Box>
    </VStack>
  )
}

const BACKENDS = [
  { value: '', label: 'Simple classifier (numpy logistic regression, tabular)' },
  { value: 'ultralytics', label: 'Ultralytics YOLO (image classifier, trains on uploaded files)' },
  { value: 'ludwig', label: 'Ludwig (tabular AutoML)' },
]

export const Route = createFileRoute('/p/$projectId/training')({
  component: TrainingPage,
})

function TrainingPage() {
  const { projectId } = Route.useParams()
  const [open, setOpen] = useState(false)
  const [detailRunId, setDetailRunId] = useState<string | null>(null)
  const { data: runs } = useQuery({
    queryKey: ['training-runs', projectId],
    queryFn: () => api.listTrainingRuns(projectId),
    refetchInterval: 4000,
  })

  return (
    <VStack align="stretch" gap={6}>
      <ProjectNav projectId={projectId} />

      <HStack justify="space-between">
        <Text fontSize="sm" color="gray.600">
          {runs?.length ?? 0} training run(s).
        </Text>
        <Dialog.Root open={open} onOpenChange={(e) => setOpen(e.open)} size="lg">
          <Dialog.Trigger asChild>
            <Button colorPalette="blue" size="sm">New training run</Button>
          </Dialog.Trigger>
          <Portal>
            <Dialog.Backdrop />
            <Dialog.Positioner>
              <Dialog.Content>
                <Dialog.Header><Dialog.Title>New training run</Dialog.Title></Dialog.Header>
                <Dialog.Body pb={6}>
                  <NewRunForm projectId={projectId} onSubmitted={() => setOpen(false)} />
                </Dialog.Body>
                <Dialog.CloseTrigger />
              </Dialog.Content>
            </Dialog.Positioner>
          </Portal>
        </Dialog.Root>
      </HStack>

      <Table.Root size="sm" bg="white">
        <Table.Header>
          <Table.Row>
            <Table.ColumnHeader>Status</Table.ColumnHeader>
            <Table.ColumnHeader>Backend</Table.ColumnHeader>
            <Table.ColumnHeader>Dataset ref</Table.ColumnHeader>
            <Table.ColumnHeader>Metrics</Table.ColumnHeader>
            <Table.ColumnHeader>MLflow run</Table.ColumnHeader>
            <Table.ColumnHeader>Created</Table.ColumnHeader>
          </Table.Row>
        </Table.Header>
        <Table.Body>
          {runs?.map((r) => {
            const scalarMetrics = Object.entries(r.metrics ?? {}).filter(([, v]) => typeof v === 'number') as [string, number][]
            const hasDetail = r.metrics?.epoch_history?.length || r.metrics?.confusion_matrix
            const inflight = INFLIGHT_STATUSES.includes(r.status)
            return (
              <Fragment key={r.id}>
                <Table.Row>
                  <Table.Cell>
                    <Badge colorPalette={r.status === 'succeeded' ? 'green' : r.status === 'failed' ? 'red' : 'yellow'}>
                      {r.status}
                    </Badge>
                  </Table.Cell>
                  <Table.Cell fontSize="xs">{(r.config?.backend as string) ?? 'numpy'}{r.config?.task ? ` / ${r.config.task}` : ''}</Table.Cell>
                  <Table.Cell>{r.dataset_ref}</Table.Cell>
                  <Table.Cell>
                    <HStack gap={1} wrap="wrap">
                      {scalarMetrics.length > 0 ? (
                        scalarMetrics.map(([k, v]) => <Badge key={k} size="sm" variant="outline">{k}: {v.toFixed(3)}</Badge>)
                      ) : (
                        <Text fontSize="xs" color="gray.400">—</Text>
                      )}
                      {(hasDetail || inflight) && (
                        <Button size="2xs" variant="ghost" onClick={() => setDetailRunId(detailRunId === r.id ? null : r.id)}>
                          {detailRunId === r.id ? 'hide' : inflight ? 'view live' : 'view chart'}
                        </Button>
                      )}
                    </HStack>
                  </Table.Cell>
                  <Table.Cell><Code fontSize="xs">{r.mlflow_run_id ?? '—'}</Code></Table.Cell>
                  <Table.Cell>{new Date(r.created_at).toLocaleTimeString()}</Table.Cell>
                </Table.Row>
                {detailRunId === r.id && (
                  <Table.Row>
                    <Table.Cell colSpan={6} bg="gray.50">
                      {inflight ? <LiveRunPanel projectId={projectId} runId={r.id} /> : <RunMetricsDetail metrics={r.metrics!} />}
                    </Table.Cell>
                  </Table.Row>
                )}
              </Fragment>
            )
          })}
        </Table.Body>
      </Table.Root>
      {runs?.length === 0 && <Text color="gray.500">No training runs yet.</Text>}
    </VStack>
  )
}

function NewRunForm({ projectId, onSubmitted }: { projectId: string; onSubmitted: () => void }) {
  const queryClient = useQueryClient()
  const { data: project } = useQuery({ queryKey: ['project', projectId], queryFn: () => api.getProject(projectId) })
  const { data: datasets } = useQuery({ queryKey: ['datasets', projectId], queryFn: () => api.listDatasets(projectId) })
  const [datasetId, setDatasetId] = useState('')
  const [datasetVersion, setDatasetVersion] = useState('')
  const [testDatasetId, setTestDatasetId] = useState('')
  const [testDatasetVersion, setTestDatasetVersion] = useState('')
  const [backend, setBackend] = useState('')
  const [task, setTask] = useState('classify')
  const [epochs, setEpochs] = useState('25')
  const [testSplit, setTestSplit] = useState('0.2')
  const [hfDataset, setHfDataset] = useState('')
  const [hfModel, setHfModel] = useState('')

  // Step 2 defaults from project settings (06 FR3), once loaded.
  useEffect(() => {
    if (project?.settings?.base_model) setHfModel((prev) => prev || project.settings!.base_model!)
  }, [project])

  const { data: versions } = useQuery({
    queryKey: ['dataset-versions', projectId, datasetId],
    queryFn: () => api.listDatasetVersions(projectId, datasetId),
    enabled: !!datasetId,
  })
  const { data: testVersions } = useQuery({
    queryKey: ['dataset-versions', projectId, testDatasetId],
    queryFn: () => api.listDatasetVersions(projectId, testDatasetId),
    enabled: !!testDatasetId,
  })

  const submit = useMutation({
    mutationFn: () => {
      const dataset = datasets?.find((d) => d.id === datasetId)
      const config: Record<string, unknown> = {
        trainer: { epochs: Number(epochs) || 25 },
        test_split: Number(testSplit) || 0.2,
      }
      if (backend) config.backend = backend
      if (backend === 'ultralytics') config.task = task
      if (backend === 'ludwig' && hfDataset) {
        config.hf_dataset = hfDataset
        if (hfModel) config.hf_model = hfModel
      }
      return api.createTrainingRun(
        projectId,
        dataset?.name ?? 'v1',
        config,
        uuid(),
        datasetId || undefined,
        datasetVersion || undefined,
        testDatasetId || undefined,
        testDatasetVersion || undefined,
      )
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['training-runs', projectId] })
      onSubmitted()
    },
  })

  return (
    <VStack align="stretch" gap={4}>
      <Accordion.Root multiple defaultValue={['data', 'model']}>
        <Accordion.Item value="data">
          <Accordion.ItemTrigger>
            <Text fontWeight="medium">1. What data</Text>
            <Accordion.ItemIndicator />
          </Accordion.ItemTrigger>
          <Accordion.ItemContent>
            <VStack align="stretch" gap={3} py={2}>
              <NativeSelect.Root size="sm">
                <NativeSelect.Field value={datasetId} onChange={(e) => { setDatasetId(e.target.value); setDatasetVersion('') }}>
                  <option value="">No dataset (synthetic data)</option>
                  {datasets?.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                </NativeSelect.Field>
                <NativeSelect.Indicator />
              </NativeSelect.Root>
              {datasetId && versions && versions.versions.length > 1 && (
                <NativeSelect.Root size="sm">
                  <NativeSelect.Field value={datasetVersion} onChange={(e) => setDatasetVersion(e.target.value)}>
                    <option value="">Latest {versions.has_uncommitted_changes ? '(uncommitted changes)' : ''}</option>
                    {versions.versions.filter((v) => v.message !== 'Repository created').map((v) => (
                      <option key={v.id} value={v.id}>{v.id.slice(0, 8)} — {v.message}</option>
                    ))}
                  </NativeSelect.Field>
                  <NativeSelect.Indicator />
                </NativeSelect.Root>
              )}
              <Text fontSize="xs" color="gray.600">Held-out test set (optional):</Text>
              <NativeSelect.Root size="sm">
                <NativeSelect.Field value={testDatasetId} onChange={(e) => { setTestDatasetId(e.target.value); setTestDatasetVersion('') }}>
                  <option value="">None (random {testSplit || '0.2'} split of training data)</option>
                  {datasets?.filter((d) => d.id !== datasetId).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                </NativeSelect.Field>
                <NativeSelect.Indicator />
              </NativeSelect.Root>
              {testDatasetId && testVersions && testVersions.versions.length > 1 && (
                <NativeSelect.Root size="sm">
                  <NativeSelect.Field value={testDatasetVersion} onChange={(e) => setTestDatasetVersion(e.target.value)}>
                    <option value="">Latest {testVersions.has_uncommitted_changes ? '(uncommitted changes)' : ''}</option>
                    {testVersions.versions.filter((v) => v.message !== 'Repository created').map((v) => (
                      <option key={v.id} value={v.id}>{v.id.slice(0, 8)} — {v.message}</option>
                    ))}
                  </NativeSelect.Field>
                  <NativeSelect.Indicator />
                </NativeSelect.Root>
              )}
            </VStack>
          </Accordion.ItemContent>
        </Accordion.Item>

        <Accordion.Item value="model">
          <Accordion.ItemTrigger>
            <Text fontWeight="medium">2. What model</Text>
            <Accordion.ItemIndicator />
          </Accordion.ItemTrigger>
          <Accordion.ItemContent>
            <VStack align="stretch" gap={3} py={2}>
              <NativeSelect.Root size="sm">
                <NativeSelect.Field value={backend} onChange={(e) => setBackend(e.target.value)}>
                  {BACKENDS.map((b) => <option key={b.value} value={b.value}>{b.label}</option>)}
                </NativeSelect.Field>
                <NativeSelect.Indicator />
              </NativeSelect.Root>
              {backend === 'ultralytics' && (
                <NativeSelect.Root size="sm">
                  <NativeSelect.Field value={task} onChange={(e) => setTask(e.target.value)}>
                    <option value="classify">Image classification</option>
                    <option value="detect">Object detection</option>
                  </NativeSelect.Field>
                  <NativeSelect.Indicator />
                </NativeSelect.Root>
              )}
              {backend === 'ludwig' && (
                <Input
                  placeholder="HF model (default from project settings' base_model, or a tiny test model)"
                  value={hfModel}
                  onChange={(e) => setHfModel(e.target.value)}
                  size="sm"
                />
              )}
              {project?.settings?.compute_target && (
                <Text fontSize="xs" color="gray.500">Compute target (from project settings): {project.settings.compute_target}</Text>
              )}
            </VStack>
          </Accordion.ItemContent>
        </Accordion.Item>

        <Accordion.Item value="advanced">
          <Accordion.ItemTrigger>
            <Text fontWeight="medium">3. How long (advanced)</Text>
            <Accordion.ItemIndicator />
          </Accordion.ItemTrigger>
          <Accordion.ItemContent>
            <VStack align="stretch" gap={3} py={2}>
              <HStack>
                <Text fontSize="sm" color="gray.600" minW="80px">Epochs</Text>
                <Input value={epochs} onChange={(e) => setEpochs(e.target.value)} maxW="100px" size="sm" />
              </HStack>
              <HStack>
                <Text fontSize="sm" color="gray.600" minW="80px">Test split</Text>
                <Input value={testSplit} onChange={(e) => setTestSplit(e.target.value)} maxW="100px" size="sm" disabled={!!testDatasetId} />
              </HStack>
              {backend === 'ludwig' && (
                <Input placeholder="HF dataset (e.g. SetFit/sst2) — optional" value={hfDataset} onChange={(e) => setHfDataset(e.target.value)} size="sm" />
              )}
            </VStack>
          </Accordion.ItemContent>
        </Accordion.Item>
      </Accordion.Root>

      <Button colorPalette="blue" onClick={() => submit.mutate()} loading={submit.isPending}>
        Submit training run
      </Button>
      {submit.isError && <Text fontSize="sm" color="red.600">{String(submit.error)}</Text>}
    </VStack>
  )
}
