import { createFileRoute } from '@tanstack/react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { VStack, HStack, Button, Input, Text, Box, Heading } from '@chakra-ui/react'
import { api } from '../lib/api'

export const Route = createFileRoute('/settings/profile')({
  component: ProfilePage,
})

function ProfilePage() {
  const queryClient = useQueryClient()
  const { data: profile } = useQuery({ queryKey: ['my-profile'], queryFn: api.getMyProfile })
  const { data: idps } = useQuery({ queryKey: ['linked-idps'], queryFn: api.listLinkedIdps, retry: false })
  const [username, setUsername] = useState('')
  const [displayName, setDisplayName] = useState('')

  useEffect(() => {
    if (profile) {
      setUsername(profile.username ?? '')
      setDisplayName(profile.display_name ?? '')
    }
  }, [profile])

  const save = useMutation({
    mutationFn: () => api.updateMyProfile({ username: username || undefined, display_name: displayName || undefined }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['my-profile'] }),
  })
  const link = useMutation({
    mutationFn: (provider: string) => api.getIdpIntent(provider, window.location.href),
    onSuccess: (data) => {
      if (data.authUrl) window.location.href = data.authUrl as string
    },
  })
  const unlink = useMutation({
    mutationFn: (linkId: string) => api.unlinkIdp(linkId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['linked-idps'] }),
  })

  return (
    <VStack align="stretch" gap={6}>
      <Heading size="lg">Profile</Heading>

      <Box bg="white" borderWidth={1} borderRadius="md" p={5}>
        <Heading size="sm" mb={3}>Display info</Heading>
        <VStack align="stretch" gap={3} maxW="360px">
          <Box>
            <Text fontSize="xs" color="gray.500" mb={1}>Username</Text>
            <Input size="sm" value={username} onChange={(e) => setUsername(e.target.value)} />
          </Box>
          <Box>
            <Text fontSize="xs" color="gray.500" mb={1}>Display name</Text>
            <Input size="sm" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
          </Box>
          <Button size="sm" alignSelf="start" onClick={() => save.mutate()} loading={save.isPending}>Save</Button>
          {save.isError && <Text fontSize="sm" color="red.600">{String(save.error)}</Text>}
        </VStack>
      </Box>

      <Box bg="white" borderWidth={1} borderRadius="md" p={5}>
        <Heading size="sm" mb={3}>Linked accounts</Heading>
        <VStack align="stretch" gap={2}>
          {Array.isArray(idps) && idps.length > 0 ? (
            idps.map((idp, i) => (
              <HStack key={i} justify="space-between" fontSize="sm">
                <Text>{JSON.stringify(idp)}</Text>
                <Button size="2xs" variant="outline" onClick={() => unlink.mutate((idp as { id?: string }).id ?? '')}>unlink</Button>
              </HStack>
            ))
          ) : (
            <Text fontSize="sm" color="gray.500">No linked accounts.</Text>
          )}
          <HStack gap={2} mt={2}>
            <Button size="sm" variant="outline" onClick={() => link.mutate('google')} loading={link.isPending}>Link Google</Button>
            <Button size="sm" variant="outline" onClick={() => link.mutate('github')} loading={link.isPending}>Link GitHub</Button>
          </HStack>
        </VStack>
      </Box>
    </VStack>
  )
}
