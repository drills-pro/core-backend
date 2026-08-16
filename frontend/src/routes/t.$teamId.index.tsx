import { createFileRoute, Link } from '@tanstack/react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Box, Button, Heading, HStack, Input, NativeSelect, Table, Badge, Text, VStack, SimpleGrid } from '@chakra-ui/react'
import { api, TASK_TYPES } from '../lib/api'

export const Route = createFileRoute('/t/$teamId/')({
  component: TeamDashboard,
})

function TeamDashboard() {
  const { teamId } = Route.useParams()
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [taskType, setTaskType] = useState<string>('legacy_untyped')

  const { data: team } = useQuery({ queryKey: ['team', teamId], queryFn: () => api.getTeam(teamId) })
  const { data: projects } = useQuery({ queryKey: ['projects'], queryFn: api.listProjects })
  const { data: usage } = useQuery({ queryKey: ['team-usage', teamId], queryFn: () => api.teamUsage(teamId) })

  const createProject = useMutation({
    mutationFn: () => api.createProject(name, taskType, teamId),
    onSuccess: () => {
      setName('')
      queryClient.invalidateQueries({ queryKey: ['projects'] })
    },
  })

  const teamProjects = projects?.filter((p) => p.team_id === teamId) ?? []

  return (
    <VStack align="stretch" gap={6}>
      <Heading size="lg">{team?.name ?? 'Team'}</Heading>

      <HStack wrap="wrap">
        <Input placeholder="New project name" value={name} onChange={(e) => setName(e.target.value)} maxW="260px" bg="white" />
        <NativeSelect.Root size="sm" maxW="240px">
          <NativeSelect.Field value={taskType} onChange={(e) => setTaskType(e.target.value)}>
            {TASK_TYPES.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </NativeSelect.Field>
          <NativeSelect.Indicator />
        </NativeSelect.Root>
        <Button colorPalette="blue" loading={createProject.isPending} onClick={() => name && createProject.mutate()}>
          Create project
        </Button>
      </HStack>

      <Box bg="white" borderRadius="md" borderWidth={1}>
        <Table.Root>
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader>Name</Table.ColumnHeader>
              <Table.ColumnHeader>Task type</Table.ColumnHeader>
              <Table.ColumnHeader>Status</Table.ColumnHeader>
              <Table.ColumnHeader>Created</Table.ColumnHeader>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {teamProjects.map((p) => (
              <Table.Row key={p.id} _hover={{ bg: 'gray.50', cursor: 'pointer' }}>
                <Table.Cell><Link to="/p/$projectId" params={{ projectId: p.id }}>{p.name}</Link></Table.Cell>
                <Table.Cell fontSize="sm">{p.task_type}</Table.Cell>
                <Table.Cell><Badge colorPalette={p.status === 'active' ? 'green' : 'gray'}>{p.status}</Badge></Table.Cell>
                <Table.Cell>{new Date(p.created_at).toLocaleString()}</Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </Table.Root>
        {teamProjects.length === 0 && <Text p={6} color="gray.500">No projects in this team yet.</Text>}
      </Box>

      <Box>
        <Heading size="md" mb={3}>Team usage</Heading>
        <SimpleGrid columns={{ base: 1, md: 3 }} gap={3}>
          {usage?.map((u, i) => (
            <Box key={i} borderWidth={1} borderRadius="md" p={3} bg="white">
              <Text fontSize="xs" color="gray.500">{u.resource_type} ({u.unit})</Text>
              <Text fontSize="lg" fontWeight="bold">{u.total_quantity}</Text>
            </Box>
          ))}
          {usage?.length === 0 && <Text color="gray.500">No usage recorded yet.</Text>}
        </SimpleGrid>
      </Box>
    </VStack>
  )
}
