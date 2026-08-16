import { Link, useRouterState } from '@tanstack/react-router'
import { HStack, Heading, VStack, Badge } from '@chakra-ui/react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'

const TABS: { to: string; label: string }[] = [
  { to: '/p/$projectId', label: 'Overview' },
  { to: '/p/$projectId/datasets', label: 'Datasets' },
  { to: '/p/$projectId/training', label: 'Training' },
  { to: '/p/$projectId/models', label: 'Models' },
  { to: '/p/$projectId/deployments', label: 'Deployments' },
  { to: '/p/$projectId/settings', label: 'Settings' },
  { to: '/p/$projectId/members', label: 'Members' },
  { to: '/p/$projectId/usage', label: 'Usage' },
]

export function ProjectNav({ projectId }: { projectId: string }) {
  const pathname = useRouterState({ select: (s) => s.location.pathname })
  const { data: project } = useQuery({ queryKey: ['project', projectId], queryFn: () => api.getProject(projectId) })

  return (
    <VStack align="stretch" gap={3} mb={6}>
      <HStack>
        <Heading size="lg">{project?.name ?? 'Project'}</Heading>
        {project?.task_type && <Badge colorPalette="purple">{project.task_type}</Badge>}
      </HStack>
      <HStack gap={4} borderBottomWidth={1} pb={2} wrap="wrap">
        {TABS.map((t) => {
          const href = t.to.replace('$projectId', projectId)
          const active = pathname === href
          return (
            <Link key={t.to} to={t.to} params={{ projectId }} style={{ fontWeight: active ? 700 : 400 }}>
              {t.label}
            </Link>
          )
        })}
      </HStack>
    </VStack>
  )
}
