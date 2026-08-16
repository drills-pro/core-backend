import { createRootRoute, Outlet, Link, useNavigate, useRouterState } from '@tanstack/react-router'
import { Box, Flex, VStack, Heading, Spacer, Button, Text, NativeSelect } from '@chakra-ui/react'
import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getAuth, logout, decodeIdToken } from '../lib/auth'
import { api } from '../lib/api'

function Sidebar() {
  const navigate = useNavigate()
  const pathname = useRouterState({ select: (s) => s.location.pathname })
  const [email, setEmail] = useState<string | null>(null)
  const authed = !!getAuth()

  useEffect(() => {
    const auth = getAuth()
    if (auth) {
      const claims = decodeIdToken(auth.id_token)
      setEmail((claims.email as string) ?? (claims.preferred_username as string) ?? 'signed in')
    } else {
      setEmail(null)
    }
  }, [pathname])

  const { data: teams } = useQuery({ queryKey: ['teams'], queryFn: api.listTeams, enabled: authed })

  // Figure out the "current" team from the URL: /t/:teamId/* or /p/:projectId/* (via project.team_id)
  const teamMatch = pathname.match(/^\/t\/([^/]+)/)
  const projectMatch = pathname.match(/^\/p\/([^/]+)/)
  const { data: currentProject } = useQuery({
    queryKey: ['project', projectMatch?.[1]],
    queryFn: () => api.getProject(projectMatch![1]),
    enabled: authed && !!projectMatch,
  })
  const currentTeamId = teamMatch?.[1] ?? currentProject?.team_id
  const { data: teamProjects } = useQuery({
    queryKey: ['projects'],
    queryFn: api.listProjects,
    enabled: authed && !!currentTeamId,
  })

  if (pathname === '/login' || pathname === '/register') return null

  return (
    <Flex as="nav" direction="column" w="240px" minH="100vh" borderRightWidth={1} bg="gray.900" color="white" px={4} py={4} gap={6}>
      <Heading size="md">
        <Link to="/">platform</Link>
      </Heading>

      {authed && (
        <VStack align="stretch" gap={4}>
          <Box>
            <Text fontSize="xs" color="gray.400" mb={1}>Team</Text>
            <NativeSelect.Root size="sm">
              <NativeSelect.Field
                value={currentTeamId ?? ''}
                onChange={(e) => e.target.value && navigate({ to: '/t/$teamId', params: { teamId: e.target.value } })}
                color="black"
                bg="white"
              >
                <option value="" disabled>Select a team…</option>
                {teams?.map((t) => (
                  <option key={t.id} value={t.id}>{t.name}</option>
                ))}
              </NativeSelect.Field>
              <NativeSelect.Indicator />
            </NativeSelect.Root>
          </Box>

          {currentTeamId && (
            <Box>
              <Text fontSize="xs" color="gray.400" mb={1}>Project</Text>
              <NativeSelect.Root size="sm">
                <NativeSelect.Field
                  value={projectMatch?.[1] ?? ''}
                  onChange={(e) => e.target.value && navigate({ to: '/p/$projectId', params: { projectId: e.target.value } })}
                  color="black"
                  bg="white"
                >
                  <option value="" disabled>Select a project…</option>
                  {teamProjects?.filter((p) => p.team_id === currentTeamId).map((p) => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </NativeSelect.Field>
                <NativeSelect.Indicator />
              </NativeSelect.Root>
            </Box>
          )}

          <VStack align="stretch" gap={1} fontSize="sm">
            <Link to="/">Teams</Link>
            {currentTeamId && <Link to="/t/$teamId" params={{ teamId: currentTeamId }}>Team dashboard</Link>}
            {currentTeamId && <Link to="/t/$teamId/settings" params={{ teamId: currentTeamId }}>Team settings</Link>}
            <Link to="/explore">Explore</Link>
          </VStack>
        </VStack>
      )}

      <Spacer />
      {email ? (
        <VStack align="stretch" gap={2}>
          <Link to="/settings/profile">
            <Text fontSize="sm" color="gray.300">{email}</Text>
          </Link>
          <Button size="sm" variant="outline" colorPalette="gray" onClick={() => logout()}>
            Sign out
          </Button>
        </VStack>
      ) : (
        <Button size="sm" onClick={() => navigate({ to: '/login' })}>
          Sign in
        </Button>
      )}
    </Flex>
  )
}

export const Route = createRootRoute({
  component: () => (
    <Flex minH="100vh" bg="gray.50">
      <Sidebar />
      <Box flex="1" maxW="1200px" mx="auto" px={6} py={8} w="100%">
        <Outlet />
      </Box>
    </Flex>
  ),
})
