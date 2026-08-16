import { createFileRoute, Link } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { Box, HStack, VStack, Text, Badge, SimpleGrid, Heading, Button } from '@chakra-ui/react'
import { api } from '../lib/api'
import { ProjectNav } from '../components/ProjectNav'

export const Route = createFileRoute('/p/$projectId/')({
  component: ProjectOverview,
})

function ProjectOverview() {
  const { projectId } = Route.useParams()
  const { data: project } = useQuery({ queryKey: ['project', projectId], queryFn: () => api.getProject(projectId) })
  const { data: datasets } = useQuery({ queryKey: ['datasets', projectId], queryFn: () => api.listDatasets(projectId) })
  const { data: runs } = useQuery({ queryKey: ['training-runs', projectId], queryFn: () => api.listTrainingRuns(projectId) })
  const { data: deployments } = useQuery({ queryKey: ['deployments', projectId], queryFn: () => api.listDeployments(projectId) })

  return (
    <VStack align="stretch" gap={6}>
      <ProjectNav projectId={projectId} />

      <Box bg="white" borderWidth={1} borderRadius="md" p={5}>
        <HStack mb={4} wrap="wrap">
          <Badge colorPalette="purple">{project?.task_type ?? '—'}</Badge>
          <Badge colorPalette={project?.status === 'active' ? 'green' : 'gray'}>{project?.status}</Badge>
          <Badge colorPalette={project?.visibility === 'public' ? 'blue' : 'gray'}>{project?.visibility ?? 'private'}</Badge>
        </HStack>
        <SimpleGrid columns={{ base: 2, md: 4 }} gap={4}>
          <Stat label="Datasets" value={datasets?.length} />
          <Stat label="Training runs" value={runs?.length} />
          <Stat label="Succeeded runs" value={runs?.filter((r) => r.status === 'succeeded').length} />
          <Stat label="Deployments" value={deployments?.length} />
        </SimpleGrid>
      </Box>

      <Box bg="white" borderWidth={1} borderRadius="md" p={5}>
        <Heading size="sm" mb={3}>Quick actions</Heading>
        <HStack wrap="wrap" gap={3}>
          <Link to="/p/$projectId/datasets" params={{ projectId }}><Button size="sm" variant="outline">Upload data</Button></Link>
          <Link to="/p/$projectId/training" params={{ projectId }}><Button size="sm" variant="outline">Start a training run</Button></Link>
          <Link to="/p/$projectId/models" params={{ projectId }}><Button size="sm" variant="outline">View models</Button></Link>
          <Link to="/p/$projectId/deployments" params={{ projectId }}><Button size="sm" variant="outline">Deploy</Button></Link>
        </HStack>
      </Box>
    </VStack>
  )
}

function Stat({ label, value }: { label: string; value: number | undefined }) {
  return (
    <Box>
      <Text fontSize="xs" color="gray.500">{label}</Text>
      <Text fontSize="2xl" fontWeight="bold">{value ?? '—'}</Text>
    </Box>
  )
}
