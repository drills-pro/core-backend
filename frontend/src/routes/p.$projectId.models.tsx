import { createFileRoute } from '@tanstack/react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useState, type ReactNode } from 'react'
import { VStack, HStack, Button, Table, Badge, Text, Box, Code, Heading } from '@chakra-ui/react'
import { api } from '../lib/api'
import { ProjectNav } from '../components/ProjectNav'
import { RunMetricsDetail } from '../components/RunCharts'

export const Route = createFileRoute('/p/$projectId/models')({
  component: ModelsPage,
})

function ModelsPage() {
  const { projectId } = Route.useParams()
  const queryClient = useQueryClient()
  const [lineageFor, setLineageFor] = useState<string | null>(null)
  const { data: runs } = useQuery({ queryKey: ['training-runs', projectId], queryFn: () => api.listTrainingRuns(projectId) })
  const { data: models } = useQuery({ queryKey: ['models', projectId], queryFn: () => api.listModels(projectId) })
  const { data: lineage } = useQuery({
    queryKey: ['lineage', projectId, lineageFor],
    queryFn: () => api.getTrainingRunLineage(projectId, lineageFor!),
    enabled: !!lineageFor,
  })
  const succeeded = runs?.filter((r) => r.status === 'succeeded') ?? []

  const register = useMutation({
    mutationFn: (runId: string) => api.registerModel(projectId, runId, `model-${runId.slice(0, 8)}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['models', projectId] }),
  })
  const promote = useMutation({
    mutationFn: ({ id, stage }: { id: string; stage: string }) => api.promoteModel(projectId, id, stage),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['models', projectId] }),
  })
  const publish = useMutation({
    mutationFn: ({ id, meta }: { id: string; meta: boolean }) => api.publishModel(projectId, id, { publish_metadata: meta }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['models', projectId] }),
  })

  return (
    <VStack align="stretch" gap={6}>
      <ProjectNav projectId={projectId} />

      <Box>
        <Text fontWeight="medium" mb={2}>Succeeded runs — register as a model / view lineage</Text>
        <HStack wrap="wrap">
          {succeeded.map((r) => (
            <HStack key={r.id} gap={1}>
              <Button size="xs" variant="outline" onClick={() => register.mutate(r.id)}>register {r.id.slice(0, 8)}</Button>
              <Button size="xs" variant="ghost" onClick={() => setLineageFor(lineageFor === r.id ? null : r.id)}>
                {lineageFor === r.id ? 'hide lineage' : 'lineage'}
              </Button>
            </HStack>
          ))}
          {succeeded.length === 0 && <Text color="gray.500" fontSize="sm">No succeeded runs yet.</Text>}
        </HStack>
      </Box>

      {lineageFor && lineage && (
        <Box bg="white" borderWidth={1} borderRadius="md" p={4}>
          <Heading size="sm" mb={3}>Lineage strip</Heading>
          <HStack gap={4} wrap="wrap" align="stretch">
            <LineageCard title="Dataset commit">
              <Text fontSize="xs">{lineage.dataset_commit_message ?? '—'}</Text>
            </LineageCard>
            <LineageCard title="Training run">
              <Badge colorPalette="green">{lineage.training_run.status}</Badge>
              {lineage.mlflow_run_url && (
                <Text fontSize="xs"><a href={lineage.mlflow_run_url} target="_blank" rel="noreferrer">MLflow run ↗</a></Text>
              )}
            </LineageCard>
            <LineageCard title="Model version">
              {lineage.model_version ? (
                <>
                  <Text fontSize="xs">{lineage.model_version.mlflow_model_name} v{lineage.model_version.mlflow_version}</Text>
                  <Badge>{lineage.model_version.stage}</Badge>
                </>
              ) : (
                <Text fontSize="xs" color="gray.400">not registered</Text>
              )}
            </LineageCard>
            <LineageCard title="Deployment">
              {lineage.deployment ? (
                <>
                  <Badge colorPalette={lineage.deployment.status === 'active' ? 'green' : 'gray'}>{lineage.deployment.status}</Badge>
                  <Code fontSize="2xs">{lineage.deployment.route_prefix}</Code>
                </>
              ) : (
                <Text fontSize="xs" color="gray.400">not deployed</Text>
              )}
            </LineageCard>
          </HStack>
          {lineage.training_run.metrics && (
            <Box mt={4} pt={4} borderTopWidth={1}>
              <RunMetricsDetail metrics={lineage.training_run.metrics} />
            </Box>
          )}
        </Box>
      )}

      <Table.Root size="sm" bg="white">
        <Table.Header>
          <Table.Row>
            <Table.ColumnHeader>Model</Table.ColumnHeader>
            <Table.ColumnHeader>Version</Table.ColumnHeader>
            <Table.ColumnHeader>Stage</Table.ColumnHeader>
            <Table.ColumnHeader>Promote</Table.ColumnHeader>
            <Table.ColumnHeader>Publish</Table.ColumnHeader>
          </Table.Row>
        </Table.Header>
        <Table.Body>
          {models?.map((m) => (
            <Table.Row key={m.id}>
              <Table.Cell>{m.mlflow_model_name}</Table.Cell>
              <Table.Cell>{m.mlflow_version}</Table.Cell>
              <Table.Cell><Badge>{m.customer_label}</Badge></Table.Cell>
              <Table.Cell>
                <HStack>
                  <Button size="2xs" onClick={() => promote.mutate({ id: m.id, stage: 'Staging' })}>Staging</Button>
                  <Button size="2xs" onClick={() => promote.mutate({ id: m.id, stage: 'Production' })}>Production</Button>
                </HStack>
              </Table.Cell>
              <Table.Cell>
                <Button size="2xs" variant="outline" onClick={() => publish.mutate({ id: m.id, meta: true })}>publish metadata</Button>
              </Table.Cell>
            </Table.Row>
          ))}
        </Table.Body>
      </Table.Root>
    </VStack>
  )
}

function LineageCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <VStack align="start" gap={1} borderWidth={1} borderRadius="md" p={3} minW="160px">
      <Text fontSize="2xs" color="gray.500" textTransform="uppercase">{title}</Text>
      {children}
    </VStack>
  )
}
