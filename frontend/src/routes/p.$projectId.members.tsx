import { createFileRoute } from '@tanstack/react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { VStack, HStack, Button, Input, NativeSelect, Table, Text, Box, Heading, Code } from '@chakra-ui/react'
import { api } from '../lib/api'
import { ProjectNav } from '../components/ProjectNav'

export const Route = createFileRoute('/p/$projectId/members')({
  component: MembersPage,
})

function MembersPage() {
  const { projectId } = Route.useParams()
  const queryClient = useQueryClient()
  const [email, setEmail] = useState('')
  const [role, setRole] = useState('project_viewer')
  const { data: members } = useQuery({ queryKey: ['members', projectId], queryFn: () => api.listMembers(projectId) })
  const { data: auditLog } = useQuery({ queryKey: ['audit-log', projectId], queryFn: () => api.getAuditLog(projectId) })

  const invite = useMutation({
    mutationFn: () => api.createInvitation(projectId, email, role),
    onSuccess: () => setEmail(''),
  })
  const removeMember = useMutation({
    mutationFn: (userId: string) => api.removeMember(projectId, userId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['members', projectId] }),
  })
  const setRoleMutation = useMutation({
    mutationFn: ({ userId, r }: { userId: string; r: string }) => api.setMemberRole(projectId, userId, r),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['members', projectId] }),
  })

  return (
    <VStack align="stretch" gap={6}>
      <ProjectNav projectId={projectId} />

      <Box bg="white" borderWidth={1} borderRadius="md" p={5}>
        <Heading size="sm" mb={3}>Invite a member</Heading>
        <HStack wrap="wrap">
          <Input placeholder="email" value={email} onChange={(e) => setEmail(e.target.value)} maxW="260px" size="sm" />
          <NativeSelect.Root size="sm" maxW="200px">
            <NativeSelect.Field value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="project_viewer">Viewer</option>
              <option value="project_editor">Editor</option>
            </NativeSelect.Field>
            <NativeSelect.Indicator />
          </NativeSelect.Root>
          <Button size="sm" onClick={() => email && invite.mutate()} loading={invite.isPending}>Invite</Button>
        </HStack>
        {invite.data && (
          <Text fontSize="xs" color="gray.600" mt={2}>
            Invitation created — share this token (no email delivery wired up yet): <Code>{invite.data.token}</Code>
          </Text>
        )}
      </Box>

      <Table.Root size="sm" bg="white">
        <Table.Header>
          <Table.Row>
            <Table.ColumnHeader>User</Table.ColumnHeader>
            <Table.ColumnHeader>Role</Table.ColumnHeader>
            <Table.ColumnHeader></Table.ColumnHeader>
          </Table.Row>
        </Table.Header>
        <Table.Body>
          {members?.map((m) => (
            <Table.Row key={m.user_id}>
              <Table.Cell fontSize="sm">{m.user_id}</Table.Cell>
              <Table.Cell>
                <NativeSelect.Root size="xs" maxW="160px">
                  <NativeSelect.Field value={m.role} onChange={(e) => setRoleMutation.mutate({ userId: m.user_id, r: e.target.value })}>
                    <option value="project_viewer">Viewer</option>
                    <option value="project_editor">Editor</option>
                  </NativeSelect.Field>
                  <NativeSelect.Indicator />
                </NativeSelect.Root>
              </Table.Cell>
              <Table.Cell>
                <Button size="2xs" variant="outline" colorPalette="red" onClick={() => removeMember.mutate(m.user_id)}>remove</Button>
              </Table.Cell>
            </Table.Row>
          ))}
        </Table.Body>
      </Table.Root>
      {members?.length === 0 && <Text color="gray.500">No members yet.</Text>}

      <Box>
        <Heading size="sm" mb={2}>Audit log</Heading>
        <VStack align="stretch" gap={1}>
          {auditLog?.map((a) => (
            <Text key={a.id} fontSize="xs" color="gray.600">
              {new Date(a.created_at).toLocaleString()} — {a.actor} {a.action} {a.entity_type}
            </Text>
          ))}
          {auditLog?.length === 0 && <Text fontSize="sm" color="gray.500">No audit events yet.</Text>}
        </VStack>
      </Box>
    </VStack>
  )
}
