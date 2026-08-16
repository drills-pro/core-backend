import { createFileRoute } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { VStack, Table, Box, Text } from '@chakra-ui/react'
import { api } from '../lib/api'
import { ProjectNav } from '../components/ProjectNav'

export const Route = createFileRoute('/p/$projectId/usage')({
  component: ProjectUsagePage,
})

function ProjectUsagePage() {
  const { projectId } = Route.useParams()
  const { data: usage } = useQuery({ queryKey: ['project-usage', projectId], queryFn: () => api.projectUsage(projectId) })

  return (
    <VStack align="stretch" gap={6}>
      <ProjectNav projectId={projectId} />
      <Box bg="white" borderWidth={1} borderRadius="md">
        <Table.Root size="sm">
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader>Resource</Table.ColumnHeader>
              <Table.ColumnHeader>Unit</Table.ColumnHeader>
              <Table.ColumnHeader>Quantity</Table.ColumnHeader>
              <Table.ColumnHeader>Occurred</Table.ColumnHeader>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {usage?.map((u) => (
              <Table.Row key={u.id}>
                <Table.Cell>{u.resource_type}</Table.Cell>
                <Table.Cell>{u.unit}</Table.Cell>
                <Table.Cell>{u.quantity}</Table.Cell>
                <Table.Cell>{new Date(u.occurred_at).toLocaleString()}</Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </Table.Root>
        {usage?.length === 0 && <Text p={6} color="gray.500">No usage recorded yet.</Text>}
      </Box>
    </VStack>
  )
}
