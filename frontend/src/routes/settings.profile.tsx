import { createFileRoute } from '@tanstack/react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { VStack, HStack, Button, Text, Box, Heading } from '@chakra-ui/react'
import { api } from '../lib/api'
import { decodeIdToken, getAuth } from '../lib/auth'

export const Route = createFileRoute('/settings/profile')({
  component: ProfilePage,
})

function ProfilePage() {
  const queryClient = useQueryClient()
  const { data: idps } = useQuery({ queryKey: ['linked-idps'], queryFn: api.listLinkedIdps, retry: false })
  // Name and email live in Zitadel; they arrive as id_token claims and are display-only here.
  const auth = getAuth()
  const claims = auth ? decodeIdToken(auth.id_token) : {}
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
        <Heading size="sm" mb={3}>Account</Heading>
        <VStack align="stretch" gap={1} fontSize="sm">
          <Text>Name: {(claims.name as string) ?? '—'}</Text>
          <Text>Email: {(claims.email as string) ?? '—'}</Text>
          <Text color="gray.500">Managed in your sign-in provider.</Text>
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
