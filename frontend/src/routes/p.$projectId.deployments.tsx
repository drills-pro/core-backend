import { createFileRoute } from '@tanstack/react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { VStack, HStack, Button, Table, Badge, Text, Box, Code, Textarea } from '@chakra-ui/react'
import { api } from '../lib/api'
import { ProjectNav } from '../components/ProjectNav'

export const Route = createFileRoute('/p/$projectId/deployments')({
  component: DeploymentsPage,
})

function DeploymentsPage() {
  const { projectId } = Route.useParams()
  const queryClient = useQueryClient()
  const [testFor, setTestFor] = useState<string | null>(null)
  const { data: runs } = useQuery({ queryKey: ['training-runs', projectId], queryFn: () => api.listTrainingRuns(projectId) })
  const { data: deployments } = useQuery({
    queryKey: ['deployments', projectId],
    queryFn: () => api.listDeployments(projectId),
    refetchInterval: 4000,
  })
  const succeeded = runs?.filter((r) => r.status === 'succeeded') ?? []
  const deploy = useMutation({
    mutationFn: (runId: string) => api.createDeployment(projectId, runId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['deployments', projectId] }),
  })

  return (
    <VStack align="stretch" gap={6}>
      <ProjectNav projectId={projectId} />

      <Box>
        <Text fontWeight="medium" mb={2}>Deploy a succeeded run</Text>
        <HStack wrap="wrap">
          {succeeded.map((r) => (
            <Button key={r.id} size="xs" variant="outline" onClick={() => deploy.mutate(r.id)}>deploy {r.id.slice(0, 8)}</Button>
          ))}
          {succeeded.length === 0 && <Text color="gray.500" fontSize="sm">No succeeded runs yet.</Text>}
        </HStack>
      </Box>

      <Table.Root size="sm" bg="white">
        <Table.Header>
          <Table.Row>
            <Table.ColumnHeader>Status</Table.ColumnHeader>
            <Table.ColumnHeader>Route</Table.ColumnHeader>
            <Table.ColumnHeader>Test</Table.ColumnHeader>
          </Table.Row>
        </Table.Header>
        <Table.Body>
          {deployments?.map((d) => (
            <Table.Row key={d.id}>
              <Table.Cell>
                <Badge colorPalette={d.status === 'active' ? 'green' : d.status === 'failed' ? 'red' : 'yellow'}>{d.status}</Badge>
              </Table.Cell>
              <Table.Cell><Code fontSize="xs">{d.route_prefix}</Code></Table.Cell>
              <Table.Cell>
                <Button size="2xs" variant="ghost" disabled={d.status !== 'active'} onClick={() => setTestFor(testFor === d.id ? null : d.id)}>
                  {testFor === d.id ? 'hide' : 'test'}
                </Button>
              </Table.Cell>
            </Table.Row>
          ))}
        </Table.Body>
      </Table.Root>
      {deployments?.length === 0 && <Text color="gray.500">No deployments yet.</Text>}

      {testFor && <TestPanel projectId={projectId} deploymentId={testFor} />}
    </VStack>
  )
}

function TestPanel({ projectId, deploymentId }: { projectId: string; deploymentId: string }) {
  const [body, setBody] = useState('{}')
  const { data: calls, refetch } = useQuery({
    queryKey: ['test-calls', projectId, deploymentId],
    queryFn: () => api.listTestCalls(projectId, deploymentId),
  })
  const test = useMutation({
    mutationFn: () => api.testDeployment(projectId, deploymentId, JSON.parse(body)),
    onSuccess: () => refetch(),
  })

  return (
    <Box bg="white" borderWidth={1} borderRadius="md" p={4}>
      <Text fontWeight="medium" mb={2}>Test this deployment</Text>
      <VStack align="stretch" gap={2}>
        <Textarea value={body} onChange={(e) => setBody(e.target.value)} fontFamily="mono" fontSize="sm" rows={4} />
        <Button size="sm" alignSelf="start" onClick={() => test.mutate()} loading={test.isPending}>Send test request</Button>
        {test.isError && <Text fontSize="sm" color="red.600">{String(test.error)}</Text>}
        {test.data && (
          <Box borderWidth={1} borderRadius="md" p={2} bg="gray.50">
            <Text fontSize="xs" color="gray.500">{test.data.latency_ms.toFixed(1)}ms</Text>
            <Code fontSize="xs" whiteSpace="pre-wrap">{JSON.stringify(test.data.response, null, 2)}</Code>
          </Box>
        )}
        {calls && calls.length > 0 && (
          <VStack align="stretch" gap={1} mt={2}>
            <Text fontSize="xs" fontWeight="medium">Recent test calls</Text>
            {calls.map((c) => (
              <HStack key={c.id} fontSize="xs" color="gray.600">
                <Text>{new Date(c.created_at).toLocaleTimeString()}</Text>
                <Text>{c.latency_ms.toFixed(1)}ms</Text>
              </HStack>
            ))}
          </VStack>
        )}
      </VStack>
    </Box>
  )
}
