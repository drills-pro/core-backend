import { createFileRoute } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { VStack, HStack, Table, Text, Box, Heading, Button, Badge } from '@chakra-ui/react'
import { api } from '../lib/api'

export const Route = createFileRoute('/explore')({
  component: ExplorePage,
})

function ExplorePage() {
  const { data: projects } = useQuery({ queryKey: ['public-projects'], queryFn: api.listPublicProjects })
  const [expanded, setExpanded] = useState<string | null>(null)

  return (
    <VStack align="stretch" gap={6}>
      <Heading size="lg">Explore public projects</Heading>
      <Box bg="white" borderWidth={1} borderRadius="md">
        <Table.Root>
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader>Name</Table.ColumnHeader>
              <Table.ColumnHeader>Task type</Table.ColumnHeader>
              <Table.ColumnHeader></Table.ColumnHeader>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {projects?.map((p) => (
              <Table.Row key={p.id}>
                <Table.Cell>{p.name ?? <Text as="span" color="gray.400">(metadata not published)</Text>}</Table.Cell>
                <Table.Cell>{p.task_type && <Badge>{String(p.task_type)}</Badge>}</Table.Cell>
                <Table.Cell>
                  <Button size="2xs" variant="ghost" onClick={() => setExpanded(expanded === p.id ? null : p.id)}>
                    {expanded === p.id ? 'hide' : 'view'}
                  </Button>
                </Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </Table.Root>
        {projects?.length === 0 && <Text p={6} color="gray.500">No public projects yet.</Text>}
      </Box>
      {expanded && <PublicProjectDetail projectId={expanded} />}
    </VStack>
  )
}

function PublicProjectDetail({ projectId }: { projectId: string }) {
  const { data: datasets } = useQuery({ queryKey: ['public-datasets', projectId], queryFn: () => api.listPublicProjectDatasets(projectId) })
  const { data: models } = useQuery({ queryKey: ['public-models', projectId], queryFn: () => api.listPublicProjectModels(projectId) })

  return (
    <HStack align="start" gap={6} wrap="wrap">
      <Box borderWidth={1} borderRadius="md" p={4} bg="white" minW="280px">
        <Text fontWeight="medium" mb={2}>Published datasets</Text>
        {datasets && datasets.length > 0 ? (
          datasets.map((d, i) => <Text key={i} fontSize="xs" color="gray.600">{JSON.stringify(d)}</Text>)
        ) : (
          <Text fontSize="sm" color="gray.500">None published.</Text>
        )}
      </Box>
      <Box borderWidth={1} borderRadius="md" p={4} bg="white" minW="280px">
        <Text fontWeight="medium" mb={2}>Published models</Text>
        {models && models.length > 0 ? (
          models.map((m, i) => <Text key={i} fontSize="xs" color="gray.600">{JSON.stringify(m)}</Text>)
        ) : (
          <Text fontSize="sm" color="gray.500">None published.</Text>
        )}
      </Box>
    </HStack>
  )
}
