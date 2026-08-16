import { createFileRoute, useNavigate, Link } from '@tanstack/react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Box, Button, Heading, HStack, Input, Table, Text, VStack, Spinner } from '@chakra-ui/react'
import { api } from '../lib/api'
import { getAuth } from '../lib/auth'

export const Route = createFileRoute('/')({
  component: TeamsPage,
})

function TeamsPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const authed = !!getAuth()

  const { data: teams, isLoading } = useQuery({ queryKey: ['teams'], queryFn: api.listTeams, enabled: authed })

  const createTeam = useMutation({
    mutationFn: (n: string) => api.createTeam(n),
    onSuccess: () => {
      setName('')
      queryClient.invalidateQueries({ queryKey: ['teams'] })
    },
  })

  if (!authed) {
    return (
      <VStack gap={4} align="start">
        <Heading size="lg">Welcome</Heading>
        <Text color="gray.600">Sign in to see your teams.</Text>
        <Button onClick={() => navigate({ to: '/login' })}>Sign in</Button>
      </VStack>
    )
  }

  return (
    <VStack gap={6} align="stretch">
      <HStack justify="space-between">
        <Heading size="lg">Teams</Heading>
      </HStack>

      <HStack>
        <Input placeholder="New team name" value={name} onChange={(e) => setName(e.target.value)} maxW="320px" bg="white" />
        <Button colorPalette="blue" loading={createTeam.isPending} onClick={() => name && createTeam.mutate(name)}>
          Create team
        </Button>
      </HStack>

      {isLoading ? (
        <Spinner />
      ) : (
        <Box bg="white" borderRadius="md" borderWidth={1}>
          <Table.Root>
            <Table.Header>
              <Table.Row>
                <Table.ColumnHeader>Name</Table.ColumnHeader>
                <Table.ColumnHeader>Created</Table.ColumnHeader>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {teams?.map((t) => (
                <Table.Row key={t.id} _hover={{ bg: 'gray.50', cursor: 'pointer' }}>
                  <Table.Cell>
                    <Link to="/t/$teamId" params={{ teamId: t.id }}>{t.name}</Link>
                  </Table.Cell>
                  <Table.Cell>{new Date(t.created_at).toLocaleString()}</Table.Cell>
                </Table.Row>
              ))}
            </Table.Body>
          </Table.Root>
          {teams?.length === 0 && (
            <Text p={6} color="gray.500">No teams yet — create one above.</Text>
          )}
        </Box>
      )}
    </VStack>
  )
}
