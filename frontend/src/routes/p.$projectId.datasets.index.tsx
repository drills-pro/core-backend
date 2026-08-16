import { createFileRoute, Link } from '@tanstack/react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { VStack, HStack, Button, Input, Table, Code, Text } from '@chakra-ui/react'
import { api } from '../lib/api'
import { ProjectNav } from '../components/ProjectNav'

export const Route = createFileRoute('/p/$projectId/datasets/')({
  component: DatasetsPage,
})

function DatasetsPage() {
  const { projectId } = Route.useParams()
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const { data: datasets } = useQuery({ queryKey: ['datasets', projectId], queryFn: () => api.listDatasets(projectId) })
  const createDataset = useMutation({
    mutationFn: (n: string) => api.createDataset(projectId, n),
    onSuccess: () => {
      setName('')
      queryClient.invalidateQueries({ queryKey: ['datasets', projectId] })
    },
  })

  return (
    <VStack align="stretch" gap={6}>
      <ProjectNav projectId={projectId} />
      <HStack>
        <Input placeholder="Dataset name" value={name} onChange={(e) => setName(e.target.value)} maxW="300px" bg="white" />
        <Button size="sm" onClick={() => name && createDataset.mutate(name)} loading={createDataset.isPending}>
          Create dataset
        </Button>
      </HStack>
      <Table.Root size="sm" bg="white">
        <Table.Header>
          <Table.Row>
            <Table.ColumnHeader>Name</Table.ColumnHeader>
            <Table.ColumnHeader>S3 prefix</Table.ColumnHeader>
          </Table.Row>
        </Table.Header>
        <Table.Body>
          {datasets?.map((d) => (
            <Table.Row key={d.id} _hover={{ bg: 'gray.50' }}>
              <Table.Cell>
                <Link to="/p/$projectId/datasets/$datasetId" params={{ projectId, datasetId: d.id }}>{d.name}</Link>
              </Table.Cell>
              <Table.Cell><Code fontSize="xs">{d.s3_prefix}</Code></Table.Cell>
            </Table.Row>
          ))}
        </Table.Body>
      </Table.Root>
      {datasets?.length === 0 && <Text color="gray.500">No datasets yet.</Text>}
    </VStack>
  )
}
