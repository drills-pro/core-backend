import { createFileRoute, Link } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { Box, Heading, Table, Text, VStack, Code } from '@chakra-ui/react'
import { api } from '../lib/api'

export const Route = createFileRoute('/t/$teamId/settings')({
  component: TeamSettings,
})

function TeamSettings() {
  const { teamId } = Route.useParams()
  const { data: team } = useQuery({ queryKey: ['team', teamId], queryFn: () => api.getTeam(teamId) })
  const { data: projects } = useQuery({ queryKey: ['projects'], queryFn: api.listProjects })
  const teamProjects = projects?.filter((p) => p.team_id === teamId) ?? []

  return (
    <VStack align="stretch" gap={6}>
      <Heading size="lg">{team?.name ?? 'Team'} settings</Heading>
      <Box>
        <Text fontSize="sm" color="gray.500">Team ID</Text>
        <Code fontSize="sm">{teamId}</Code>
      </Box>
      <Box>
        <Heading size="md" mb={2}>Members &amp; invites</Heading>
        <Text fontSize="sm" color="gray.600" mb={3}>
          Membership and invitations are managed per project — open a project's Members tab below.
        </Text>
        <Table.Root size="sm">
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader>Project</Table.ColumnHeader>
              <Table.ColumnHeader>Members</Table.ColumnHeader>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {teamProjects.map((p) => (
              <Table.Row key={p.id}>
                <Table.Cell>{p.name}</Table.Cell>
                <Table.Cell>
                  <Link to="/p/$projectId/members" params={{ projectId: p.id }}>manage members</Link>
                </Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </Table.Root>
      </Box>
    </VStack>
  )
}
